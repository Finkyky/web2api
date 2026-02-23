import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple


WINDOW_HOURS_DEFAULT = 5


@dataclass
class ChatAccountState:
    """
    单个 2pai 账号的配额状态。

    - name: 账号显示名
    - session_key: claude.ai 的 session cookie 值
    - profile_id: 浏览器 profile 目录名，用于 user-data-dir
    - window: 收到 429 后封禁时长（Claude 免费额度约 5 小时窗口）

    额度用尽时 Claude API 返回 429，此时标记 blocked_until 并切换其他账号。
    """

    name: str
    session_key: str
    profile_id: str  # 浏览器 profile，未配置时与 name 一致
    window: datetime.timedelta

    blocked_until: Optional[datetime.datetime] = None  # 解封时间（收到 429 时设置）

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def is_usable(self, now: Optional[datetime.datetime] = None) -> bool:
        """当前账号是否可用（未被封禁）。"""
        if now is None:
            now = self._now()
        if self.blocked_until is not None and now < self.blocked_until:
            return False
        return True

    def block(self, now: Optional[datetime.datetime] = None) -> None:
        """收到 429 时调用，封禁账号直到 window 结束。"""
        if now is None:
            now = self._now()
        self.blocked_until = now + self.window


@dataclass
class ProxyGroup:
    """
    一个代理 IP + 若干 2pai 账号。

    - proxy_host: 例如 "sg.arxlabs.io:3010"
    - proxy_user / proxy_pass: 写进 chrome 插件的用户名、密码
    """

    proxy_host: str
    proxy_user: str
    proxy_pass: str
    fingerprint_id: str
    accounts: List[ChatAccountState] = field(default_factory=list)
    current_index: int = 0

    def find_next_usable_account(
        self, now: datetime.datetime
    ) -> Optional[ChatAccountState]:
        """在本 IP 下轮询找下一个可用账号。"""
        n = len(self.accounts)
        if n == 0:
            return None
        for offset in range(n):
            idx = (self.current_index + offset) % n
            acc = self.accounts[idx]
            if acc.is_usable(now):
                self.current_index = (idx + 1) % n
                return acc
        return None


class AccountPool:
    """
    多 IP / 多账号轮换池。

    典型配置结构（Python 中）：

    ACCOUNT_CONFIG = [
        {
            "proxy_host": "ip1:port",
            "proxy_user": "user1",
            "proxy_pass": "pass1",
            "fingerprint_id": "4567",
            "timezone": "America/Chicago",
            "accounts": [
                {"name": "claude01", "session_key": "..."},
                {"name": "claude02", "session_key": "...", "profile_id": "acc02"},
            ],
        },
        ...
    ]
    """

    def __init__(
        self,
        proxy_groups: List[ProxyGroup],
        window: Optional[datetime.timedelta] = None,
    ) -> None:
        if not proxy_groups:
            raise ValueError("proxy_groups 不能为空")
        self._proxy_groups = proxy_groups
        self._window = window or datetime.timedelta(hours=WINDOW_HOURS_DEFAULT)
        self._current_group_index = 0

    @classmethod
    def from_config(
        cls,
        config: List[Dict[str, Any]],
        *,
        window_hours: int = WINDOW_HOURS_DEFAULT,
    ) -> "AccountPool":
        window = datetime.timedelta(hours=window_hours)
        groups: List[ProxyGroup] = []
        for group_cfg in config:
            proxy_host = str(group_cfg["proxy_host"])
            proxy_user = str(group_cfg["proxy_user"])
            proxy_pass = str(group_cfg["proxy_pass"])
            fingerprint_id = str(group_cfg["fingerprint_id"])
            acc_cfgs = group_cfg.get("accounts", [])
            accounts: List[ChatAccountState] = []
            for a in acc_cfgs:
                name = str(a["name"])
                session_key = str(a["session_key"])
                profile_id = str(a.get("profile_id") or name)
                accounts.append(
                    ChatAccountState(
                        name=name,
                        session_key=session_key,
                        profile_id=profile_id,
                        window=window,
                    )
                )
            groups.append(
                ProxyGroup(
                    proxy_host=proxy_host,
                    proxy_user=proxy_user,
                    proxy_pass=proxy_pass,
                    fingerprint_id=fingerprint_id,
                    accounts=accounts,
                )
            )
        return cls(groups, window)

    def _now(self) -> datetime.datetime:
        return datetime.datetime.now(datetime.timezone.utc)

    def acquire(self) -> Tuple[ProxyGroup, ChatAccountState]:
        """
        获取一个可用的 (ProxyGroup, ChatAccountState) 组合。

        策略：轮询 IP 和账号，跳过 blocked_until 未到的账号。
        收到 429 时由调用方调用 block_account 封禁该账号。
        """
        now = self._now()
        n_groups = len(self._proxy_groups)
        for g_offset in range(n_groups):
            g_idx = (self._current_group_index + g_offset) % n_groups
            group = self._proxy_groups[g_idx]
            account = group.find_next_usable_account(now)
            if account is not None:
                self._current_group_index = g_idx
                return group, account
        raise RuntimeError("当前没有可用账号（全部处于封禁期，请等待解封或添加新账号）")

    def block_account(self, account: ChatAccountState) -> None:
        """收到 429 时调用，封禁该账号直到 window 结束。"""
        account.block()

    def snapshot_states(self) -> List[Dict[str, Any]]:
        """返回所有 IP + 账号状态快照，方便调试或监控。"""
        now = self._now()
        result: List[Dict[str, Any]] = []
        for group in self._proxy_groups:
            for acc in group.accounts:
                result.append(
                    {
                        "proxy_host": group.proxy_host,
                        "proxy_user": group.proxy_user,
                        "account": acc.name,
                        "profile_id": acc.profile_id,
                        "used_in_window": 0,  # 保留字段以兼容 account_state 表
                        "window_started_at": None,
                        "blocked_until": acc.blocked_until,
                        "is_usable": acc.is_usable(now),
                    }
                )
        return result

    def restore_from_snapshots(self, snapshots: Iterable[Dict[str, Any]]) -> None:
        """
        根据快照列表恢复 blocked_until。
        key: (proxy_host, proxy_user, account)
        """
        index: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for row in snapshots:
            proxy_host = str(row.get("proxy_host", ""))
            proxy_user = str(row.get("proxy_user", ""))
            account_name = str(row.get("account", ""))
            if not (proxy_host and proxy_user and account_name):
                continue
            index[(proxy_host, proxy_user, account_name)] = row

        for group in self._proxy_groups:
            for acc in group.accounts:
                key = (group.proxy_host, group.proxy_user, acc.name)
                row = index.get(key)
                if row is None:
                    continue
                bu = row.get("blocked_until")
                if isinstance(bu, datetime.datetime):
                    acc.blocked_until = bu


__all__ = ["ChatAccountState", "ProxyGroup", "AccountPool"]
