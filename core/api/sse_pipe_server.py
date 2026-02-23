"""
SSE 管道：浏览器将 fetch 得到的响应体流式 POST 到本服务，Python 从队列读取。
用于替代 expose_binding，避免非 Playwright 启动的浏览器上 binding 在 goto 后失效。
"""

import asyncio
import logging
from typing import Any

from fastapi import Request
from fastapi.responses import PlainTextResponse

logger = logging.getLogger(__name__)

_queues: dict[str, asyncio.Queue[str | None]] = {}
_DONE = "__done__"


def register_queue(request_id: str) -> asyncio.Queue[str | None]:
    """注册一个队列，供 POST 处理器写入；请求方从队列读取直到收到 __done__。"""
    q: asyncio.Queue[str | None] = asyncio.Queue()
    _queues[request_id] = q
    return q


def get_queue(request_id: str) -> asyncio.Queue[str | None] | None:
    return _queues.get(request_id)


def unregister(request_id: str) -> None:
    _queues.pop(request_id, None)


async def handle_sse_pipe(request: Request, request_id: str) -> PlainTextResponse:
    """
    接收浏览器 POST 的流式 body，按块放入队列；流结束后放入 __done__。
    """
    queue = get_queue(request_id)
    if queue is None:
        return PlainTextResponse("queue not found", status_code=404)
    try:
        body = await request.body()
        # 单次 POST 可能是一次性 body（如 __error__:HTTP 400）或流式已缓冲
        text = body.decode("utf-8", errors="replace")
        if text.strip():
            await queue.put(text)
    except Exception as e:
        logger.warning("sse_pipe 读 body 异常 request_id=%s: %s", request_id, e)
        await queue.put(f"__error__: {e!s}")
    finally:
        await queue.put(_DONE)
        unregister(request_id)
    return PlainTextResponse("ok")
