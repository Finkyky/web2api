"""OpenAI 兼容的请求/响应模型。"""

import json
from typing import Any

from pydantic import BaseModel, Field


class OpenAIContentPart(BaseModel):
    type: str
    text: str | None = None


class OpenAIMessage(BaseModel):
    role: str = Field(..., description="system | user | assistant | tool")
    content: str | list[OpenAIContentPart] | None = ""
    tool_calls: list[dict[str, Any]] | None = Field(
        default=None, description="assistant 发起的工具调用"
    )
    tool_call_id: str | None = Field(
        default=None, description="tool 消息对应的 call id"
    )

    model_config = {"extra": "allow"}


class OpenAIChatRequest(BaseModel):
    model: str = Field(default="", description="模型名，可忽略")
    messages: list[OpenAIMessage] = Field(..., description="对话列表")
    stream: bool = Field(default=False, description="是否流式返回")
    tools: list[dict] | None = Field(default=None, description="工具列表")
    tool_choice: str | dict | None = Field(default=None, description="工具选择策略")


def _norm_content(
    c: str | list[OpenAIContentPart] | None, *, skip_tool_result: bool = False
) -> str:
    """将 content 转为单段字符串。支持 text、content（Claude tool_result 用 content）。
    skip_tool_result: 若 True，跳过 type=tool_result 的块（由 extract_user_content 单独处理）。
    """
    if c is None:
        return ""
    if isinstance(c, str):
        return c
    if not isinstance(c, list):
        return ""
    return " ".join(
        p.text or "" for p in c if isinstance(p, OpenAIContentPart) and p.text
    )


REACT_STRICT_SUFFIX = (
    "(严格 ReAct 执行模式;禁止输出「无法执行工具所以直接给方案」等解释或替代内容)"
)


def extract_user_content(
    messages: list[OpenAIMessage],
    *,
    tools_text: str = "",
    use_react: bool = False,
    react_prompt_prefix: str = "",
) -> str:
    """
    从 messages 中提取完整对话，拼成发给 Claude 的 prompt。
    支持 user、assistant、tool 角色；assistant 的 tool_calls 与 tool 结果会拼回。
    ReAct 模式：完整 ReAct Prompt 仅第一次对话传入；后续对话只传对话内容 + 用户问题后的严格模式后缀。
    """
    parts: list[str] = []

    is_first_turn = not any(m.role in ("assistant", "tool") for m in messages)

    if use_react and react_prompt_prefix and is_first_turn:
        parts.append(react_prompt_prefix)
    elif not use_react and tools_text:
        parts.append(
            "你是一个助手，可以调用以下工具完成任务。调用时请严格按格式输出：\n"
            '<tool_call>{"name":"工具名","arguments":{参数对象}}</tool_call>\n'
            "只在实际调用工具时使用该格式，不要在其他解释中输出。\n\n可用工具：\n"
            + tools_text
            + "\n"
        )

    for m in messages:
        if m.role == "user":
            # Claude 格式：content 中 type=tool_result 的块作为工具结果
            if isinstance(m.content, list):
                for p in m.content:
                    is_tool_result = (
                        isinstance(p, dict) and p.get("type") == "tool_result"
                    ) or (
                        hasattr(p, "type") and getattr(p, "type", None) == "tool_result"
                    )
                    if is_tool_result:
                        txt = (
                            (p.get("content") or p.get("text"))
                            if isinstance(p, dict)
                            else (
                                getattr(p, "content", None) or getattr(p, "text", None)
                            )
                        )
                        if txt:
                            if use_react:
                                parts.append(
                                    f"**Observation**: {txt}\n\n请根据以上观察结果继续。如需调用工具，输出 Thought / Action / Action Input；若任务已完成，输出 Final Answer。"
                                )
                            else:
                                parts.append(f"工具结果：{txt}")
                            continue
                    elif (
                        hasattr(p, "type") and getattr(p, "type", None) == "tool_result"
                    ):
                        txt = getattr(p, "content", None) or getattr(p, "text", None)
                        if txt:
                            if use_react:
                                parts.append(
                                    f"**Observation**: {txt}\n\n请根据以上观察结果继续。如需调用工具，输出 Thought / Action / Action Input；若任务已完成，输出 Final Answer。"
                                )
                            else:
                                parts.append(f"工具结果：{txt}")
                            continue
            txt = _norm_content(m.content)
            if txt:
                if use_react:
                    parts.append(f"**User**: {txt} {REACT_STRICT_SUFFIX}")
                else:
                    parts.append(f"用户：{txt}")
        elif m.role == "assistant":
            tool_calls_list = list(m.tool_calls or [])
            # 兼容 Claude 格式：content 数组中 type=tool_use 的块
            if isinstance(m.content, list):
                for p in m.content:
                    if isinstance(p, dict) and p.get("type") == "tool_use":
                        name = p.get("name", "")
                        inp = p.get("input")
                        args = (
                            json.dumps(inp, ensure_ascii=False)
                            if isinstance(inp, dict)
                            else str(inp or "{}")
                        )
                        tool_calls_list.append(
                            {"function": {"name": name, "arguments": args}}
                        )
                    elif hasattr(p, "type") and getattr(p, "type", None) == "tool_use":
                        name = getattr(p, "name", "") or ""
                        inp = getattr(p, "input", None)
                        args = (
                            json.dumps(inp, ensure_ascii=False)
                            if isinstance(inp, dict)
                            else str(inp or "{}")
                        )
                        tool_calls_list.append(
                            {"function": {"name": name, "arguments": args}}
                        )
            if tool_calls_list:
                for tc in tool_calls_list:
                    fn = tc.get("function") or {}
                    name = fn.get("name", "")
                    args = fn.get("arguments", "{}")
                    if use_react:
                        parts.append(
                            f"**Assistant**:\n\n```\nAction: {name}\nAction Input: {args}\n```"
                        )
                    else:
                        parts.append(f"助手调用工具：{name}({args})")
            else:
                txt = _norm_content(m.content)
                if txt:
                    if use_react:
                        parts.append(f"**Assistant**:\n\n{txt}")
                    else:
                        parts.append(f"助手：{txt}")
        elif m.role == "tool":
            txt = _norm_content(m.content)
            if use_react:
                parts.append(
                    f"**Observation**: {txt}\n\n请根据以上观察结果继续。如需调用工具，输出 Thought / Action / Action Input；若任务已完成，输出 Final Answer。"
                )
            else:
                parts.append(f"工具结果：{txt}")

    if use_react:
        parts.append("请你100%严格执行")
    else:
        parts.append("请继续完成任务。")
    return "\n".join(parts)
