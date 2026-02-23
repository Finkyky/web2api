"""
账号池配置的 SQLite 持久化。

数据格式与 multi_web2api 中 ACCOUNT_CONFIG 一致：
[
    {
        "proxy_host": "sg.arxlabs.io:3010",
        "proxy_user": "...",
        "proxy_pass": "...",
        "fingerprint_id": "4567",
        "timezone": "America/Chicago",
        "accounts": [
            {"name": "claude-01", "session_key": "..."},
            {"name": "claude-02", "session_key": "...", "profile_id": "acc02"},
        ],
    }
]
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

DB_FILENAME = "account_pool.sqlite3"


def get_db_path() -> Path:
    return Path(__file__).resolve().parent / DB_FILENAME


def _get_conn() -> sqlite3.Connection:
    p = get_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(p)


def init_config_tables(conn: sqlite3.Connection) -> None:
    """创建配置相关表（proxy_group、account）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS proxy_group (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_host TEXT NOT NULL,
            proxy_user TEXT NOT NULL,
            proxy_pass TEXT NOT NULL,
            fingerprint_id TEXT NOT NULL DEFAULT '',
            timezone TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS account (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            session_key TEXT NOT NULL,
            max_requests INTEGER NOT NULL DEFAULT 40,
            profile_id TEXT,
            FOREIGN KEY (proxy_group_id) REFERENCES proxy_group(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_account_proxy_group_id ON account(proxy_group_id)"
    )
    conn.commit()


def load_config_from_db() -> List[Dict[str, Any]]:
    """从数据库加载完整配置，格式与 ACCOUNT_CONFIG 一致。"""
    conn = _get_conn()
    try:
        init_config_tables(conn)
        groups: List[Dict[str, Any]] = []
        for row in conn.execute(
            "SELECT id, proxy_host, proxy_user, proxy_pass, fingerprint_id, timezone FROM proxy_group ORDER BY id"
        ).fetchall():
            gid, proxy_host, proxy_user, proxy_pass, fingerprint_id, timezone = row
            accounts = []
            for acc in conn.execute(
                "SELECT name, session_key, max_requests, profile_id FROM account WHERE proxy_group_id = ? ORDER BY id",
                (gid,),
            ).fetchall():
                name, session_key, max_requests, profile_id = acc
                a: Dict[str, Any] = {
                    "name": name,
                    "session_key": session_key,
                    "max_requests": max_requests,
                }
                if profile_id:
                    a["profile_id"] = profile_id
                accounts.append(a)
            groups.append(
                {
                    "proxy_host": proxy_host,
                    "proxy_user": proxy_user,
                    "proxy_pass": proxy_pass,
                    "fingerprint_id": fingerprint_id or "",
                    "timezone": timezone,
                    "accounts": accounts,
                }
            )
        return groups
    finally:
        conn.close()


def save_config_to_db(config: List[Dict[str, Any]]) -> None:
    """将完整配置写入数据库（先清空配置表再插入）。"""
    conn = _get_conn()
    try:
        init_config_tables(conn)
        conn.execute("DELETE FROM account")
        conn.execute("DELETE FROM proxy_group")
        for group_cfg in config:
            cur = conn.execute(
                """
                INSERT INTO proxy_group (proxy_host, proxy_user, proxy_pass, fingerprint_id, timezone)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(group_cfg.get("proxy_host", "")),
                    str(group_cfg.get("proxy_user", "")),
                    str(group_cfg.get("proxy_pass", "")),
                    str(group_cfg.get("fingerprint_id", "")),
                    group_cfg.get("timezone"),
                ),
            )
            gid = cur.lastrowid
            for a in group_cfg.get("accounts", []):
                conn.execute(
                    """
                    INSERT INTO account (proxy_group_id, name, session_key, max_requests, profile_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        gid,
                        str(a.get("name", "")),
                        str(a.get("session_key", "")),
                        int(a.get("max_requests", 40)),
                        a.get("profile_id"),
                    ),
                )
        conn.commit()
    finally:
        conn.close()


__all__ = [
    "get_db_path",
    "init_config_tables",
    "load_config_from_db",
    "save_config_to_db",
]
