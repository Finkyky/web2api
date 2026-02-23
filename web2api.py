"""
Web2API：启动指纹浏览器 + 通过 CDP 调用 claude.ai 流式聊天。单文件整合。
用法:
  uv run python CDPDemo/web2api.py launch
  uv run python CDPDemo/web2api.py chat --chat "有色金属"
  uv run python CDPDemo/web2api.py chat --launch --chat "有色金属"
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

_SCRIPT_DIR = Path(__file__).resolve().parent

# --- 启动浏览器（原 launch_chromium）---
CHROMIUM_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
PROFILE_ID = "wisconsin"
PROXY_HOST = "sg.arxlabs.io:3010"
TIMEZONE = "America/Chicago"
FINGERPRINT_ID = "4567"
REMOTE_DEBUGGING_PORT = 9222
CDP_ENDPOINT = "http://127.0.0.1:9222"

# --- Claude API（原 cdp_demo）---
CLAUDE_API_BASE = "https://claude.ai/api"

_PAGE_FETCH_STREAM_JS = """
async ({ url, body }) => {
  const done = "__done__";
  const errPrefix = "__error__:";
  try {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 90000);
    const resp = await fetch(url, {
      method: "POST",
      body: body,
      headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
      signal: ctrl.signal
    });
    clearTimeout(t);
    if (!resp.ok) {
      await window.sendChunk(errPrefix + "HTTP " + resp.status);
      await window.sendChunk(done);
      return;
    }
    if (!resp.body) {
      await window.sendChunk(errPrefix + "No response body");
      await window.sendChunk(done);
      return;
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    while (true) {
      const { done: streamDone, value } = await reader.read();
      if (streamDone) break;
      await window.sendChunk(dec.decode(value));
    }
  } catch (e) {
    const msg = e.name === "AbortError" ? "请求超时(90s)" : (e.message || String(e));
    await window.sendChunk(errPrefix + msg);
  }
  await window.sendChunk(done);
}
"""


def launch_browser() -> Optional[subprocess.Popen]:
    """启动 Chromium（代理 + 认证扩展），返回进程对象；失败返回 None。"""
    extension_path = _SCRIPT_DIR / "proxy-auth-extension"
    user_data_dir = Path.home() / "fp-data" / PROFILE_ID
    if not Path(CHROMIUM_BIN).exists():
        print(f"Chromium 不存在: {CHROMIUM_BIN}", file=sys.stderr)
        return None
    if not extension_path.is_dir():
        print(f"扩展目录不存在: {extension_path}", file=sys.stderr)
        return None
    args = [
        CHROMIUM_BIN,
        f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}",
        f"--load-extension={extension_path}",
        f"--fingerprint={FINGERPRINT_ID}",
        "--fingerprint-platform=windows",
        "--fingerprint-brand=Edge",
        f"--user-data-dir={user_data_dir}",
        f"--timezone={TIMEZONE}",
        f"--proxy-server=http://{PROXY_HOST}",
        "--force-webrtc-ip-handling-policy",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=AsyncDNS",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    return subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_for_cdp(
    endpoint: str, max_attempts: int = 30, interval: float = 1.0
) -> bool:
    parsed = urlparse(endpoint)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or REMOTE_DEBUGGING_PORT
    for _ in range(max_attempts):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(interval)
    return False


async def connect_browser(
    endpoint: str = CDP_ENDPOINT, timeout: float = 10000
) -> Browser:
    playwright = await async_playwright().start()
    return await playwright.chromium.connect_over_cdp(endpoint, timeout=timeout)


async def _ensure_claude_page(context: BrowserContext) -> Page:
    if context.pages:
        page = context.pages[0]
        if "claude.ai" in (page.url or ""):
            return page
        await page.goto(
            "https://claude.ai", wait_until="domcontentloaded", timeout=20000
        )
        return page
    page = await context.new_page()
    await page.goto("https://claude.ai", wait_until="domcontentloaded", timeout=20000)
    return page


async def _get_org_uuid(context: BrowserContext) -> Optional[str]:
    resp = await context.request.get(f"{CLAUDE_API_BASE}/account", timeout=15000)
    if resp.status != 200:
        await resp.dispose()
        return None
    data = await resp.json()
    await resp.dispose()
    memberships = data.get("memberships") or []
    if not memberships:
        return None
    org = memberships[0].get("organization") or {}
    return org.get("uuid")


async def _create_conversation(context: BrowserContext, org_uuid: str) -> Optional[str]:
    url = f"{CLAUDE_API_BASE}/organizations/{org_uuid}/chat_conversations"
    resp = await context.request.post(
        url,
        data=json.dumps({"name": "", "model": "claude-sonnet-4-5-20250929"}),
        headers={"Content-Type": "application/json"},
        timeout=15000,
    )
    if resp.status not in (200, 201):
        text = (await resp.text())[:500]
        await resp.dispose()
        logger.warning("创建会话失败 %s: %s", resp.status, text)
        return None
    data = await resp.json()
    await resp.dispose()
    return data.get("uuid")


def _get_conversation_from_spec(spec_path: Path) -> Optional[tuple[str, str]]:
    if not spec_path.is_file():
        return None
    try:
        spec = json.loads(spec_path.read_text())
        org = spec.get("org_uuid")
        conv = spec.get("conversation_uuid")
        if (
            isinstance(org, str)
            and isinstance(conv, str)
            and len(org) > 0
            and len(conv) > 0
        ):
            return (org.strip(), conv.strip())
    except Exception:  # noqa: S110
        pass
    return None


def _save_conversation_to_spec(
    spec_path: Path,
    *,
    org_uuid: Optional[str] = None,
    conversation_uuid: Optional[str] = None,
    parent_message_uuid: Optional[str] = None,
) -> None:
    spec: dict[str, Any] = {}
    if spec_path.is_file():
        try:
            spec = json.loads(spec_path.read_text())
        except Exception:  # noqa: S110
            pass
    if org_uuid is not None:
        spec["org_uuid"] = org_uuid
    if conversation_uuid is not None:
        spec["conversation_uuid"] = conversation_uuid
    if parent_message_uuid is not None:
        body = spec.setdefault("request_body_json", {})
        if isinstance(body, dict):
            body = dict(body)
            body["parent_message_uuid"] = parent_message_uuid
            spec["request_body_json"] = body
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2))


def _completion_body_from_spec(
    spec_path: Path, message: str
) -> Optional[dict[str, Any]]:
    """
    以默认完整 body 为基准，仅从 spec 合并 parent_message_uuid（用于续写同一会话）。
    spec 里只持久化了 parent_message_uuid，若直接用 spec 的 request_body_json 会缺少
    prompt/timezone/tools 等必填字段，导致第二次请求 400。因此必须基于 default body。
    """
    body = _default_completion_body(message)
    if not spec_path.is_file():
        return body
    try:
        spec = json.loads(spec_path.read_text())
        saved = spec.get("request_body_json")
        if not isinstance(saved, dict):
            return body
        parent_uuid = saved.get("parent_message_uuid")
        if parent_uuid:
            body["parent_message_uuid"] = parent_uuid
        return body
    except Exception:  # noqa: S110
        return body


def _default_completion_body(message: str) -> dict[str, Any]:
    print("*" * 20)
    print(message)
    print("*" * 20)
    # return {"prompt": message}
    return {
        "prompt": message,
        "timezone": "America/Chicago",
        "personalized_styles": [
            {
                "type": "default",
                "key": "Default",
                "name": "Normal",
                "nameKey": "normal_style_name",
                "prompt": "Normal\n",
                "summary": "Default responses from Claude",
                "summaryKey": "normal_style_summary",
                "isDefault": True,
            }
        ],
        "locale": "en-US",
        "tools": [
            {"type": "web_search_v0", "name": "web_search"},
            {"type": "artifacts_v0", "name": "artifacts"},
            {"type": "repl_v0", "name": "repl"},
            {"type": "widget", "name": "weather_fetch"},
            {"type": "widget", "name": "recipe_display_v0"},
            {"type": "widget", "name": "places_map_display_v0"},
            {"type": "widget", "name": "message_compose_v1"},
            {"type": "widget", "name": "ask_user_input_v0"},
            {"type": "widget", "name": "places_search"},
            {"type": "widget", "name": "fetch_sports_data"},
        ],
        "attachments": [],
        "files": [],
        "sync_sources": [],
        "rendering_mode": "messages",
        "create_conversation_params": {
            "name": "",
            "include_conversation_preferences": True,
            "is_temporary": False,
        },
    }


def _parse_sse_buffer(buffer: str, chunk: str) -> tuple[str, list[str], Optional[str]]:
    buffer += chunk
    lines = buffer.split("\n")
    buffer = lines[-1]
    result: list[str] = []
    message_id: Optional[str] = None
    for line in lines[:-1]:
        line = line.strip()
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]" or not payload:
            continue
        try:
            obj = json.loads(payload)
            if not isinstance(obj, dict):
                continue
            kind = obj.get("type")
            if kind == "content_block_delta":
                delta = obj.get("delta")
                if (
                    isinstance(delta, dict)
                    and delta.get("type") == "text_delta"
                    and "text" in delta
                ):
                    result.append(str(delta["text"]))
            elif kind == "completion" and "completion" in obj:
                result.append(str(obj["completion"]))
            elif kind == "message_start":
                msg = obj.get("message")
                if isinstance(msg, dict):
                    for key in ("id", "uuid"):
                        if msg.get(key):
                            message_id = str(msg[key])
                            break
                if not message_id:
                    mid = (
                        obj.get("message_id")
                        or obj.get("message_uuid")
                        or obj.get("uuid")
                    )
                    if mid:
                        message_id = str(mid)
        except json.JSONDecodeError:
            pass
    return (buffer, result, message_id)


async def _post_completion_stream_via_page(
    page: Page,
    org_uuid: str,
    conversation_uuid: str,
    message: str,
    spec_path: Path,
    out_message_id: Optional[list[str]] = None,
    parent_message_uuid: Optional[str] = None,
) -> AsyncIterator[str]:
    url = f"{CLAUDE_API_BASE}/organizations/{org_uuid}/chat_conversations/{conversation_uuid}/completion"
    body = _default_completion_body(message)
    if parent_message_uuid:
        body["parent_message_uuid"] = parent_message_uuid
    body_json = json.dumps(body)
    chunk_queue: asyncio.Queue[str | dict[str, Any]] = asyncio.Queue()

    def on_chunk(_source: dict[str, Any], chunk: str | dict[str, Any]) -> None:
        chunk_queue.put_nowait(chunk)

    await page.expose_binding("sendChunk", on_chunk)

    async def run_fetch() -> None:
        await page.evaluate(_PAGE_FETCH_STREAM_JS, {"url": url, "body": body_json})

    fetch_task = asyncio.create_task(run_fetch())
    buffer = ""
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(chunk_queue.get(), timeout=130.0)
            except asyncio.TimeoutError:
                logger.warning("流式读取超时")
                break
            if not isinstance(chunk, str):
                if isinstance(chunk, dict) and len(chunk) == 1:
                    chunk = next(iter(chunk.values()))
                chunk = str(chunk) if chunk is not None else ""
            if chunk == "__done__":
                break
            if chunk.startswith("__error__:"):
                raise RuntimeError(chunk[10:].strip())
            buffer, texts, message_id = _parse_sse_buffer(buffer, chunk)
            if out_message_id is not None and message_id:
                out_message_id.append(message_id)
            for t in texts:
                yield t
    finally:
        fetch_task.cancel()
        try:
            await fetch_task
        except asyncio.CancelledError:
            pass


async def stream_claude_reply(
    context: BrowserContext, message: str
) -> AsyncIterator[str]:
    """
    与 _demo_claude_chat_stream 相同逻辑，但以异步生成器形式逐块 yield 助手回复内容。
    供 multi_web2api 等需要返回内容的调用方使用。
    """
    page = await _ensure_claude_page(context)
    spec_path = _SCRIPT_DIR / "completion_spec.json"
    org_uuid = await _get_org_uuid(context)
    if not org_uuid:
        raise RuntimeError("无法获取 org_uuid，请确认已登录 claude.ai")
    loaded = _get_conversation_from_spec(spec_path)
    if loaded and loaded[0] == org_uuid and loaded[1]:
        conv_uuid = loaded[1]
        logger.info("复用会话: %s", conv_uuid)
    else:
        conv_uuid = await _create_conversation(context, org_uuid)
        if not conv_uuid:
            raise RuntimeError("无法创建会话")
        _save_conversation_to_spec(
            spec_path, org_uuid=org_uuid, conversation_uuid=conv_uuid
        )
        logger.info("已创建会话: %s", conv_uuid)
    # 不传 parent_message_uuid：Claude 网页 completion 在复用同一 conv 时，续发仅需 prompt，
    # 带 parent 易因格式/会话不一致导致 400，故每次均用「首条消息」形式的 body
    out_message_id: list[str] = []
    async for piece in _post_completion_stream_via_page(
        page,
        org_uuid,
        conv_uuid,
        message,
        spec_path,
        out_message_id,
        parent_message_uuid=None,
    ):
        yield piece
    if out_message_id:
        _save_conversation_to_spec(spec_path, parent_message_uuid=out_message_id[-1])


async def _demo_claude_chat_stream(context: BrowserContext, message: str) -> None:
    try:
        full: list[str] = []
        async for piece in stream_claude_reply(context, message):
            print(piece, end="", flush=True)
            full.append(piece)
        print()
        if full:
            logger.info("流式回复总长度: %d 字符", len("".join(full)))
    except RuntimeError as e:
        logger.warning("%s", e)


async def _run_chat(
    endpoint: str = CDP_ENDPOINT, chat_message: Optional[str] = None
) -> None:
    browser = await connect_browser(endpoint)
    try:
        default_ctx = browser.contexts[0] if browser.contexts else None
        if not default_ctx:
            logger.warning("没有默认 context")
            return
        if chat_message is not None:
            logger.info("调用 claude.ai 聊天 API（流式），发送: %r", chat_message)
            await _demo_claude_chat_stream(default_ctx, chat_message)
    finally:
        await browser.close()


def _cmd_launch(_args: argparse.Namespace) -> int:
    proc = launch_browser()
    if proc is None:
        return 1
    print(f"已启动 Chromium PID={proc.pid}，CDP 端口 {REMOTE_DEBUGGING_PORT}")
    return 0


async def _cmd_chat_async(args: argparse.Namespace) -> int:
    if getattr(args, "launch", False):
        proc = launch_browser()
        if proc is None:
            return 1
        print("正在等待 CDP 就绪...", flush=True)
        endpoint = getattr(args, "endpoint", CDP_ENDPOINT)
        if not await _wait_for_cdp(endpoint):
            print("CDP 未在预期时间内就绪", file=sys.stderr)
            return 1
    await _run_chat(
        endpoint=getattr(args, "endpoint", CDP_ENDPOINT),
        chat_message=getattr(args, "chat_message", None),
    )
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    return asyncio.run(_cmd_chat_async(args))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Web2API：启动浏览器或通过 CDP 向 claude.ai 发流式聊天",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    launch_p = sub.add_parser(
        "launch", help="启动带代理与认证扩展的 Chromium（CDP 9222）"
    )
    launch_p.set_defaults(func=_cmd_launch)
    chat_p = sub.add_parser(
        "chat", help="连接浏览器并向 claude.ai 发送消息（流式回复）"
    )
    chat_p.set_defaults(func=_cmd_chat)
    chat_p.add_argument(
        "--chat",
        dest="chat_message",
        metavar="TEXT",
        required=True,
        help="要发送的消息",
    )
    chat_p.add_argument("--endpoint", default=CDP_ENDPOINT, help="CDP 端点")
    chat_p.add_argument(
        "--launch", action="store_true", help="若浏览器未启动则先启动再发消息"
    )
    chat_p.add_argument(
        "--stream", action="store_true", help="流式输出（当前默认即流式，保留兼容）"
    )
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
