"""
v2 配置 API：GET/PUT /api/v2/config；配置页 GET /config/v2。
"""

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from core.account.pool import AccountPool
from core.api.chat_handler import ChatHandler
from core.config.repository import ConfigRepository
from core.plugin.base import PluginRegistry

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_config_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v2/types")
    def get_types() -> list[str]:
        """返回已注册的 type 列表，供配置页 type 下拉使用。"""
        return PluginRegistry.all_types()

    @router.get("/api/v2/config")
    def get_config(request: Request) -> list[dict[str, Any]]:
        """获取 v2 配置（代理组 + 账号 name/type/auth）。"""
        repo: ConfigRepository | None = getattr(request.app.state, "config_repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        return repo.load_raw()

    @router.put("/api/v2/config")
    def put_config(request: Request, config: list[dict[str, Any]]) -> dict[str, Any]:
        """更新 v2 配置并立即生效。"""
        repo: ConfigRepository | None = getattr(request.app.state, "config_repo", None)
        if repo is None:
            raise HTTPException(status_code=503, detail="服务未就绪")
        if not config:
            raise HTTPException(status_code=400, detail="配置不能为空")
        for i, g in enumerate(config):
            if not isinstance(g, dict):
                raise HTTPException(status_code=400, detail=f"第 {i + 1} 项应为对象")
            for k in ("proxy_host", "proxy_user", "proxy_pass", "fingerprint_id"):
                if k not in g:
                    raise HTTPException(
                        status_code=400, detail=f"代理组 {i + 1} 缺少字段: {k}"
                    )
            accounts = g.get("accounts", [])
            if not accounts:
                raise HTTPException(
                    status_code=400, detail=f"代理组 {i + 1} 至少需要一个账号"
                )
            for j, a in enumerate(accounts):
                if not isinstance(a, dict) or not (a.get("name") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 name",
                    )
                if not (a.get("type") or "").strip():
                    raise HTTPException(
                        status_code=400,
                        detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 type（如 claude）",
                    )
        try:
            repo.save_raw(config)
        except Exception as e:
            logger.exception("保存 v2 配置失败")
            raise HTTPException(status_code=400, detail=str(e)) from e
        # 立即生效：重新加载池并替换 chat_handler
        try:
            groups = repo.load_groups()
            if groups:
                new_pool = AccountPool.from_groups(groups)
                session_cache = request.app.state.session_cache
                browser_manager = request.app.state.browser_manager
                request.app.state.chat_handler = ChatHandler(
                    pool=new_pool,
                    session_cache=session_cache,
                    browser_manager=browser_manager,
                    config_repo=repo,
                )
            else:
                request.app.state.chat_handler = None
        except Exception as e:
            logger.exception("重载账号池失败")
            raise HTTPException(
                status_code=500, detail=f"配置已保存但重载失败: {e}"
            ) from e
        return {"status": "ok", "message": "配置已保存并生效"}

    @router.get("/config/v2")
    def config_page_v2() -> FileResponse:
        """v2 配置页入口。"""
        path = STATIC_DIR / "config_v2.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="配置页未就绪")
        return FileResponse(path)

    return router
