import sys
from pathlib import Path

import argparse
import asyncio
import datetime
import json
import logging
import sqlite3
import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Dict, List

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from account_pool import AccountPool, ChatAccountState, ProxyGroup
from config_db import (
    get_db_path,
    init_config_tables,
    load_config_from_db,
    save_config_to_db,
)
from proxy_extension_builder import EXTENSION_DIR, generate_proxy_auth_extension
import web2api


logger = logging.getLogger(__name__)


# ----------------------------
# 1. 账号 + IP 池配置（从数据库加载）
# ----------------------------

# 配置存于 SQLite，通过 GET/PUT /api/config 或前端配置页维护；启动时从 DB 加载
DB_PATH = get_db_path()
_POOL: AccountPool | None = None


def _get_pool() -> AccountPool:
    if _POOL is None:
        raise RuntimeError("账号池未初始化（数据库无配置或服务未就绪）")
    return _POOL


def _build_pool_from_config(config: List[Dict[str, Any]]) -> AccountPool:
    if not config:
        raise ValueError("配置为空，请先在前端配置页添加代理组与账号")
    return AccountPool.from_config(config, window_hours=5)


# ----------------------------
# 2. 启动浏览器（带 IP + 扩展）
# ----------------------------

CHROMIUM_BIN = "/Applications/Chromium.app/Contents/MacOS/Chromium"
TIMEZONE = "America/Chicago"
REMOTE_DEBUGGING_PORT = 9222
CDP_ENDPOINT = f"http://127.0.0.1:{REMOTE_DEBUGGING_PORT}"
CLAUDE_SESSION_COOKIE_NAME = "sessionKey"

# 浏览器进程缓存：同一 IP 组复用，切换 IP 时关闭旧进程再起新进程
_CachedProxyKey = tuple[str, str, str]  # (proxy_host, proxy_user, fingerprint_id)
_cached_proc: subprocess.Popen[Any] | None = None
_cached_proxy_key: _CachedProxyKey | None = None
# 当前浏览器里已生效的 session_key，仅当切换账号时才需要 set cookie + reload
_last_session_key: str | None = None


def _is_cdp_listening() -> bool:
    """检测 9222 端口是否有 CDP 在监听。"""
    import socket

    try:
        with socket.create_connection(
            ("127.0.0.1", REMOTE_DEBUGGING_PORT), timeout=1.0
        ):
            pass
        return True
    except OSError:
        return False


def _close_cached_browser() -> None:
    """关闭缓存的浏览器进程并清空缓存。"""
    global _cached_proc, _cached_proxy_key, _last_session_key
    _last_session_key = None
    if _cached_proc is None:
        _cached_proxy_key = None
        return
    try:
        _cached_proc.terminate()
        _cached_proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        _cached_proc.kill()
        _cached_proc.wait(timeout=3)
    except Exception as e:
        logger.warning("关闭缓存浏览器时异常: %s", e)
    _cached_proc = None
    _cached_proxy_key = None
    _last_session_key = None


def _launch_and_cache(
    *,
    proxy_host: str,
    proxy_user: str,
    proxy_pass: str,
    profile_id: str,
    fingerprint_id: str,
) -> subprocess.Popen[Any]:
    """启动浏览器并写入缓存。"""
    global _cached_proc, _cached_proxy_key
    proc = launch_browser_with_proxy(
        proxy_host=proxy_host,
        proxy_user=proxy_user,
        proxy_pass=proxy_pass,
        profile_id=profile_id,
        fingerprint_id=fingerprint_id,
    )
    if proc is None:
        raise RuntimeError("启动浏览器失败")
    _cached_proc = proc
    _cached_proxy_key = (proxy_host, proxy_user, fingerprint_id)
    return proc


