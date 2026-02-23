"""
OpenAI 兼容路由：/{type}/v1/chat/completions、/{type}/v1/models。
依赖注入 ChatHandler（由 app 组装）。
支持 ReAct：解析 Thought/Action/Action Input 格式并转为 OpenAI tool_calls，由 Cursor 执行。
"""

import json
import logging
import re
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from core.api.chat_handler import ChatHandler
from core.api.function_call import (
    build_claude_text_sse_events,
    build_claude_tool_use_sse_events,
    build_tool_calls_response,
)
from core.api.conv_parser import strip_session_id_prefix
from core.api.react import (
    detect_react_mode,
    format_react_prompt,
    parse_react_output,
    react_output_to_tool_calls,
)
from core.api.schemas import OpenAIChatRequest, extract_user_content

logger = logging.getLogger(__name__)

OPENAI_MODEL_ID = "claude-sonnet-4-5-20250929"
CHAT_REQUEST_DEBUG = (
    Path(__file__).resolve().parent.parent.parent / "chat_request_debug.json"
)
CHAT_RESPONSE_DEBUG = (
    Path(__file__).resolve().parent.parent.parent / "chat_response_debug.json"
)


def _save_response_debug(data: dict | list) -> None:
    try:
        with open(CHAT_RESPONSE_DEBUG, "a", encoding="utf-8") as f:
            f.write("\n========\n")
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except OSError as e:
        logger.debug("追加 chat_response_debug 失败: %s", e)


def get_chat_handler(request: Request) -> ChatHandler:
    """从 app state 取出 ChatHandler。"""
    handler = getattr(request.app.state, "chat_handler", None)
    if handler is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    return handler


