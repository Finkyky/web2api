"""
会话 ID 的携带方式：零宽字符编码（不可见）或兼容旧版 <!-- conv_uuid=xxx -->。
支持 content 为 string 或 list[{ type, text }]。
"""

import re
from typing import Any

# 零宽字符集，用于将会话 ID 编码为不可见前缀（5 个字符表示 0-4，两两组合表示十六进制 0-15）
_ZERO_WIDTH = (
    "\u200b",  # 零宽空格
    "\u200c",  # 零宽非连接符
    "\u200d",  # 零宽连接符
    "\ufeff",  # 零宽非断空格
    "\u180e",  # 蒙古文元音分隔符
)
_ZW_TO_DIGIT = {c: str(i) for i, c in enumerate(_ZERO_WIDTH)}
_ZW_SET = frozenset(_ZERO_WIDTH)

CONV_UUID_PATTERN = re.compile(r"<!--\s*conv_uuid=([^\s>]+)\s*-->", re.IGNORECASE)


def _uuid_hex(session_id: str) -> str:
    """去掉 UUID 中的连字符，得到 32 位十六进制串。"""
    return session_id.replace("-", "").lower()


def encode_session_id_zero_width(session_id: str) -> str:
    """
    将会话 ID（UUID）编码为零宽字符序列，可直接拼在回复内容最前，视觉上不可见。
    每两位十六进制用两个零宽字符表示（基 5 编码）：hex_val -> (v//5, v%5)。
    """
    hex_str = _uuid_hex(session_id)
    if len(hex_str) != 32:
        return ""
    out: list[str] = []
    for c in hex_str:
        n = int(c, 16)
        out.append(_ZERO_WIDTH[n // 5])
        out.append(_ZERO_WIDTH[n % 5])
    return "".join(out)


def decode_session_id_zero_width(text: str) -> str | None:
    """
    从文本开头解析零宽字符编码的会话 ID，遇到非零宽字符即停止。
    若有效零宽字符数为 64（32 个十六进制×2），则解码为 UUID 并返回。
    """
    run: list[str] = []
    for c in text:
        if c not in _ZW_SET:
            break
        run.append(c)
    if len(run) < 64:
        return None
    run = run[:64]
    hex_parts: list[str] = []
    for i in range(0, 64, 2):
        a = _ZW_TO_DIGIT.get(run[i])
        b = _ZW_TO_DIGIT.get(run[i + 1])
        if a is None or b is None:
            return None
        val = int(a) * 5 + int(b)
        if val > 15:
            return None
        hex_parts.append(f"{val:x}")
    hex_str = "".join(hex_parts)
    return f"{hex_str[:8]}-{hex_str[8:12]}-{hex_str[12:16]}-{hex_str[16:20]}-{hex_str[20:32]}"


def _normalize_content(content: str | list[Any]) -> str:
    """将 content 转为单段字符串便于正则匹配。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text" and "text" in p:
            parts.append(str(p["text"]))
        elif isinstance(p, str):
            parts.append(p)
    return " ".join(parts)


def parse_conv_uuid_from_messages(messages: list[dict[str, Any]]) -> str | None:
    """
    从 messages 中解析会话 ID：优先从助手回复开头的零宽字符解码，否则匹配 <!-- conv_uuid=xxx -->。
    若未找到则返回 None。
    """
    for m in messages:
        content = m.get("content")
        if content is None:
            continue
        text = _normalize_content(content)
        decoded = decode_session_id_zero_width(text)
        if decoded is not None:
            return decoded
        match = CONV_UUID_PATTERN.search(text)
        if match:
            return match.group(1).strip()
    return None


def session_id_prefix(session_id: str) -> str:
    """返回响应中需加在内容最前面的不可见前缀（零宽字符编码的会话 ID）。"""
    encoded = encode_session_id_zero_width(session_id)
    return encoded if encoded else f"<!-- conv_uuid={session_id} -->"


def strip_session_id_prefix(text: str) -> str:
    """去掉开头的零宽字符编码的 session_id（64 个零宽字符），返回剩余正文。"""
    count = 0
    for c in text:
        if c in _ZW_SET:
            count += 1
            if count >= 64:
                return text[64:]
        else:
            break
    return text
