"""
浏览器管理器：按 ProxyKey 多浏览器并存，每键一个进程+CDP+context+按 type 的 Page 动态调度池。
每 page 支持多并发（默认 5），用 request_id 隔离；满 5 时预开新 page；非主 page 空闲 10 分钟自动关闭。
"""

import asyncio
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from core.constants import (
    CDP_PORT_RANGE,
    CHROMIUM_BIN,
    TIMEZONE,
    user_data_dir,
)
from core.runtime.keys import ProxyKey

logger = logging.getLogger(__name__)

CreatePageFn = Callable[[BrowserContext], Coroutine[Any, Any, Page]]
ApplyAuthFn = Callable[[BrowserContext, Page], Coroutine[Any, Any, None]]


@dataclass
class _PageSlot:
    """单个 page 槽位：引用计数 + 是否主 page + 空闲关闭任务。"""

    page: Page
    ref_count: int = 0
    is_main: bool = False
    idle_task: asyncio.Task[None] | None = field(default=None, repr=False)


@dataclass
class _PagePool:
    """某 type 的动态 page 池：槽位列表 + 每 page 最大并发 + 空闲关闭时间。"""

    slots: list[_PageSlot] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    create_page_fn: CreatePageFn | None = None
    page_max_concurrent: int = 5
    idle_shutdown_seconds: float = 600.0


async def _wait_for_cdp(
    host: str,
    port: int,
    max_attempts: int = 30,
    interval: float = 1.0,
) -> bool:
    for _ in range(max_attempts):
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, asyncio.TimeoutError):
            await asyncio.sleep(interval)
    return False


