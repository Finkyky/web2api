"""
FastAPI 应用组装：配置加载、账号池、会话缓存、浏览器管理、插件注册、路由挂载。
"""

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.account.pool import AccountPool
from core.api.chat_handler import ChatHandler
from core.api.config_routes import create_config_router
from core.api.routes import create_router
from core.config.repository import ConfigRepository
from core.plugin.base import PluginRegistry
from core.plugin.claude import register_claude_plugin
from core.runtime.browser_manager import BrowserManager
from core.runtime.keys import ProxyKey
from core.runtime.session_cache import SessionCache

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化配置与 ChatHandler，关闭时不做持久化（会话缓存进程内）。"""
    # 注册插件
    register_claude_plugin()

    repo = ConfigRepository()
    repo.init_schema()
    groups = repo.load_groups()

    if not groups:
        logger.warning("v2 数据库无配置，请先在 /config/v2 配置页添加或通过 API 写入")
        app.state.chat_handler = None
        app.state.session_cache = SessionCache()
        app.state.browser_manager = BrowserManager()
        app.state.config_repo = ConfigRepository()
        yield
        return

    pool = AccountPool.from_groups(groups)
    session_cache = SessionCache()
    browser_manager = BrowserManager()
    # 启动时为每个代理组、每个已注册 type 预建浏览器、填满 page 池并预登录
    for g in groups:
        proxy_key = ProxyKey(
            g.proxy_host,
            g.proxy_user,
            g.fingerprint_id,
        )
        for type_name in PluginRegistry.all_types():
            plugin = PluginRegistry.get(type_name)
            if plugin is None:
                continue
            accounts_of_type = [
                a for a in g.accounts if a.type == type_name and a.is_available()
            ]
            apply_auth_fn: Any = None
            if accounts_of_type:
                account = accounts_of_type[0]
                pl = plugin

                async def _apply_auth(ctx: Any, p: Any) -> None:
                    await pl.apply_auth(ctx, p, account.auth)

                apply_auth_fn = _apply_auth
            else:
                logger.debug(
                    "代理组 %s 下无可用 %s 账号，跳过预登录",
                    proxy_key.fingerprint_id,
                    type_name,
                )
            try:
                await browser_manager.init_page_pool(
                    proxy_key,
                    g.proxy_pass,
                    type_name,
                    plugin.create_page,
                    apply_auth_fn=apply_auth_fn,
                )
            except Exception as e:
                logger.warning(
                    "启动时预建 page 池失败 proxy=%s type=%s: %s",
                    proxy_key.fingerprint_id,
                    type_name,
                    e,
                )
    app.state.chat_handler = ChatHandler(
        pool=pool,
        session_cache=session_cache,
        browser_manager=browser_manager,
        config_repo=repo,
    )
    app.state.session_cache = session_cache
    app.state.browser_manager = browser_manager
    app.state.config_repo = repo
    logger.info("v2 服务已就绪，已注册 type: %s", ", ".join(PluginRegistry.all_types()))
    yield
    # shutdown: 可在此关闭 browser_manager 中浏览器（当前为按需连接，不常驻）
    app.state.chat_handler = None


def create_app() -> FastAPI:
    app = FastAPI(
        title="Web2API v2 (Plugin)",
        description="按 type 路由的 OpenAI 兼容接口，baseUrl: http://ip:port/{type}/v1/...",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(create_router())
    app.include_router(create_config_router())
    return app


app = create_app()
