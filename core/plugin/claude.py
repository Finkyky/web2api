"""
Claude 插件：仅实现站点特有的 URL/请求体/SSE 单条解析与会话创建，其余复用 core.plugin.helpers / base。
调试时可通过环境变量 CLAUDE_START_URL、CLAUDE_API_BASE 指向 mock（如 http://127.0.0.1:8001/mock）。
"""

import json
import os
import logging
import re
from typing import Any, AsyncIterator

from playwright.async_api import BrowserContext, Page

from core.plugin.base import AbstractPlugin, make_429_unfreeze_handler
from core.plugin.helpers import (
    apply_cookie_auth,
    create_page_for_site,
    stream_completion_via_sse,
)

logger = logging.getLogger(__name__)

CLAUDE_API_BASE = "https://claude.ai/api"
SESSION_COOKIE_NAME = "sessionKey"
CLAUDE_ORIGIN = "claude.ai"
CLAUDE_START_URL = "https://claude.ai"


def _get_claude_urls() -> tuple[str, str]:
    """支持环境变量覆盖，便于指向 mock 调试。"""
    start = os.environ.get("CLAUDE_START_URL", CLAUDE_START_URL)
    api_base = os.environ.get("CLAUDE_API_BASE", CLAUDE_API_BASE)
    return start, api_base


# API 要求 parent_message_uuid 为标准 UUID，仅当 SSE 解析出的值符合此格式才写入
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _default_completion_body(
    message: str, *, is_follow_up: bool = False
) -> dict[str, Any]:
    """构建 completion 请求体。续写（is_follow_up=True）时不得带 create_conversation_params，否则 API 返回 400。"""
    body: dict[str, Any] = {
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
    }
    if not is_follow_up:
        body["create_conversation_params"] = {
            "name": "",
            "include_conversation_preferences": True,
            "is_temporary": False,
        }
    return body


def _parse_one_sse_event(payload: str) -> tuple[list[str], str | None, str | None]:
    """解析单条 SSE data 行，返回 (texts, message_id, error)。"""
    result: list[str] = []
    message_id: str | None = None
    error_message: str | None = None
    try:
        obj = json.loads(payload)
        if not isinstance(obj, dict):
            return (result, message_id, error_message)
        kind = obj.get("type")
        if kind == "error":
            err = obj.get("error") or {}
            error_message = err.get("message") or err.get("type") or "Unknown error"
            return (result, message_id, error_message)
        if "text" in obj and obj.get("text"):
            result.append(str(obj["text"]))
        elif kind == "content_block_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict) and "text" in delta:
                result.append(str(delta["text"]))
            elif isinstance(delta, str) and delta:
                result.append(delta)
        elif kind == "completion" and "completion" in obj:
            result.append(str(obj["completion"]))
        elif kind == "message_delta":
            delta = obj.get("delta")
            if isinstance(delta, dict) and delta.get("text"):
                result.append(str(delta["text"]))
        elif kind == "message_start":
            # API 要求 parent_message_uuid 为标准 UUID，优先取 uuid 再取 id（chatcompl_*）
            msg = obj.get("message")
            if isinstance(msg, dict):
                for key in ("uuid", "id"):
                    if msg.get(key):
                        message_id = str(msg[key])
                        break
            if not message_id:
                mid = (
                    obj.get("message_uuid") or obj.get("uuid") or obj.get("message_id")
                )
                if mid:
                    message_id = str(mid)
        elif (
            kind
            and kind
            not in (
                "ping",
                "content_block_start",
                "content_block_stop",
                "message_stop",
            )
            and not result
        ):
            logger.debug(
                "SSE 未解析出正文 type=%s payload=%s",
                kind,
                payload[:200] if len(payload) > 200 else payload,
            )
    except json.JSONDecodeError:
        pass
    return (result, message_id, error_message)


async def _get_org_uuid(context: BrowserContext) -> str | None:
    _, api_base = _get_claude_urls()
    resp = await context.request.get(f"{api_base}/account", timeout=15000)
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