def launch_browser_with_proxy(
    *,
    proxy_host: str,
    proxy_user: str,
    proxy_pass: str,
    profile_id: str,
    fingerprint_id: str,
) -> subprocess.Popen[Any] | None:
    """
    根据指定 IP + 账号启动 Chromium：
    - 先动态写入代理认证扩展（background.js）；
    - 再用该扩展 + proxy_host + profile_id 启动浏览器。
    """
    user_data_dir = Path.home() / "fp-data" / profile_id

    if not Path(CHROMIUM_BIN).exists():
        print(f"Chromium 不存在: {CHROMIUM_BIN}", file=sys.stderr)
        return None

    # 写入当前 IP 的代理认证信息
    generate_proxy_auth_extension(proxy_user=proxy_user, proxy_pass=proxy_pass)

    if not EXTENSION_DIR.is_dir():
        print(f"扩展目录不存在: {EXTENSION_DIR}", file=sys.stderr)
        return None

    args: List[str] = [
        CHROMIUM_BIN,
        f"--remote-debugging-port={REMOTE_DEBUGGING_PORT}",
        f"--load-extension={EXTENSION_DIR}",
        f"--fingerprint={fingerprint_id}",
        "--fingerprint-platform=windows",
        "--fingerprint-brand=Edge",
        f"--user-data-dir={user_data_dir}",
        f"--timezone={TIMEZONE}",
        f"--proxy-server=http://{proxy_host}",
        "--force-webrtc-ip-handling-policy",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--disable-features=AsyncDNS",
        "--no-first-run",
        "--no-default-browser-check",
    ]

    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


async def _wait_for_cdp_ready(
    endpoint: str = CDP_ENDPOINT, max_attempts: int = 30, interval: float = 1.0
) -> bool:
    """等待 CDP 端口就绪，复用 web2api 里的逻辑。"""
    return await web2api._wait_for_cdp(
        endpoint, max_attempts=max_attempts, interval=interval
    )  # type: ignore[attr-defined]


# ----------------------------
# 3. 业务：获取账号 + 发起聊天
# ----------------------------


