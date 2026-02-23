"""
代理认证：浏览器用代理启动后，遇到 407 时自动带认证，无需手动输入。
用法：先启动带代理的浏览器（--proxy-server=...），再设置 PROXY_USER/PROXY_PASS，运行本脚本。

优先用 Context 的 extra HTTP headers 带 Proxy-Authorization，多数情况下可避免弹窗；
若仍弹窗，再尝试 CDP Fetch.authRequired（部分 Chrome/指纹浏览器对代理 407 不触发该事件）。
"""

import asyncio
import base64
import logging
import os

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://127.0.0.1:9222"
PROXY_USER = os.environ.get("PROXY_USER", "your_proxy_user")
PROXY_PASS = os.environ.get("PROXY_PASS", "your_proxy_pass")


def _proxy_auth_header() -> str:
    raw = f"{PROXY_USER}:{PROXY_PASS}"
    return "Basic " + base64.b64encode(raw.encode()).decode()


async def main() -> None:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(CDP_ENDPOINT, timeout=10000)
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx:
            logger.warning("没有默认 context")
            return

        # 方案一：让所有请求自带 Proxy-Authorization，部分环境可避免 407 弹窗
        await ctx.set_extra_http_headers({"Proxy-Authorization": _proxy_auth_header()})
        logger.info("已设置 Context 的 Proxy-Authorization 头")

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        cdp = await page.context.new_cdp_session(page)

        auth_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

        def on_auth_required(params: dict) -> None:
            if auth_future.done():
                return
            logger.info("收到 CDP 认证请求: %s", params.get("authChallenge"))
            auth_future.set_result(params)

        cdp.on("Fetch.authRequired", on_auth_required)
        await cdp.send("Fetch.enable", {"handleAuthRequests": True})

        def on_response_received(params: dict) -> None:
            status = (params.get("response") or {}).get("status")
            if status == 407:
                logger.info("Network 收到 407（代理认证）")

        cdp.on("Network.responseReceived", on_response_received)
        await cdp.send("Network.enable", {})

        logger.info("打开页面（超时 60s）...")
        goto_task = asyncio.create_task(
            page.goto(
                "http://httpbin.org/ip",
                wait_until="domcontentloaded",
                timeout=60000,
            )
        )
        try:
            params = await asyncio.wait_for(auth_future, timeout=65.0)
            request_id = params.get("requestId")
            if request_id:
                await cdp.send(
                    "Fetch.continueWithAuth",
                    {
                        "requestId": request_id,
                        "authChallengeResponse": {
                            "response": "ProvideCredentials",
                            "username": PROXY_USER,
                            "password": PROXY_PASS,
                        },
                    },
                )
                logger.info("已发送 continueWithAuth")
            await goto_task
        except asyncio.TimeoutError:
            await goto_task
        logger.info("当前 URL: %s", page.url)
    except Exception as e:
        logger.warning("%s", e)
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