async def _post_create_conversation(
    context: BrowserContext, org_uuid: str
) -> str | None:
    _, api_base = _get_claude_urls()
    url = f"{api_base}/organizations/{org_uuid}/chat_conversations"
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


class ClaudePlugin(AbstractPlugin):
    """Claude Web2API 插件。auth 需含 sessionKey。"""

    def __init__(self) -> None:
        self._session_state: dict[str, dict[str, Any]] = {}

    @property
    def type_name(self) -> str:
        return "claude"

    async def create_page(self, context: BrowserContext) -> Page:
        start_url, _ = _get_claude_urls()
        return await create_page_for_site(context, start_url)

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
        **kwargs: Any,
    ) -> None:
        await apply_cookie_auth(
            context,
            page,
            auth,
            SESSION_COOKIE_NAME,
            ["sessionKey", "session_key"],
            ".claude.ai",
            reload=reload,
        )

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
    ) -> str | None:
        org_uuid = await _get_org_uuid(context)
        if not org_uuid:
            logger.warning("无法获取 org_uuid，请确认已登录 claude.ai")
            return None
        logger.info("[claude] create_conversation org_uuid=%s", org_uuid)
        conv_uuid = await _post_create_conversation(context, org_uuid)
        if not conv_uuid:
            return None
        self._session_state[conv_uuid] = {
            "org_uuid": org_uuid,
            "parent_message_uuid": None,
        }
        logger.info(
            "[claude] create_conversation done conv_uuid=%s _session_state.keys=%s",
            conv_uuid,
            list(self._session_state.keys()),
        )
        return conv_uuid

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        conv_uuid = session_id
        state = self._session_state.get(conv_uuid)
        if not state:
            logger.error(
                "[claude] stream_completion 未知会话 conv_uuid=%s _session_state.keys=%s",
                conv_uuid,
                list(self._session_state.keys()),
            )
            raise RuntimeError(f"未知会话 ID: {conv_uuid}")
        org_uuid = state["org_uuid"]
        parent_message_uuid = state.get("parent_message_uuid")

        _, api_base = _get_claude_urls()
        url = f"{api_base}/organizations/{org_uuid}/chat_conversations/{conv_uuid}/completion"
        is_follow_up = parent_message_uuid is not None
        body = _default_completion_body(message, is_follow_up=is_follow_up)
        if parent_message_uuid:
            body["parent_message_uuid"] = parent_message_uuid
        body_json = json.dumps(body)
        start_url, _ = _get_claude_urls()
        chat_page_url = f"{start_url.rstrip('/')}/chat/{conv_uuid}"
        logger.info(
            "[claude] stream_completion conv_uuid=%s org_uuid=%s parent_message_uuid=%s url=%s",
            conv_uuid,
            org_uuid,
            parent_message_uuid,
            url,
        )
        out_message_id: list[str] = []
        request_id: str = kwargs.get("request_id", "")

        async for text in stream_completion_via_sse(
            context,
            page,
            url,
            body_json,
            _parse_one_sse_event,
            request_id,
            chat_page_url=chat_page_url,
            on_http_error=make_429_unfreeze_handler(),
            collect_message_id=out_message_id,
        ):
            yield text

        if out_message_id and conv_uuid in self._session_state:
            # 只保留符合 UUID 格式的 id，API 拒收 chatcompl_* 等格式
            last_uuid = next(
                (m for m in reversed(out_message_id) if _UUID_RE.match(m)), None
            )
            if last_uuid:
                self._session_state[conv_uuid]["parent_message_uuid"] = last_uuid
                logger.info(
                    "[claude] stream_completion updated parent_message_uuid=%s",
                    last_uuid,
                )


def register_claude_plugin() -> None:
    """注册 Claude 插件到全局 Registry。"""
    from core.plugin.base import PluginRegistry

    PluginRegistry.register(ClaudePlugin())