async def run_chat_with_pool(
    message: str,
    *,
    auto_launch: bool = True,
    proxy_group: ProxyGroup | None = None,
    account: ChatAccountState | None = None,
) -> None:
    """
    从池中选择一个 (IP, 账号)，必要时启动/复用浏览器，并通过 web2api 调用 claude.ai。
    - 若调用方已传入 proxy_group/account（如 HTTP 已 acquire 过），则不再 acquire，避免重复扣次数。
    - 无 9222 监听或需切换 IP 时自动启动浏览器并缓存 process；同 IP 组下次聊天复用该 process。
    """
    pool = _get_pool()
    if proxy_group is None or account is None:
        proxy_group, account = pool.acquire()
    proxy_key: _CachedProxyKey = (
        proxy_group.proxy_host,
        proxy_group.proxy_user,
        proxy_group.fingerprint_id,
    )

    logger.info(
        "选中 IP=%s, 代理账号=%s, 2pai账号=%s(profile=%s)",
        proxy_group.proxy_host,
        proxy_group.proxy_user,
        account.name,
        account.profile_id,
    )

    # 切换 IP 或进程已死：关闭旧缓存
    if _cached_proc is not None:
        if _cached_proxy_key != proxy_key or _cached_proc.poll() is not None:
            logger.info("关闭旧浏览器进程（切换 IP 或进程已退出）")
            _close_cached_browser()
            await asyncio.sleep(2)  # 等端口释放后再起新进程

    need_launch = _cached_proc is None or not _is_cdp_listening()
    if need_launch:
        if not auto_launch:
            raise RuntimeError(
                "9222 端口无浏览器监听，请设置 auto_launch: true 或先手动启动浏览器"
            )
        proc = _launch_and_cache(
            proxy_host=proxy_group.proxy_host,
            proxy_user=proxy_group.proxy_user,
            proxy_pass=proxy_group.proxy_pass,
            profile_id=account.profile_id,
            fingerprint_id=proxy_group.fingerprint_id,
        )
        logger.info("已启动 Chromium PID=%s，等待 CDP 就绪...", proc.pid)
        ok = await _wait_for_cdp_ready(CDP_ENDPOINT)
        if not ok:
            _close_cached_browser()
            raise RuntimeError("CDP 未在预期时间内就绪")

    # 连接已启动的浏览器；仅当切换账号时才 set cookie + reload，同一账号复用不刷新
    global _last_session_key
    browser = await web2api.connect_browser(CDP_ENDPOINT)  # type: ignore[attr-defined]
    try:
        default_ctx = browser.contexts[0] if browser.contexts else None
        if not default_ctx:
            raise RuntimeError("没有默认 BrowserContext")

        need_switch_account = _last_session_key != account.session_key
        if need_switch_account:
            _last_session_key = account.session_key
            await default_ctx.add_cookies(
                [
                    {
                        "name": CLAUDE_SESSION_COOKIE_NAME,
                        "value": account.session_key,
                        "domain": ".claude.ai",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = await web2api._ensure_claude_page(default_ctx)  # type: ignore[attr-defined]
            await page.reload(wait_until="domcontentloaded")
        else:
            page = await web2api._ensure_claude_page(default_ctx)  # type: ignore[attr-defined]

        try:
            await web2api._demo_claude_chat_stream(default_ctx, message)  # type: ignore[attr-defined]
        except RuntimeError as e:
            if "429" in str(e):
                pool.block_account(account)
                save_pool_state_to_db(pool)
                logger.warning(
                    "账号 %s 返回 429 额度用尽，已封禁 5 小时，切换账号重试",
                    account.name,
                )
                await run_chat_with_pool(message, auto_launch=auto_launch)
            else:
                raise
    finally:
        await browser.close()


async def stream_chat_with_pool(
    message: str,
    *,
    auto_launch: bool = True,
    proxy_group: ProxyGroup | None = None,
    account: ChatAccountState | None = None,
) -> AsyncGenerator[str, None]:
    """
    与 run_chat_with_pool 相同的选号与浏览器逻辑，但以异步生成器形式逐块 yield 助手回复。
    用于 OpenAI 兼容的流式接口。
    """
    pool = _get_pool()
    if proxy_group is None or account is None:
        proxy_group, account = pool.acquire()
    proxy_key: _CachedProxyKey = (
        proxy_group.proxy_host,
        proxy_group.proxy_user,
        proxy_group.fingerprint_id,
    )

    logger.info(
        "stream 选中 IP=%s, 代理账号=%s, 2pai账号=%s(profile=%s)",
        proxy_group.proxy_host,
        proxy_group.proxy_user,
        account.name,
        account.profile_id,
    )

    if _cached_proc is not None:
        if _cached_proxy_key != proxy_key or _cached_proc.poll() is not None:
            logger.info("关闭旧浏览器进程（切换 IP 或进程已退出）")
            _close_cached_browser()
            await asyncio.sleep(2)

    need_launch = _cached_proc is None or not _is_cdp_listening()
    if need_launch:
        if not auto_launch:
            raise RuntimeError(
                "9222 端口无浏览器监听，请设置 auto_launch: true 或先手动启动浏览器"
            )
        proc = _launch_and_cache(
            proxy_host=proxy_group.proxy_host,
            proxy_user=proxy_group.proxy_user,
            proxy_pass=proxy_group.proxy_pass,
            profile_id=account.profile_id,
            fingerprint_id=proxy_group.fingerprint_id,
        )
        logger.info("已启动 Chromium PID=%s，等待 CDP 就绪...", proc.pid)
        ok = await _wait_for_cdp_ready(CDP_ENDPOINT)
        if not ok:
            _close_cached_browser()
            raise RuntimeError("CDP 未在预期时间内就绪")

    global _last_session_key
    browser = await web2api.connect_browser(CDP_ENDPOINT)  # type: ignore[attr-defined]
    try:
        default_ctx = browser.contexts[0] if browser.contexts else None
        if not default_ctx:
            raise RuntimeError("没有默认 BrowserContext")

        need_switch_account = _last_session_key != account.session_key
        if need_switch_account:
            _last_session_key = account.session_key
            await default_ctx.add_cookies(
                [
                    {
                        "name": CLAUDE_SESSION_COOKIE_NAME,
                        "value": account.session_key,
                        "domain": ".claude.ai",
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            page = await web2api._ensure_claude_page(default_ctx)  # type: ignore[attr-defined]
            await page.reload(wait_until="domcontentloaded")
        else:
            await web2api._ensure_claude_page(default_ctx)  # type: ignore[attr-defined]

        try:
            async for piece in web2api.stream_claude_reply(default_ctx, message):  # type: ignore[attr-defined]
                yield piece
        except RuntimeError as e:
            if "429" in str(e):
                pool.block_account(account)
                save_pool_state_to_db(pool)
                logger.warning(
                    "账号 %s 返回 429 额度用尽，已封禁 5 小时，切换账号重试",
                    account.name,
                )
                async for piece in stream_chat_with_pool(
                    message, auto_launch=auto_launch
                ):
                    yield piece
            else:
                raise
    finally:
        await browser.close()


# ----------------------------
# 3.1 SQLite 持久化：账号封禁状态跨进程
# ----------------------------


def _get_db_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    return conn


def init_db() -> None:
    """初始化 SQLite 表结构（配置表 + 账号状态表）。"""
    conn = _get_db_conn()
    try:
        init_config_tables(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_state (
                proxy_host TEXT NOT NULL,
                proxy_user TEXT NOT NULL,
                account TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                used_in_window INTEGER NOT NULL,
                window_started_at TEXT,
                blocked_until TEXT,
                PRIMARY KEY (proxy_host, proxy_user, account, profile_id)
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_pool_state_to_db(pool: AccountPool) -> None:
    """将当前池状态快照保存到 SQLite。"""
    conn = _get_db_conn()
    try:
        snapshots = pool.snapshot_states()
        rows: List[Dict[str, Any]] = []
        for s in snapshots:
            rows.append(
                {
                    "proxy_host": s["proxy_host"],
                    "proxy_user": s["proxy_user"],
                    "account": s["account"],
                    "profile_id": s["profile_id"],
                    "used_in_window": int(s.get("used_in_window", 0)),
                    "window_started_at": (
                        s["window_started_at"].isoformat()
                        if s.get("window_started_at") is not None
                        else None
                    ),
                    "blocked_until": (
                        s["blocked_until"].isoformat()
                        if s.get("blocked_until") is not None
                        else None
                    ),
                }
            )
        conn.executemany(
            """
            INSERT OR REPLACE INTO account_state (
                proxy_host, proxy_user, account, profile_id,
                used_in_window, window_started_at, blocked_until
            ) VALUES (
                :proxy_host, :proxy_user, :account, :profile_id,
                :used_in_window, :window_started_at, :blocked_until
            )
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def load_pool_state_from_db(pool: AccountPool) -> None:
    """从 SQLite 读取状态并恢复到池。"""
    if not DB_PATH.exists():
        return
    conn = _get_db_conn()
    try:
        cur = conn.execute(
            """
            SELECT
                proxy_host,
                proxy_user,
                account,
                profile_id,
                used_in_window,
                window_started_at,
                blocked_until
            FROM account_state
            """
        )
        snapshots: List[Dict[str, Any]] = []
        for (
            proxy_host,
            proxy_user,
            account,
            profile_id,
            used_in_window,
            window_started_at,
            blocked_until,
        ) in cur.fetchall():
            ws_dt = (
                datetime.datetime.fromisoformat(window_started_at)
                if window_started_at
                else None
            )
            bu_dt = (
                datetime.datetime.fromisoformat(blocked_until)
                if blocked_until
                else None
            )
            snapshots.append(
                {
                    "proxy_host": proxy_host,
                    "proxy_user": proxy_user,
                    "account": account,
                    "profile_id": profile_id,
                    "used_in_window": int(used_in_window),
                    "window_started_at": ws_dt,
                    "blocked_until": bu_dt,
                }
            )
        pool.restore_from_snapshots(snapshots)
    finally:
        conn.close()


# ----------------------------
# 4. FastAPI HTTP 接口
# ----------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    global _POOL
    # startup
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    init_db()
    config = load_config_from_db()
    if config:
        _POOL = _build_pool_from_config(config)
        load_pool_state_from_db(_POOL)
        logger.info("FastAPI 已启动，账号池状态已从 SQLite 恢复")
    else:
        _POOL = None
        logger.warning("FastAPI 已启动，但数据库无配置，请先在配置页添加代理组与账号")
    yield
    # shutdown
    if _POOL is not None:
        save_pool_state_to_db(_POOL)
        logger.info("FastAPI 已关闭，账号池状态已保存到 SQLite")


app = FastAPI(title="Multi-IP 2pai Service", lifespan=lifespan)

# CORS：兼容 Chatbox 等前端的 OPTIONS 预检请求
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    auto_launch: bool = True


class ChatResponse(BaseModel):
    ok: bool
    proxy_host: str
    proxy_user: str
    account: str
    profile_id: str


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(req: ChatRequest) -> ChatResponse:
    """
    HTTP 接口：从池中取一个 (IP, 账号) 并发起一次聊天。

    返回本次使用的 IP / 代理账号 / 2pai 账号信息。
    """
    try:
        pool = _get_pool()
        proxy_group, account = pool.acquire()

        await run_chat_with_pool(
            req.message,
            auto_launch=req.auto_launch,
            proxy_group=proxy_group,
            account=account,
        )

        save_pool_state_to_db(pool)

        return ChatResponse(
            ok=True,
            proxy_host=proxy_group.proxy_host,
            proxy_user=proxy_group.proxy_user,
            account=account.name,
            profile_id=account.profile_id,
        )
    except Exception as exc:
        logger.exception("HTTP 聊天调用失败")
        raise HTTPException(status_code=500, detail=str(exc))  # noqa: B904


# ----------------------------
# 4.2 OpenAI 兼容接口（便于接入各类应用）
# ----------------------------

OPENAI_COMPATIBLE_MODEL = "claude-sonnet-4-5-20250929"


class OpenAIContentPart(BaseModel):
    """兼容 OpenAI 新版格式：content 可以是 [{\"type\": \"text\", \"text\": \"...\"}, ...]."""

    type: str
    text: str | None = None


class OpenAIMessage(BaseModel):
    role: str = Field(..., description="system | user | assistant")
    # 兼容两种写法：
    # 1) content: \"纯字符串\"
    # 2) content: [{\"type\": \"text\", \"text\": \"...\"}, ...]
    content: str | list[OpenAIContentPart] = ""


class OpenAIChatRequest(BaseModel):
    model: str = Field(default=OPENAI_COMPATIBLE_MODEL, description="模型名，可忽略")
    messages: List[OpenAIMessage] = Field(..., description="对话列表")
    stream: bool = Field(default=False, description="是否流式返回")


def _openai_extract_user_content(messages: List[OpenAIMessage]) -> str:
    """从 messages 中提取要发送给 Claude 的文本：取最后一条 user，若无则拼接所有 content。"""

    def _normalize_content(c: str | list[OpenAIContentPart]) -> str:
        if isinstance(c, str):
            return c
        # content 是数组：只取 type == \"text\" 的 text 字段并拼接
        parts: list[str] = []
        for p in c:
            if p.type == "text" and p.text:
                parts.append(p.text)
        return " ".join(parts)

    user_parts: List[str] = []
    for m in messages:
        if m.role == "user" and m.content:
            user_parts.append(_normalize_content(m.content))
    if user_parts:
        return user_parts[-1]

    merged: list[str] = []
    for m in messages:
        if m.content:
            merged.append(_normalize_content(m.content))
    return " ".join(merged).strip() or ""


async def _openai_stream_generator(
    message: str, auto_launch: bool = True
) -> AsyncGenerator[str, None]:
    """acquire 账号后流式 yield；收到 429 时自动封禁并切换账号重试。"""
    pool = _get_pool()
    proxy_group, account = pool.acquire()
    try:
        async for piece in stream_chat_with_pool(
            message,
            auto_launch=auto_launch,
            proxy_group=proxy_group,
            account=account,
        ):
            yield piece
    finally:
        save_pool_state_to_db(pool)


@app.get("/v1/models")
def openai_list_models() -> Dict[str, Any]:
    """OpenAI 兼容：列出模型，便于客户端统一用 /v1/models 发现能力。"""
    return {
        "object": "list",
        "data": [
            {
                "id": OPENAI_COMPATIBLE_MODEL,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "claude",
            }
        ],
    }


@app.post("/v1/chat/completions")
async def openai_chat_completions(req: OpenAIChatRequest) -> Any:
    """
    OpenAI 兼容的聊天补全接口。
    - 请求体：model, messages: [{ role, content }], stream: bool
    - 非流式：返回 JSON { id, object, created, model, choices: [{ message, finish_reason }] }
    - 流式：返回 SSE，每块 data 为 OpenAI 格式的 chunk。
    """
    content = _openai_extract_user_content(req.messages)
    if not content:
        raise HTTPException(
            status_code=400, detail="messages 中需至少有一条带 content 的 user 消息"
        )

    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if req.stream:

        async def sse_stream() -> AsyncGenerator[str, None]:
            try:
                async for piece in _openai_stream_generator(content, auto_launch=True):
                    chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": req.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": piece},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'id': chat_id, 'object': 'chat.completion.chunk', 'created': created, 'model': req.model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception("流式 chat/completions 失败")
                err = {"error": {"message": str(e), "type": "server_error"}}
                yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            sse_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 非流式：收集完整回复后返回
    full_content: List[str] = []
    try:
        async for piece in _openai_stream_generator(content, auto_launch=True):
            full_content.append(piece)
    except Exception as exc:
        logger.exception("chat/completions 失败")
        raise HTTPException(status_code=500, detail=str(exc))  # noqa: B904

    reply = "".join(full_content)
    return {
        "id": chat_id,
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": reply},
                "finish_reason": "stop",
            }
        ],
    }


# ----------------------------
# 4.1 配置 API + 静态配置页
# ----------------------------

# 配置页静态资源目录（与 multi_web2api.py 同目录下的 static）
STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/config", response_model=List[Dict[str, Any]])
def get_config() -> List[Dict[str, Any]]:
    """获取当前账号池配置（与 multi_web2api 中 ACCOUNT_CONFIG 格式一致）。"""
    return load_config_from_db()


@app.get("/api/pool-state")
def get_pool_state() -> List[Dict[str, Any]]:
    """获取账号池状态（含 blocked_until 解冻时间），供配置页展示。"""
    pool = _get_pool()
    snapshots = pool.snapshot_states()
    # 将 datetime 转为 ISO 字符串，便于前端展示
    out: List[Dict[str, Any]] = []
    for s in snapshots:
        row = dict(s)
        bu = row.get("blocked_until")
        row["blocked_until"] = (
            bu.isoformat() if isinstance(bu, datetime.datetime) else bu
        )
        out.append(row)
    return out


@app.put("/api/config")
def put_config(config: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    更新账号池配置并立即生效。
    请求体格式与 GET /api/config 一致。
    """
    global _POOL
    if not config:
        raise HTTPException(status_code=400, detail="配置不能为空")
    # 校验结构
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
            if not isinstance(a, dict) or "name" not in a or "session_key" not in a:
                raise HTTPException(
                    status_code=400,
                    detail=f"代理组 {i + 1} 账号 {j + 1} 需包含 name 与 session_key",
                )
    save_config_to_db(config)
    try:
        _POOL = _build_pool_from_config(config)
        load_pool_state_from_db(_POOL)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"配置生效失败: {e}")  # noqa: B904
    return {"status": "ok", "message": "配置已保存并生效"}


@app.get("/config")
def config_page() -> FileResponse:
    """配置页入口。"""
    index = STATIC_DIR / "config.html"
    if not index.is_file():
        raise HTTPException(status_code=404, detail="配置页未就绪")
    return FileResponse(index)


# ----------------------------
# 5. CLI 入口（保留原来的命令行用法）
# ----------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="多 IP / 多账号 2pai 服务入口（基于 CDP + claude.ai）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 启动浏览器（仅调试用）
    launch_p = sub.add_parser("launch", help="从池中取一个 (IP,账号) 并启动浏览器")
    launch_p.set_defaults(func=_cmd_launch)

    # 聊天
    chat_p = sub.add_parser(
        "chat", help="从池中取一个 (IP,账号) 并调用 claude.ai 聊天（流式）"
    )
    chat_p.add_argument(
        "--chat",
        dest="chat_message",
        metavar="TEXT",
        required=True,
        help="要发送的消息",
    )
    chat_p.add_argument(
        "--no-launch",
        action="store_true",
        help="不自动启动浏览器（假设已手动按所选 profile 启动好）",
    )
    chat_p.set_defaults(func=_cmd_chat)

    # 查看当前池状态
    stat_p = sub.add_parser("stat", help="查看当前 IP/账号池状态")
    stat_p.set_defaults(func=_cmd_stat)

    # 启动 HTTP 服务（配置页 /config、聊天接口 /chat）
    serve_p = sub.add_parser("serve", help="启动 FastAPI 服务")
    serve_p.add_argument("--host", default="127.0.0.1", help="监听地址")
    serve_p.add_argument("--port", type=int, default=8000, help="监听端口")
    serve_p.set_defaults(func=_cmd_serve)

    return parser


def _cmd_launch(_args: argparse.Namespace) -> int:
    pool = _get_pool()
    proxy_group, account = pool.acquire()
    proc = launch_browser_with_proxy(
        proxy_host=proxy_group.proxy_host,
        proxy_user=proxy_group.proxy_user,
        proxy_pass=proxy_group.proxy_pass,
        profile_id=account.profile_id,
        fingerprint_id=proxy_group.fingerprint_id,
    )
    if proc is None:
        return 1
    print(
        f"已启动 Chromium PID={proc.pid}, IP={proxy_group.proxy_host}, "
        f"代理账号={proxy_group.proxy_user}, 2pai账号={account.name} (profile={account.profile_id})"
    )
    return 0


def _cmd_stat(_args: argparse.Namespace) -> int:
    from pprint import pprint

    pprint(_get_pool().snapshot_states())
    return 0


def _cmd_chat(args: argparse.Namespace) -> int:
    auto_launch = not getattr(args, "no_launch", False)
    try:
        asyncio.run(run_chat_with_pool(args.chat_message, auto_launch=auto_launch))
    except Exception:
        logger.exception("聊天调用失败")
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn  # noqa: E402

    uvicorn.run(
        "multi_web2api:app",
        host=args.host,
        port=args.port,
        reload=False,
    )
    return 0


def main() -> int:
    global _POOL
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    init_db()
    config = load_config_from_db()
    if config:
        try:
            _POOL = _build_pool_from_config(config)
            load_pool_state_from_db(_POOL)
        except Exception as e:
            logger.error("从数据库加载配置失败: %s", e)
            return 1
    else:
        _POOL = None
    parser = _build_arg_parser()
    args = parser.parse_args()
    if _POOL is None and args.command in ("launch", "chat", "stat"):
        logger.error("数据库无配置，请先通过配置页或 API 添加代理组与账号")
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
