"""
统一的 YAML 配置加载，所有配置直接从 config.yaml 读取。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

_config_cache: dict[str, Any] | None = None


def load_config() -> dict[str, Any]:
    """加载根目录下的 config.yaml，不存在时返回空 dict。"""
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    if not _CONFIG_PATH.exists():
        _config_cache = {}
        return {}
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            _config_cache = {}
        else:
            _config_cache = dict(data)
    except Exception:
        _config_cache = {}
    return _config_cache


def get(section: str, key: str, default: Any = None) -> Any:
    """从 config 读取 section.key，不存在则返回 default。"""
    cfg = load_config().get(section) or {}
    if not isinstance(cfg, dict):
        return default
    val = cfg.get(key)
    return val if val is not None else default
