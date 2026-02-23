"""
聊天请求编排：解析 conv_uuid、查缓存/拉账号、确保 browser/page、调用插件流式补全，
并在响应最前加上零宽字符编码的会话 ID（不可见）。
"""

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator, cast

from core.account.pool import AccountPool
from core.config.repository import ConfigRepository
from core.plugin.base import AccountFrozenError, PluginRegistry
from core.runtime.browser_manager import BrowserManager
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache

from core.api.conv_parser import parse_conv_uuid_from_messages, session_id_prefix
from core.api.function_call import format_tools_for_prompt
from core.api.react import format_react_prompt
from core.api.schemas import OpenAIChatRequest, extract_user_content

logger = logging.getLogger(__name__)


def _request_messages_as_dicts(req: OpenAIChatRequest) -> list[dict[str, Any]]:
    """转为 conv_parser 需要的 list[dict]。"""
    out: list[dict[str, Any]] = []
    for m in req.messages:
        d: dict[str, Any] = {"role": m.role}
        if isinstance(m.content, list):
            d["content"] = [p.model_dump() for p in m.content]
        else:
            d["content"] = m.content
        out.append(d)
    return out


class ChatHandler:
    """编排一次 chat 请求：会话解析、资源获取、插件调用。"""

    def __init__(
        self,
        pool: AccountPool,
        session_cache: SessionCache,
        browser_manager: BrowserManager,
        config_repo: ConfigRepository | None = None,
    ) -> None:
        self._pool = pool
        self._session_cache = session_cache
        self._browser_manager = browser_manager
        self._config_repo = config_repo
        # 每个 type 当前占用的 proxy_key；仅当切到下一 IP 组时 release 旧的
        self._last_proxy_key_per_type: dict[str, ProxyKey] = {}

    def report_account_unfreeze(
        self,
        fingerprint_id: str,
        account_name: str,
        unfreeze_at: int,
    ) -> None:
        """记录账号解冻时间并重载池，使后续 acquire 按当前时间与解冻时间判断可用性。"""
        if self._config_repo is None:
            return
        self._config_repo.update_account_unfreeze_at(
            fingerprint_id, account_name, unfreeze_at
        )
        groups = self._config_repo.load_groups()
        self._pool.reload(groups)

    async def stream_completion(
        self,
        type_name: str,
        req: OpenAIChatRequest,
    ) -> AsyncIterator[str]:
        """
        流式返回助手回复；首块为零宽编码的会话 ID，随后为正文。
        """
        plugin = PluginRegistry.get(type_name)
        if plugin is None:
            raise ValueError(f"未注册的 type: {type_name}")

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
            raise ValueError("messages 中需至少有一条带 content 的 user 消息")

        debug_path = (
            Path(__file__).resolve().parent.parent.parent / "chat_promt_debug.json"
        )
        debug_path.write_text(
            json.dumps({"prompt": content}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        raw_messages = _request_messages_as_dicts(req)
        conv_uuid = parse_conv_uuid_from_messages(raw_messages)
        if plugin.parse_session_id(raw_messages) is not None:
            conv_uuid = plugin.parse_session_id(raw_messages) or conv_uuid
        logger.info(
            "[chat] type=%s parsed conv_uuid=%s from_messages=%s",
            type_name,
            conv_uuid,
            "yes" if conv_uuid else "no",
        )

        max_retries = 3
        skip_session_cache = False
        for attempt in range(max_retries):
            session_id = None
            proxy_key = None
            proxy_pass = ""
            account = None
            group = None
            page = None

            in_cache = (
                not skip_session_cache
                and conv_uuid is not None
                and conv_uuid in self._session_cache
            )
            logger.info(
                "[chat] attempt=%s conv_uuid=%s in_cache=%s",
                attempt + 1,
                conv_uuid,
                in_cache,
            )

            if in_cache and conv_uuid is not None:
                entry = self._session_cache.get(conv_uuid)
                if entry and entry.type_name == type_name:
                    session_id = conv_uuid
                    proxy_key = entry.proxy_key
                    pair = self._pool.get_account_by_id(entry.account_id)
                    if pair:
                        group, account = pair
                        proxy_pass = group.proxy_pass
                        self._last_proxy_key_per_type[type_name] = proxy_key
                        logger.info(
                            "[chat] session from cache conv_uuid=%s account_id=%s proxy=%s",
                            conv_uuid,
                            entry.account_id,
                            getattr(proxy_key, "fingerprint_id", proxy_key),
                        )
                    else:
                        logger.warning(
                            "[chat] cache hit but account missing conv_uuid=%s account_id=%s",
                            conv_uuid,
                            entry.account_id,
                        )
                else:
                    logger.info(
                        "[chat] cache miss or type mismatch conv_uuid=%s entry=%s",
                        conv_uuid,
                        entry,
                    )
            if (
                session_id is None
                or proxy_key is None
                or account is None
                or group is None
            ):
                group, account, proxy_key = None, None, None
                for pk in self._browser_manager.current_proxy_keys():
                    g = self._pool.get_group_by_proxy_key(pk)
                    if g is None:
                        continue
                    pair = self._pool.acquire_from_group(g, type_name)
                    if pair is not None:
                        group, account = pair
                        proxy_key = pk
                        proxy_pass = g.proxy_pass
                        break
                if group is None or account is None or proxy_key is None:
                    group, account = self._pool.acquire(type_name)
                    proxy_key = ProxyKey(
                        group.proxy_host,
                        group.proxy_user,
                        group.fingerprint_id,
                    )
                    proxy_pass = group.proxy_pass
                old_pk = self._last_proxy_key_per_type.get(type_name)
                if old_pk is not None and old_pk != proxy_key:
                    await self._browser_manager.release_async(old_pk, type_name)
                self._last_proxy_key_per_type[type_name] = proxy_key
                if session_id is None:
                    logger.info(
                        "[chat] acquired new account type=%s account_id=%s proxy=%s",
                        type_name,
                        self._pool.account_id(group, account)
                        if group and account
                        else None,
                        getattr(proxy_key, "fingerprint_id", proxy_key),
                    )

            context = await self._browser_manager.ensure_browser(proxy_key, proxy_pass)
            page, request_id, _ = await self._browser_manager.acquire_page_slot(
                proxy_key, context, type_name, plugin.create_page
            )
            logger.info(
                "[chat] acquired page slot type=%s page.url=%s request_id=%s",
                type_name,
                page.url if page else None,
                request_id[:8],
            )
            try:
                account_id = self._pool.account_id(group, account)
                # 启动时主 page 已登录，context 已有 cookie，请求直接 fetch 即可

                if session_id is None:
                    logger.info("[chat] create_conversation type=%s", type_name)
                    new_sid = await plugin.create_conversation(context, page)
                    if not new_sid:
                        raise RuntimeError("插件创建会话失败")
                    session_id = new_sid
                    self._session_cache.put(
                        session_id,
                        proxy_key,
                        type_name,
                        account_id,
                    )
                    logger.info(
                        "[chat] session_cache.put session_id=%s account_id=%s",
                        session_id,
                        account_id,
                    )

                logger.info(
                    "[chat] stream_completion session_id=%s content_len=%s",
                    session_id,
                    len(content),
                )
                yield session_id_prefix(session_id)
                stream = cast(
                    AsyncIterator[str],
                    plugin.stream_completion(
                        context, page, session_id, content, request_id=request_id
                    ),
                )
                async for chunk in stream:
                    yield chunk
                return
            except AccountFrozenError as e:
                logger.warning(
                    "账号限流/额度用尽（插件上报），记录解冻时间并切账号重试: %s", e
                )
                if self._config_repo and group and account:
                    self.report_account_unfreeze(
                        group.fingerprint_id, account.name, e.unfreeze_at
                    )
                skip_session_cache = True
                if attempt == max_retries - 1:
                    raise RuntimeError(
                        f"已重试 {max_retries} 次仍限流/过载，请稍后再试: {e}"
                    ) from e
                continue
            except RuntimeError:
                raise
            finally:
                if page is not None:
                    logger.info(
                        "[chat] release_page_slot type=%s proxy=%s",
                        type_name,
                        getattr(proxy_key, "fingerprint_id", proxy_key),
                    )
                    await self._browser_manager.release_page_slot(
                        proxy_key, type_name, page
                    )


async def stream_completion_http(
    type_name: str,
    req: OpenAIChatRequest,
    handler: ChatHandler,
) -> AsyncIterator[str]:
    """供 HTTP 层调用的流式生成器；首块为注释，后续为正文。"""
    async for chunk in handler.stream_completion(type_name, req):
        yield chunk
