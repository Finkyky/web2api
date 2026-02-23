"""
通过 CDP 连接浏览器：打开 chatgpt.com，设置 Cookie test-key=112233，然后刷新页面。
用法：先启动指纹浏览器并开启远程调试端口 9222，再运行本脚本。
"""

import asyncio
import logging

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://127.0.0.1:9222"
TARGET_URL = "https://chatgpt.com/"


async def main() -> None:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(
        CDP_ENDPOINT,
        timeout=10000,
    )
    try:
        ctx = browser.contexts[0] if browser.contexts else None
        if not ctx:
            logger.warning("没有默认 context")
            return
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        logger.info("打开: %s", TARGET_URL)
        await page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=20000)
        logger.info("设置 Cookie: test-key=112233")
        await ctx.add_cookies(
            [
                {
                    "name": "test-key",
                    "value": "112233",
                    "domain": ".chatgpt.com",
                    "path": "/",
                }
            ]
        )
        logger.info("刷新页面")
        await page.reload(wait_until="domcontentloaded", timeout=20000)
        logger.info("完成")
    finally:
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
