"""
插件抽象与注册表：type_name -> 插件实现。
基础架构按 path 的 type 查找插件并调用。
"""

import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, AsyncIterator

from playwright.async_api import BrowserContext, Page


def make_429_unfreeze_handler(
    default_unfreeze_seconds: int = 5 * 3600,
) -> Callable[[str], None]:
    """
    返回 on_http_error 回调：若 msg 含 429 则解析解冻时间戳并 raise AccountFrozenError，否则 raise RuntimeError。
    接入方直接 on_http_error=make_429_unfreeze_handler() 即可。
    """

    def handler(msg: str) -> None:
        if "429" in msg:
            m = re.search(r"429[:\s]+(\d+)", msg)
            unfreeze_at = (
                int(m.group(1)) if m else int(time.time()) + default_unfreeze_seconds
            )
            raise AccountFrozenError(msg, unfreeze_at)
        raise RuntimeError(msg)

    return handler


class AccountFrozenError(RuntimeError):
    """
    插件在检测到账号被限流/额度用尽时抛出，携带解冻时间戳（Unix 秒）。
    由 chat_handler 捕获后写入配置并重试其他账号；是否 429 或其它状态由各站点插件自行判断。
    """

    def __init__(self, message: str, unfreeze_at: int) -> None:
        super().__init__(message)
        self.unfreeze_at = unfreeze_at


class AbstractPlugin(ABC):
    """
    各 type（如 claude、kimi）需实现此接口并注册。
    429/账号封禁由插件自行处理。
    """

    @property
    @abstractmethod
    def type_name(self) -> str:
        """如 'claude', 'kimi'。"""
        ...

    async def create_page(self, context: BrowserContext) -> Page:
        """总是新建一个 page 并打开该 type 的入口 URL，用于 page 池初始化与补回。"""
        raise NotImplementedError

    async def apply_auth(
        self,
        context: BrowserContext,
        page: Page,
        auth: dict[str, Any],
        *,
        reload: bool = True,
        **kwargs: Any,
    ) -> None:
        """用账号的 auth JSON 写 cookie 等；reload=False 时仅写 cookie 不 reload，用于共享 page 时不打断其他请求。"""
        raise NotImplementedError

    async def create_conversation(
        self,
        context: BrowserContext,
        page: Page,
    ) -> str | None:
        """调用该 type 的 API 创建会话，返回会话 ID；失败返回 None。"""
        raise NotImplementedError

    async def stream_completion(
        self,
        context: BrowserContext,
        page: Page,
        session_id: str,
        message: str,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """在已有会话上发一条 message，逐块 yield 助手回复。"""
        raise NotImplementedError

    def parse_session_id(self, messages: list[dict[str, Any]]) -> str | None:
        """
        可选：从请求 messages 中解析会话 ID。
        若返回 None，基础架构使用默认规则（<!-- conv_uuid=xxx -->）。
        """
        return None


class PluginRegistry:
    """全局插件注册表：type_name -> AbstractPlugin。"""

    _plugins: dict[str, AbstractPlugin] = {}

    @classmethod
    def register(cls, plugin: AbstractPlugin) -> None:
        cls._plugins[plugin.type_name] = plugin

    @classmethod
    def get(cls, type_name: str) -> AbstractPlugin | None:
        return cls._plugins.get(type_name)

    @classmethod
    def all_types(cls) -> list[str]:
        return list(cls._plugins.keys())