def create_router() -> APIRouter:
    """创建 v1 兼容路由，路径为 /{type}/v1/..."""
    router = APIRouter()

    @router.get("/{type}/v1/models")
    def list_models(type: str) -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": OPENAI_MODEL_ID,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": type,
                }
            ],
        }

    @router.post("/{type}/v1/chat/completions")
    async def chat_completions(
        type: str,
        req: OpenAIChatRequest,
        handler: ChatHandler = Depends(get_chat_handler),
    ) -> Any:
        if not req.messages:
            raise HTTPException(
                status_code=400,
                detail="messages 不能为空",
            )
        from core.api.function_call import format_tools_for_prompt

        tools_text = format_tools_for_prompt(req.tools or [])
        use_react = bool(req.tools)
        react_prompt_prefix = format_react_prompt(req.tools or []) if use_react else ""
        content = extract_user_content(
            req.messages,
            tools_text=tools_text,
            use_react=use_react,
            react_prompt_prefix=react_prompt_prefix,
        )
        if not content.strip():
            raise HTTPException(
                status_code=400,
                detail="messages 中需至少有一条带 content 的 user 消息",
            )

        try:
            with open(CHAT_REQUEST_DEBUG, "a", encoding="utf-8") as f:
                f.write("\n========\n")
                f.write(json.dumps(req.model_dump(), ensure_ascii=False, indent=2))
        except OSError as e:
            logger.debug("追加 chat_request_debug 失败: %s", e)

        chat_id = f"chatcmpl-{uuid_mod.uuid4().hex[:24]}"
        message_id = f"msg_{uuid_mod.uuid4().hex[:24]}"
        created = int(time.time())
        model = req.model or OPENAI_MODEL_ID

        if req.stream:

            async def sse_stream() -> AsyncIterator[str]:
                try:
                    buffer = ""
                    is_tool_call: bool | None = None
                    emitted_message_start = False
                    emitted_content_block_start = False
                    emitted_tool_calls = False
                    msg_start, block_start, make_delta_sse, make_stop_sse = (
                        build_claude_text_sse_events(message_id, model)
                    )
                    make_delta_sse = cast(Callable[[str], str], make_delta_sse)
                    make_stop_sse = cast(Callable[[], str], make_stop_sse)

                    async for chunk in handler.stream_completion(type, req):
                        buffer += chunk

                        if use_react and is_tool_call is None:
                            is_tool_call = detect_react_mode(buffer)
                        elif not use_react and is_tool_call is None:
                            is_tool_call = False

                        if is_tool_call is False:
                            if not emitted_message_start:
                                emitted_message_start = True
                                emitted_content_block_start = True
                                yield msg_start
                                yield block_start
                                content_to_send = buffer
                            else:
                                content_to_send = chunk
                            yield make_delta_sse(content_to_send)
                        # is_tool_call True 或 None：继续缓冲

                    if use_react and (is_tool_call is True or is_tool_call is None):
                        content_for_parse = strip_session_id_prefix(buffer)
                        parsed = parse_react_output(content_for_parse)
                        tool_calls_list = (
                            react_output_to_tool_calls(parsed) if parsed else []
                        )
                        if tool_calls_list and not emitted_tool_calls:
                            emitted_tool_calls = True
                            session_id_content = (
                                buffer[:64] if len(buffer) >= 64 else ""
                            )
                            thought = ""
                            if "Thought" in content_for_parse:
                                m = re.search(
                                    r"Thought[:：]\s*(.+?)(?=\s*Action[:：]|$)",
                                    content_for_parse,
                                    re.DOTALL | re.I,
                                )
                                thought = (m.group(1) or "").strip() if m else ""
                            text_content = (
                                f"{session_id_content}\n\n{thought}".strip()
                                if thought
                                else session_id_content
                            )
                            for sse in build_claude_tool_use_sse_events(
                                tool_calls_list,
                                message_id,
                                model,
                                text_content=text_content,
                            ):
                                yield sse
                        elif not tool_calls_list and not emitted_message_start:
                            # 解析后无 tool_calls，将 buffer 按纯文本发出
                            content_to_emit = strip_session_id_prefix(buffer)
                            yield msg_start
                            yield block_start
                            yield make_delta_sse(content_to_emit)
                            yield make_stop_sse()
                            emitted_message_start = True

                    # 流结束后：若已 emit 纯文本 delta，补 message_stop
                    if (
                        emitted_message_start
                        and emitted_content_block_start
                        and not emitted_tool_calls
                    ):
                        yield make_stop_sse()

                    # 流结束后：若从未 emit 任何内容（is_tool_call=None 且无 tool_calls），补齐纯文本
                    if (
                        use_react
                        and not emitted_message_start
                        and not emitted_tool_calls
                        and buffer
                    ):
                        content_to_emit = strip_session_id_prefix(buffer)
                        yield msg_start
                        yield block_start
                        yield make_delta_sse(content_to_emit)
                        yield make_stop_sse()
                    # 保存解析结果（已有 parsed、tool_calls_list）
                    content_for_save = strip_session_id_prefix(buffer)
                    parsed_save = parse_react_output(content_for_save)
                    tc_list = (
                        react_output_to_tool_calls(parsed_save) if parsed_save else []
                    )
                    if tc_list:
                        thought = ""
                        if "Thought" in content_for_save:
                            m = re.search(
                                r"Thought[:：]\s*(.+?)(?=\s*Action[:：]|$)",
                                content_for_save,
                                re.DOTALL | re.I,
                            )
                            thought = (m.group(1) or "").strip() if m else ""
                        content_blocks: list[dict[str, Any]] = []
                        if thought:
                            content_blocks.append({"type": "text", "text": thought})
                        for tc in tc_list:
                            args = tc.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args) if args else {}
                                except json.JSONDecodeError:
                                    args = {}
                            content_blocks.append(
                                {
                                    "type": "tool_use",
                                    "id": f"toolu_{uuid_mod.uuid4().hex[:16]}",
                                    "name": tc.get("name", ""),
                                    "input": args if isinstance(args, dict) else {},
                                }
                            )
                        _save_response_debug(
                            {
                                "id": chat_id,
                                "type": "message",
                                "role": "assistant",
                                "content": content_blocks,
                                "stop_reason": "tool_use",
                                "model": model,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            }
                        )
                    else:
                        _save_response_debug(
                            {
                                "id": chat_id,
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "text", "text": content_for_save}],
                                "stop_reason": "end_turn",
                                "model": model,
                                "usage": {"input_tokens": 0, "output_tokens": 0},
                            }
                        )
                except ValueError as e:
                    logger.warning("chat 请求参数错误: %s", e)
                    yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'invalid_request_error'}}, ensure_ascii=False)}\n\n"
                except Exception as e:
                    logger.exception("流式 chat 失败")
                    yield f"data: {json.dumps({'error': {'message': str(e), 'type': 'server_error'}}, ensure_ascii=False)}\n\n"

            return StreamingResponse(
                sse_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # 非流式：收集完整内容后解析，若有 tool_call 则返回 tool_calls 格式
        full: list[str] = []
        try:
            async for chunk in handler.stream_completion(type, req):
                full.append(chunk)
        except Exception as e:
            logger.exception("chat/completions 失败")
            raise HTTPException(status_code=500, detail=str(e)) from e

        reply = "".join(full)
        tool_calls_list: list[dict[str, Any]] = []
        if use_react:
            content_for_parse = strip_session_id_prefix(reply)
            parsed = parse_react_output(content_for_parse)
            tool_calls_list = react_output_to_tool_calls(parsed) if parsed else []
        if tool_calls_list:
            # Claude 格式：content 数组 [text, tool_use, ...]
            session_id_content = reply[:64] if len(reply) >= 64 else ""
            thought_ns = ""
            if "Thought" in content_for_parse:
                m = re.search(
                    r"Thought[:：]\s*(.+?)(?=\s*Action[:：]|$)",
                    content_for_parse,
                    re.DOTALL | re.I,
                )
                thought_ns = (m.group(1) or "").strip() if m else ""
            text_content = (
                f"{session_id_content}\n\n{thought_ns}".strip()
                if thought_ns
                else session_id_content
            )
            resp = build_tool_calls_response(
                tool_calls_list,
                chat_id,
                model,
                created,
                text_content=text_content,
            )
            # 保存解析结果（thought_ns 已在上面计算）
            content_blocks_ns: list[dict[str, Any]] = []
            if thought_ns:
                content_blocks_ns.append({"type": "text", "text": thought_ns})
            for tc in tool_calls_list:
                args = tc.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except json.JSONDecodeError:
                        args = {}
                content_blocks_ns.append(
                    {
                        "type": "tool_use",
                        "id": f"toolu_{uuid_mod.uuid4().hex[:16]}",
                        "name": tc.get("name", ""),
                        "input": args if isinstance(args, dict) else {},
                    }
                )
            _save_response_debug(
                {
                    "id": chat_id,
                    "type": "message",
                    "role": "assistant",
                    "content": content_blocks_ns,
                    "stop_reason": "tool_use",
                    "model": model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                }
            )
            return resp

        resp = {
            "id": chat_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }
            ],
        }
        # 保存解析结果（纯文本）
        _save_response_debug(
            {
                "id": chat_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": reply}],
                "stop_reason": "end_turn",
                "model": model,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
        )
        return resp

    return router
