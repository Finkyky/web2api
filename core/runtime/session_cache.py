"""
会话缓存：session_id 全局唯一，映射到 (proxy_key, type, account_id)。
通过 session_id 可向上查找到 page、context。
"""

from dataclasses import dataclass

from core.runtime.keys import ProxyKey


@dataclass
class SessionEntry:
    """单条会话：用于通过 session_id 反查 context/page 与账号。"""

    proxy_key: ProxyKey
    type_name: str
    account_id: str


class SessionCache:
    """进程内会话缓存，不持久化、不跨进程。"""

    def __init__(self) -> None:
        self._store: dict[str, SessionEntry] = {}

    def get(self, session_id: str) -> SessionEntry | None:
        return self._store.get(session_id)

    def put(
        self,
        session_id: str,
        proxy_key: ProxyKey,
        type_name: str,
        account_id: str,
    ) -> None:
        self._store[session_id] = SessionEntry(
            proxy_key=proxy_key,
            type_name=type_name,
            account_id=account_id,
        )

    def __contains__(self, session_id: str) -> bool:
        return session_id in self._store
