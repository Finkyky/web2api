"""
账号池：从配置加载代理组与账号，按 type 轮询 acquire。
支持按 proxy_key 查 group、从指定 group 取该 type 账号，供「先查现役浏览器再开新」使用。
"""

from typing import Iterator

from core.config.schema import AccountConfig, ProxyGroupConfig
from core.runtime.keys import ProxyKey


class AccountPool:
    """
    多 IP / 多账号池，按 type 过滤后轮询。
    acquire(type) 返回 (ProxyGroupConfig, AccountConfig)。
    get_group_by_proxy_key / acquire_from_group 供现役浏览器复用时使用。
    """

    def __init__(self, groups: list[ProxyGroupConfig]) -> None:
        self._groups = list(groups)
        self._indices: dict[str, int] = {}  # type -> 全局轮询下标
        self._group_type_indices: dict[
            tuple[str, str], int
        ] = {}  # (fingerprint_id, type) -> 组内轮询下标

    @classmethod
    def from_groups(cls, groups: list[ProxyGroupConfig]) -> "AccountPool":
        return cls(groups)

    def reload(self, groups: list[ProxyGroupConfig]) -> None:
        """用新加载的配置替换当前组（如更新解冻时间后从 repository 重新 load_groups）。"""
        self._groups = list(groups)

    def _accounts_by_type(
        self, type_name: str
    ) -> Iterator[tuple[ProxyGroupConfig, AccountConfig]]:
        """按 type 遍历所有 (group, account)，仅包含当前可用的账号（解冻时间已过或未设置）。"""
        for g in self._groups:
            for a in g.accounts:
                if a.type == type_name and a.is_available():
                    yield g, a

    def acquire(self, type_name: str) -> tuple[ProxyGroupConfig, AccountConfig]:
        """
        按 type 轮询获取一组 (ProxyGroupConfig, AccountConfig)。
        若该 type 无账号则抛出 ValueError。
        """
        pairs = list(self._accounts_by_type(type_name))
        if not pairs:
            raise ValueError(f"没有类别为 {type_name!r} 的账号，请先在配置中添加")
        n = len(pairs)
        idx = self._indices.get(type_name, 0) % n
        self._indices[type_name] = (idx + 1) % n
        return pairs[idx]

    def account_id(self, group: ProxyGroupConfig, account: AccountConfig) -> str:
        """生成账号唯一标识，用于会话缓存等。"""
        return f"{group.fingerprint_id}:{account.name}"

    def get_account_by_id(
        self, account_id: str
    ) -> tuple[ProxyGroupConfig, AccountConfig] | None:
        """根据 account_id（fingerprint_id:name）反查 (group, account)，用于复用会话时取 auth。"""
        for g in self._groups:
            for a in g.accounts:
                if self.account_id(g, a) == account_id:
                    return g, a
        return None

    def get_group_by_proxy_key(self, proxy_key: ProxyKey) -> ProxyGroupConfig | None:
        """根据 proxy_key（proxy_host, proxy_user, fingerprint_id）反查对应代理组。"""
        for g in self._groups:
            if (
                g.proxy_host == proxy_key.proxy_host
                and g.proxy_user == proxy_key.proxy_user
                and g.fingerprint_id == proxy_key.fingerprint_id
            ):
                return g
        return None

    def acquire_from_group(
        self,
        group: ProxyGroupConfig,
        type_name: str,
    ) -> tuple[ProxyGroupConfig, AccountConfig] | None:
        """
        从指定 group 内按 type 轮询取一个账号；若无该 type 则返回 None。
        供「现役浏览器对应 IP 组是否还有该 type 可用」时使用。
        """
        pairs = [(g, a) for g, a in self._accounts_by_type(type_name) if g is group]
        if not pairs:
            return None
        n = len(pairs)
        key = (group.fingerprint_id, type_name)
        idx = self._group_type_indices.get(key, 0) % n
        self._group_type_indices[key] = (idx + 1) % n
        return pairs[idx]