def _is_cdp_listening(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            pass
        return True
    except OSError:
        return False


@dataclass
class _BrowserEntry:
    proc: subprocess.Popen[Any]
    port: int
    browser: Browser
    context: BrowserContext
    page_pools: dict[str, _PagePool] = field(default_factory=dict)
    refcount: int = 0


class BrowserManager:
    """
    按 ProxyKey 多浏览器并存；refcount 按 type 占用，归零时立即关闭。
    先查 current_proxy_keys 再决定复用或开新浏览器。
    """

    def __init__(
        self,
        chromium_bin: str = CHROMIUM_BIN,
        port_range: list[int] | None = None,
        page_pool_size: int = 3,
        page_max_concurrent: int = 5,
        idle_shutdown_seconds: float = 600.0,
    ) -> None:
        self._chromium_bin = chromium_bin
        self._port_range = port_range or list(CDP_PORT_RANGE)
        self._page_pool_size = page_pool_size
        self._page_max_concurrent = page_max_concurrent
        self._idle_shutdown_seconds = idle_shutdown_seconds
        self._entries: dict[ProxyKey, _BrowserEntry] = {}
        self._available_ports: set[int] = set(self._port_range)
        self._playwright: Any = None

    def _close_entry(self, proxy_key: ProxyKey) -> None:
        """同步关闭：仅终止进程并回收端口（无法 await browser.close，进程退出后 CDP 自然断开）。"""
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        for pool in entry.page_pools.values():
            for slot in pool.slots:
                if slot.idle_task and not slot.idle_task.done():
                    slot.idle_task.cancel()
        entry.page_pools.clear()
        try:
            entry.proc.terminate()
            entry.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            entry.proc.kill()
            entry.proc.wait(timeout=3)
        except Exception as e:
            logger.warning("关闭浏览器进程时异常: %s", e)
        self._available_ports.add(entry.port)
        del self._entries[proxy_key]

    async def _close_entry_async(self, proxy_key: ProxyKey) -> None:
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        for pool in entry.page_pools.values():
            for slot in pool.slots:
                if slot.idle_task and not slot.idle_task.done():
                    slot.idle_task.cancel()
                try:
                    await slot.page.close()
                except Exception:
                    pass
        entry.page_pools.clear()
        if entry.browser is not None:
            try:
                await entry.browser.close()
            except Exception as e:
                logger.warning("关闭 CDP 浏览器时异常: %s", e)
        try:
            entry.proc.terminate()
            entry.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            entry.proc.kill()
            entry.proc.wait(timeout=3)
        except Exception as e:
            logger.warning("关闭浏览器进程时异常: %s", e)
        self._available_ports.add(entry.port)
        del self._entries[proxy_key]

    def _launch_process(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
        port: int,
    ) -> subprocess.Popen[Any]:
        """启动 Chromium 进程（代理 + 扩展），使用指定 port。"""
        from proxy_extension_builder import generate_proxy_auth_extension

        extension_path = generate_proxy_auth_extension(
            proxy_user=proxy_key.proxy_user,
            proxy_pass=proxy_pass,
            fingerprint_id=proxy_key.fingerprint_id,
        )
        if not Path(extension_path).is_dir():
            raise RuntimeError(f"扩展目录不存在: {extension_path}")

        udd = user_data_dir(proxy_key.fingerprint_id)
        udd.mkdir(parents=True, exist_ok=True)

        if not Path(self._chromium_bin).exists():
            raise RuntimeError(f"Chromium 不存在: {self._chromium_bin}")

        args = [
            self._chromium_bin,
            f"--remote-debugging-port={port}",
            f"--load-extension={extension_path}",
            f"--fingerprint={proxy_key.fingerprint_id}",
            "--fingerprint-platform=windows",
            "--fingerprint-brand=Edge",
            f"--user-data-dir={udd}",
            f"--timezone={TIMEZONE}",
            f"--proxy-server=http://{proxy_key.proxy_host}",
            "--force-webrtc-ip-handling-policy",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--disable-features=AsyncDNS",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        env = os.environ.copy()
        env["NODE_OPTIONS"] = (
            env.get("NODE_OPTIONS") or ""
        ).strip() + " --no-deprecation"
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        return proc

    async def ensure_browser(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
    ) -> BrowserContext:
        """
        确保存在对应 proxy_key 的浏览器；若已有且存活则 refcount+1 并返回 context，否则起新进程并 refcount=1。
        调用方在请求结束时必须调用 release(proxy_key)。
        """
        entry = self._entries.get(proxy_key)
        if entry is not None:
            if entry.proc.poll() is not None or not _is_cdp_listening(entry.port):
                await self._close_entry_async(proxy_key)
            else:
                return entry.context

        if not self._available_ports:
            raise RuntimeError(
                "无可用 CDP 端口，当前并发浏览器数已达上限，请稍后重试或增大 CDP_PORT_RANGE"
            )
        port = self._available_ports.pop()
        proc = self._launch_process(proxy_key, proxy_pass, port)
        logger.info("已启动 Chromium PID=%s port=%s，等待 CDP 就绪...", proc.pid, port)
        ok = await _wait_for_cdp("127.0.0.1", port)
        if not ok:
            self._available_ports.add(port)
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                pass
            raise RuntimeError("CDP 未在预期时间内就绪")
        self._available_ports.discard(port)

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        endpoint = f"http://127.0.0.1:{port}"
        browser = await self._playwright.chromium.connect_over_cdp(
            endpoint, timeout=10000
        )
        context = browser.contexts[0] if browser.contexts else None
        if context is None:
            await browser.close()
            self._available_ports.add(port)
            raise RuntimeError("浏览器无默认 context")
        self._entries[proxy_key] = _BrowserEntry(
            proc=proc,
            port=port,
            browser=browser,
            context=context,
            page_pools={},
            refcount=0,
        )
        return context

    def release(self, proxy_key: ProxyKey, type_name: str) -> None:
        """
        释放该浏览器上某 type 的占用（如切到下一 IP 组时调用）。
        清空该 type 的 page 池，refcount 归零则关浏览器。
        """
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        if type_name in entry.page_pools:
            pool = entry.page_pools[type_name]
            for slot in pool.slots:
                if slot.idle_task and not slot.idle_task.done():
                    slot.idle_task.cancel()
            del entry.page_pools[type_name]
            entry.refcount -= 1
            if entry.refcount <= 0:
                self._close_entry(proxy_key)

    async def release_async(self, proxy_key: ProxyKey, type_name: str) -> None:
        """释放该浏览器上某 type 的占用；清空该 type 的 page 池，refcount 归零则关浏览器。"""
        entry = self._entries.get(proxy_key)
        if entry is None:
            return
        if type_name in entry.page_pools:
            pool = entry.page_pools[type_name]
            for slot in pool.slots:
                if slot.idle_task and not slot.idle_task.done():
                    slot.idle_task.cancel()
                try:
                    await slot.page.close()
                except Exception:
                    pass
            del entry.page_pools[type_name]
            entry.refcount -= 1
            if entry.refcount <= 0:
                await self._close_entry_async(proxy_key)

    def current_proxy_keys(self) -> list[ProxyKey]:
        """返回当前所有现役浏览器的 proxy_key，供「先查现役再开新」时使用。"""
        return list(self._entries.keys())

    async def _idle_shutdown_task(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        slot: _PageSlot,
        delay_seconds: float,
    ) -> None:
        """空闲 N 秒后关闭该 slot 的 page 并从池中移除。"""
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
        entry = self._entries.get(proxy_key)
        if entry is None or type_name not in entry.page_pools:
            return
        pool = entry.page_pools[type_name]
        async with pool.lock:
            if slot.ref_count != 0 or slot not in pool.slots:
                return
            pool.slots.remove(slot)
            slot.idle_task = None
        try:
            await slot.page.close()
        except Exception as e:
            logger.debug("关闭空闲 page 时异常: %s", e)
        logger.info(
            "[pool] idle shutdown page type=%s proxy=%s",
            type_name,
            getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
        )

    async def _preopen_page(self, proxy_key: ProxyKey, type_name: str) -> None:
        """预开一个 page 入池（ref_count=0），供后续请求使用。"""
        entry = self._entries.get(proxy_key)
        if entry is None or type_name not in entry.page_pools:
            return
        pool = entry.page_pools[type_name]
        if pool.create_page_fn is None:
            return
        try:
            new_page = await pool.create_page_fn(entry.context)
        except Exception as e:
            logger.warning("预开 page 失败 type=%s: %s", type_name, e)
            return
        async with pool.lock:
            pool.slots.append(_PageSlot(page=new_page, ref_count=0, is_main=False))
        logger.info(
            "[pool] preopened page type=%s proxy=%s new_page.url=%s",
            type_name,
            getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
            new_page.url,
        )

    async def init_page_pool(
        self,
        proxy_key: ProxyKey,
        proxy_pass: str,
        type_name: str,
        create_page_fn: CreatePageFn,
        *,
        pool_size: int | None = None,
        apply_auth_fn: ApplyAuthFn | None = None,
    ) -> None:
        """
        应用启动时调用：确保浏览器存在并为该 (proxy_key, type) 创建 page 池并预填。
        若池已存在则跳过。
        若传入 apply_auth_fn：先创建 1 个 page 并对其执行登录（cookie 写入 context），
        再创建其余 page；后续 page 继承 context 的 cookie，无需再登录。
        """
        context = await self.ensure_browser(proxy_key, proxy_pass)
        entry = self._entries.get(proxy_key)
        if entry is None:
            raise RuntimeError("ensure_browser 未创建 entry")
        if type_name in entry.page_pools:
            return
        pool = _PagePool(
            create_page_fn=create_page_fn,
            page_max_concurrent=self._page_max_concurrent,
            idle_shutdown_seconds=self._idle_shutdown_seconds,
        )
        entry.page_pools[type_name] = pool
        entry.refcount += 1
        # 启动只开 1 个主 page，并执行登录；后续按需动态开页
        if apply_auth_fn is not None:
            try:
                first_page = await create_page_fn(context)
                await apply_auth_fn(entry.context, first_page)
                pool.slots.append(_PageSlot(page=first_page, ref_count=0, is_main=True))
                logger.info(
                    "[pool] init 1 main page with auth (dynamic pool) type=%s proxy=%s",
                    type_name,
                    getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
                )
            except Exception as e:
                logger.warning(
                    "启动时初始化 page 池（主 page+登录）失败 type=%s: %s", type_name, e
                )
        else:
            try:
                first_page = await create_page_fn(context)
                pool.slots.append(_PageSlot(page=first_page, ref_count=0, is_main=True))
            except Exception as e:
                logger.warning("启动时初始化 page 池失败 type=%s: %s", type_name, e)

    async def apply_auth_to_pool(
        self,
        proxy_key: ProxyKey,
        type_name: str,
        apply_auth_fn: ApplyAuthFn,
    ) -> None:
        """
        对池内所有 slot 的 page 执行 apply_auth（如写 cookie、reload）。
        若无池或池空则跳过。
        """
        entry = self._entries.get(proxy_key)
        if entry is None or type_name not in entry.page_pools:
            return
        pool = entry.page_pools[type_name]
        async with pool.lock:
            for slot in pool.slots:
                try:
                    await apply_auth_fn(entry.context, slot.page)
                except Exception as e:
                    logger.warning(
                        "启动时对 page 应用 auth 失败 type=%s: %s", type_name, e
                    )

    async def acquire_page_slot(
        self,
        proxy_key: ProxyKey,
        context: BrowserContext,
        type_name: str,
        create_page_fn: CreatePageFn,
    ) -> tuple[Page, str, bool]:
        """
        从该 (proxy_key, type) 的动态池中占一个槽位，返回 (page, request_id, is_sole_user)。
        is_sole_user 保留供将来需要「仅独占时做 reload」等逻辑时使用；当前请求路径只写 cookie 不 reload。
        """
        entry = self._entries.get(proxy_key)
        if entry is None:
            raise RuntimeError("ensure_browser 未先于 acquire_page_slot 调用")
        if type_name not in entry.page_pools:
            pool = _PagePool(
                create_page_fn=create_page_fn,
                page_max_concurrent=self._page_max_concurrent,
                idle_shutdown_seconds=self._idle_shutdown_seconds,
            )
            entry.page_pools[type_name] = pool
            entry.refcount += 1
            first_page = await create_page_fn(context)
            pool.slots.append(_PageSlot(page=first_page, ref_count=0, is_main=True))
        pool = entry.page_pools[type_name]
        request_id = str(uuid.uuid4())
        async with pool.lock:
            for slot in pool.slots:
                if slot.ref_count < pool.page_max_concurrent:
                    if slot.idle_task and not slot.idle_task.done():
                        slot.idle_task.cancel()
                        slot.idle_task = None
                    slot.ref_count += 1
                    is_sole_user = slot.ref_count == 1
                    if slot.ref_count == pool.page_max_concurrent:
                        asyncio.create_task(self._preopen_page(proxy_key, type_name))
                    logger.info(
                        "[pool] acquire_page_slot type=%s proxy=%s ref_count=%s request_id=%s",
                        type_name,
                        getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
                        slot.ref_count,
                        request_id[:8],
                    )
                    return (slot.page, request_id, is_sole_user)
        # 所有 slot 已满（含预开页已被空闲关闭的情况），按需新建 page
        new_page = await create_page_fn(context)
        async with pool.lock:
            new_slot = _PageSlot(page=new_page, ref_count=1, is_main=False)
            pool.slots.append(new_slot)
            if new_slot.ref_count == pool.page_max_concurrent:
                asyncio.create_task(self._preopen_page(proxy_key, type_name))
            logger.info(
                "[pool] acquire_page_slot new page (all full or preopened closed) type=%s proxy=%s request_id=%s",
                type_name,
                getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
                request_id[:8],
            )
            return (new_page, request_id, True)

    async def release_page_slot(
        self, proxy_key: ProxyKey, type_name: str, page: Page
    ) -> None:
        """
        请求用完后调用：槽位 ref_count 减 1；若归零且非主 page 则启动空闲关闭倒计时。
        """
        entry = self._entries.get(proxy_key)
        if entry is None or type_name not in entry.page_pools:
            return
        pool = entry.page_pools[type_name]
        async with pool.lock:
            for slot in pool.slots:
                if slot.page is page:
                    slot.ref_count -= 1
                    if slot.idle_task and not slot.idle_task.done():
                        slot.idle_task.cancel()
                        slot.idle_task = None
                    if slot.ref_count == 0 and not slot.is_main:
                        slot.idle_task = asyncio.create_task(
                            self._idle_shutdown_task(
                                proxy_key,
                                type_name,
                                slot,
                                pool.idle_shutdown_seconds,
                            )
                        )
                    logger.info(
                        "[pool] release_page_slot type=%s proxy=%s ref_count=%s",
                        type_name,
                        getattr(proxy_key, "fingerprint_id", str(proxy_key)[:20]),
                        slot.ref_count,
                    )
                    return
        logger.warning(
            "[pool] release_page_slot page not found in pool type=%s", type_name
        )

    @property
    def current_proxy_key(self) -> ProxyKey | None:
        """返回任意一个当前存在的 key（兼容旧用法）；多浏览器时语义以 refcount 为准。"""
        if not self._entries:
            return None
        return next(iter(self._entries))
