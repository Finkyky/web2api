"""
Mock Claude API：与 claude.py 调用格式兼容，不消耗 token。
设置 CLAUDE_START_URL 和 CLAUDE_API_BASE 指向 http://ip:port/mock 即可调试。
"""

import asyncio
import json
import uuid as uuid_mod
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse

router = APIRouter(prefix="/mock", tags=["mock"])

MOCK_ORG_UUID = "00000000-0000-0000-0000-000000000001"

# MOCK_REPLY = """
# Thought: 用户要求在 utils.py 中写一个快速排序算法。首先需要读取该文件以确认是否存在以及当前内容，然后决定是编辑还是新建。
# Action: Read
# Action Input: {"path": "utils.py"}
# """
MOCK_REPLY = """
用户要求在 utils.py 中写一个快速排序算法。首先需要读取该文件以确认是否存在以及当前内容，然后决定是编辑还是新建
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def mock_start_page() -> str:
    """CLAUDE_START_URL 指向 /mock 时，浏览器加载此页。"""
    return """
<!DOCTYPE html>
<html><head><title>Mock Claude</title></head>
<body><p>Mock Claude - 调试用</p></body>
</html>
"""


@router.get("/account")
def mock_account() -> dict:
    """_get_org_uuid 调用的 GET /account，返回 memberships 含 org uuid。"""
    return {
        "memberships": [
            {"organization": {"uuid": MOCK_ORG_UUID}},
        ],
    }


@router.post("/organizations/{org_uuid}/chat_conversations")
def mock_create_conversation(org_uuid: str) -> dict:
    """_post_create_conversation 调用的创建会话接口。"""
    return {
        "uuid": str(uuid_mod.uuid4()),
    }


@router.post("/organizations/{org_uuid}/chat_conversations/{conv_uuid}/completion")
async def mock_completion(
    org_uuid: str,
    conv_uuid: str,  # noqa: ARG001
) -> StreamingResponse:
    """stream_completion 调用的 completion 接口，返回 SSE 流。"""

    async def sse_stream() -> AsyncIterator[str]:
        msg_uuid = str(uuid_mod.uuid4())
        # 先发 2KB 填充，绕过代理/Nginx 等对 SSE 的缓冲（通常缓冲到 ~1-4KB 才输出）
        yield ": " + " " * 2046 + "\n\n"
        # message_start
        yield f"data: {json.dumps({'type': 'message_start', 'message': {'id': msg_uuid, 'uuid': msg_uuid, 'model': 'claude-sonnet-4-5-20250929', 'type': 'message', 'role': 'assistant'}})}\n\n"
        # content_block_start
        yield f"data: {json.dumps({'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        # content_block_delta 分块流式输出
        chunk_size = 2
        for i in range(0, len(MOCK_REPLY), chunk_size):
            chunk = MOCK_REPLY[i : i + chunk_size]
            yield f"data: {json.dumps({'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': chunk}})}\n\n"
            await asyncio.sleep(0.3)
        # content_block_stop
        yield f"data: {json.dumps({'type': 'content_block_stop', 'index': 0})}\n\n"
        # message_stop
        yield f"data: {json.dumps({'type': 'message_stop'})}\n\n"

    return StreamingResponse(
        sse_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
