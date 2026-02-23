"""
Function Call 层：解析 Claude 输出的 <tool_call> 格式，转换为 OpenAI tool_calls；
将 tools 和 tool 结果拼入 prompt 给 Claude。
"""

import json
import re
import uuid
from collections.abc import Callable
from typing import Any

TOOL_CALL_PREFIX = "<tool_call>"
TOOL_CALL_PREFIX_LEN = len(TOOL_CALL_PREFIX)
TOOL_CALL_PATTERN = re.compile(
    r"<tool_call>\s*(.*?)\s*</tool_call>",
    re.DOTALL,
)


def parse_tool_calls(text: str) -> list[dict[str, Any]]:
    """
    从文本中解析所有 <tool_call>...</tool_call> 块。
    返回 [{"name": str, "arguments": dict | str}, ...]
    """
    if not text or not text.strip():
        return []
    matches = TOOL_CALL_PATTERN.findall(text)
    result: list[dict[str, Any]] = []
    for m in matches:
        try:
            obj = json.loads(m.strip())
            if isinstance(obj, dict) and "name" in obj:
                args = obj.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                result.append({"name": obj["name"], "arguments": args})
        except json.JSONDecodeError:
            pass
    return result


def detect_tool_call_mode(buffer: str, *, strip_session_id: bool = True) -> bool | None:
    """
    根据 buffer 内容判断是否为 tool_call 模式。
    None=尚未确定，True=tool_call，False=普通文本。
    strip_session_id: 若 True，先去掉开头的零宽 session_id 前缀再判断。
    """
    content = buffer
    if strip_session_id:
        from core.api.conv_parser import strip_session_id_prefix

        content = strip_session_id_prefix(buffer)
    stripped = content.lstrip()
    if stripped.startswith(TOOL_CALL_PREFIX):
        return True
    if len(stripped) > TOOL_CALL_PREFIX_LEN:
        return False
    return None


def format_tools_for_prompt(tools: list[dict[str, Any]]) -> str:
    """
    将 OpenAI 格式的 tools 转为可读文本，用于 prompt。
    兼容 OpenAI 格式 {type, function: {name, description, parameters}}
    和 Cursor 格式 {name, description, input_schema}。
    """
    if not tools:
        return ""
    lines: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if t.get("type") == "function" else t
        if not isinstance(fn, dict):
            fn = t
        name = fn.get("name")
        if not name:
            continue
        desc = fn.get("description") or fn.get("summary") or ""
        params = fn.get("parameters") or fn.get("input_schema") or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                params = {}
        props = params.get("properties") or {}
        required = params.get("required") or []
        args_desc = ", ".join(
            f"{k}: {v.get('type', 'any')}" + (" (必填)" if k in required else "")
            for k, v in props.items()
        )
        lines.append(
            f"- {name}({args_desc}): {desc[:200]}" + ("..." if len(desc) > 200 else "")
        )
    return "\n".join(lines) if lines else ""


def build_tool_calls_response(
    tool_calls_list: list[dict[str, Any]],
    chat_id: str,
    model: str,
    created: int,
    *,
    text_content: str = "",
) -> dict[str, Any]:
    """将解析出的 tool_calls 转为 Claude 格式的 chat.completion 响应。
    仅使用 content 数组（text + tool_use 块），无 tool_calls。
    """
    content_blocks: list[dict[str, Any]] = []
    if text_content:
        content_blocks.append({"type": "text", "text": text_content})
    for tc in tool_calls_list:
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        if isinstance(args, dict):
            args_obj = args
        else:
            try:
                args_obj = json.loads(str(args)) if args else {}
            except json.JSONDecodeError:
                args_obj = {}
        content_blocks.append(
            {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:16]}",
                "name": name,
                "input": args_obj,
            }
        )
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content_blocks,
                },
                "finish_reason": "tool_use",
            }
        ],
    }


def _sse_event(event_type: str, data: dict[str, Any]) -> str:
    """构建 Claude 原生 SSE 事件：event: <type>\\ndata: <json>\\n\\n"""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def build_claude_tool_use_sse_events(
    tool_calls_list: list[dict[str, Any]],
    message_id: str,
    model: str,
    *,
    text_content: str = "",
) -> list[str]:
    """构建 Claude 原生流式 SSE 事件（message_start, content_block_start/stop, message_delta, message_stop）。
    用于 tool_use 场景，Cursor 按 Claude 协议解析，id/name/input 完整保留。
    """
    events: list[str] = []
    events.append(
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
    )
    block_index = 0
    if text_content:
        events.append(
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": {"type": "text", "text": text_content},
                },
            )
        )
        events.append(
            _sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": block_index},
            )
        )
        block_index += 1
    for tc in tool_calls_list:
        name = tc.get("name", "")
        args = tc.get("arguments", {})
        if isinstance(args, dict):
            args_obj = args
        else:
            try:
                args_obj = json.loads(str(args)) if args else {}
            except json.JSONDecodeError:
                args_obj = {}
        tool_use_block = {
            "type": "tool_use",
            "id": f"toolu_{uuid.uuid4().hex[:16]}",
            "name": name,
            "input": args_obj,
        }
        events.append(
            _sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": block_index,
                    "content_block": tool_use_block,
                },
            )
        )
        events.append(
            _sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": block_index},
            )
        )
        block_index += 1
    events.append(
        _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            },
        )
    )
    events.append(_sse_event("message_stop", {"type": "message_stop"}))
    return events


def build_claude_text_sse_events(
    message_id: str,
    model: str,
) -> tuple[str, str, Callable[[str], str], Callable[[], str]]:
    """返回 Claude 流式事件的工厂。
    返回 (message_start_sse, content_block_start_sse, make_delta_sse, make_stop_sse)。
    """
    msg_start = _sse_event(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )
    block_start = _sse_event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
    )

    def make_delta_sse(text: str) -> str:
        return _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )

    def make_stop_sse() -> str:
        return (
            _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0})
            + _sse_event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 0},
                },
            )
            + _sse_event("message_stop", {"type": "message_stop"})
        )

    return msg_start, block_start, make_delta_sse, make_stop_sse
