"""
Claude 插件：仅实现站点特有的 workspace 获取、会话创建、请求体构建、SSE 解析和限流处理。
其余编排逻辑（create_page / apply_auth / stream_completion 流程）全部由 BaseSitePlugin 完成。
调试时可在 config.yaml 的 claude.start_url、claude.api_base 指向 mock。
"""

import datetime
import json
import logging
import time
from typing import Any

from playwright.async_api import BrowserContext

from core.plugin.base import BaseSitePlugin, PluginRegistry, SiteConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 站点特有：请求体 & SSE 解析
# ---------------------------------------------------------------------------


def _default_completion_body(
    message: str, *, is_follow_up: bool = False
) -> dict[str, Any]:
    """构建 Claude completion 请求体。续写时不带 create_conversation_params，否则 API 返回 400。"""
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
    """解析单条 Claude SSE data 行，返回 (texts, message_id, error)。"""
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
        elif kind == "message_start":
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
                "message_delta",
                "message_limit",
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


def _is_terminal_sse_event(payload: str) -> bool:
    """Claude 正常流结束时会发送 message_stop。"""
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("type") == "message_stop"


# ---------------------------------------------------------------------------
# ClaudePlugin — 只需声明配置 + 实现 5 个 hook
# ---------------------------------------------------------------------------


class ClaudePlugin(BaseSitePlugin):
    """Claude Web2API 插件。auth 需含 sessionKey。"""

    type_name = "claude"

    site = SiteConfig(
        start_url="https://claude.ai",
        api_base="https://claude.ai/api",
        cookie_name="sessionKey",
        cookie_domain=".claude.ai",
        auth_keys=["sessionKey", "session_key"],
        config_section="claude",
    )

    def model_mapping(self) -> dict[str, str] | None:
        return {"s4": "claude-sonnet-4-5-20250929"}

    # ---- 5 个必须实现的 hook ----

    async def fetch_workspace(self, context: BrowserContext) -> dict[str, Any] | None:
        resp = await context.request.get(f"{self.api_base}/account", timeout=15000)
        if resp.status != 200:
            await resp.dispose()
            return None
        data = await resp.json()
        await resp.dispose()
        memberships = data.get("memberships") or []
        if not memberships:
            return None
        org = memberships[0].get("organization") or {}
        org_uuid = org.get("uuid")
        return {"org_uuid": org_uuid} if org_uuid else None

    async def create_session(
        self,
        context: BrowserContext,
        workspace: dict[str, Any],
    ) -> str | None:
        org_uuid = workspace["org_uuid"]
        url = f"{self.api_base}/organizations/{org_uuid}/chat_conversations"
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

    def build_completion_url(self, session_id: str, state: dict[str, Any]) -> str:
        org_uuid = state["workspace"]["org_uuid"]
        return f"{self.api_base}/organizations/{org_uuid}/chat_conversations/{session_id}/completion"

    def build_completion_body(
        self,
        message: str,
        session_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        parent = state.get("parent_message_uuid")
        body = _default_completion_body(message, is_follow_up=parent is not None)
        if parent:
            body["parent_message_uuid"] = parent
        return body

    def parse_sse_event(
        self,
        payload: str,
    ) -> tuple[list[str], str | None, str | None]:
        return _parse_one_sse_event(payload)

    def is_terminal_sse_event(self, payload: str) -> bool:
        return _is_terminal_sse_event(payload)

    # ---- 可选 hook 覆盖 ----

    def on_http_error(
        self,
        message: str,
        headers: dict[str, str] | None,
    ) -> int | None:
        if "429" not in message:
            return None
        if headers:
            reset = headers.get("anthropic-ratelimit-requests-reset") or headers.get(
                "Anthropic-Ratelimit-Requests-Reset"
            )
            if reset:
                try:
                    s = str(reset).strip()
                    if s.endswith("Z"):
                        s = s[:-1] + "+00:00"
                    dt = datetime.datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    return int(dt.timestamp())
                except Exception:
                    pass
        return int(time.time()) + 5 * 3600


def register_claude_plugin() -> None:
    """注册 Claude 插件到全局 Registry。"""
    PluginRegistry.register(ClaudePlugin())
