"""
通过 CDP 连接已启动的指纹浏览器（端口 9222），向 claude.ai 发流式聊天。
用法：先启动指纹浏览器并开启远程调试 9222，再运行：
  uv run python CDPDemo/cdp_demo.py --chat "有色金属" --stream
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CDP_ENDPOINT = "http://127.0.0.1:9222"
CLAUDE_API_BASE = "https://claude.ai/api"


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
        data=json.dumps({"name": "", "model": "claude-sonnet-4-20250514"}),
        headers={"Content-Type": "application/json"},
        timeout=15000,
    )
    if resp.status != 200 and resp.status != 201:
        text = (await resp.text())[:500]
        await resp.dispose()
        logger.warning("创建会话失败 %s: %s", resp.status, text)
        return None
    data = await resp.json()
    await resp.dispose()
    return data.get("uuid")


def _get_conversation_from_spec(
    spec_path: Path,
) -> Optional[tuple[str, str]]:
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
    if not spec_path.is_file():
        return None
    try:
        spec = json.loads(spec_path.read_text())
        body = spec.get("request_body_json")
        if not isinstance(body, dict):
            return None
        body = dict(body)
        if "text" in body:
            body["text"] = message
        if "prompt" in body:
            body["prompt"] = message
        if "message" in body and isinstance(body["message"], dict):
            body["message"] = {**body["message"], "text": message}
        return body
    except Exception:  # noqa: S110
        return None


def _default_completion_body(message: str) -> dict[str, Any]:
    return {"prompt": message}


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


async def _post_completion_stream_via_page(
    page: Page,
    org_uuid: str,
    conversation_uuid: str,
    message: str,
    spec_path: Path,
    out_message_id: Optional[list[str]] = None,
) -> AsyncIterator[str]:
    url = f"{CLAUDE_API_BASE}/organizations/{org_uuid}/chat_conversations/{conversation_uuid}/completion"
    body = _completion_body_from_spec(spec_path, message) or _default_completion_body(
        message
    )
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


async def demo_claude_chat_via_api_stream(
    context: BrowserContext, message: str
) -> None:
    page = await _ensure_claude_page(context)
    spec_path = Path(__file__).resolve().parent / "completion_spec.json"
    org_uuid = await _get_org_uuid(context)
    if not org_uuid:
        logger.warning("无法获取 org_uuid，请确认已登录 claude.ai")
        return
    loaded = _get_conversation_from_spec(spec_path)
    if loaded and loaded[0] == org_uuid and loaded[1]:
        conv_uuid = loaded[1]
        logger.info("复用会话: %s，流式回复:", conv_uuid)
    else:
        conv_uuid = await _create_conversation(context, org_uuid)
        if not conv_uuid:
            logger.warning("无法创建会话")
            return
        _save_conversation_to_spec(
            spec_path, org_uuid=org_uuid, conversation_uuid=conv_uuid
        )
        logger.info("已创建会话: %s，流式回复:", conv_uuid)
    out_message_id: list[str] = []
    try:
        full: list[str] = []
        async for piece in _post_completion_stream_via_page(
            page, org_uuid, conv_uuid, message, spec_path, out_message_id
        ):
            print(piece, end="", flush=True)
            full.append(piece)
        print()
        reply = "".join(full)
        if reply:
            logger.info("流式回复总长度: %d 字符", len(reply))
        if out_message_id:
            _save_conversation_to_spec(
                spec_path, parent_message_uuid=out_message_id[-1]
            )
    except RuntimeError as e:
        logger.warning("%s", e)


async def run_demo(
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
            await demo_claude_chat_via_api_stream(default_ctx, chat_message)
    finally:
        await browser.close()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="CDP 连接指纹浏览器，向 claude.ai 发流式聊天"
    )
    parser.add_argument(
        "--endpoint",
        default=CDP_ENDPOINT,
        help="CDP 端点，默认 http://127.0.0.1:9222",
    )
    parser.add_argument(
        "--chat",
        default=None,
        metavar="TEXT",
        dest="chat_message",
        help='发送的消息，例如 --chat "有色金属"',
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="流式输出（当前仅支持流式，保留此参数兼容）",
    )
    args = parser.parse_args()

    asyncio.run(run_demo(endpoint=args.endpoint, chat_message=args.chat_message))


if __name__ == "__main__":
    main()
