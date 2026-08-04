#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器运行时、同步桥接与资源清理。

这里不包含任何站点/OAuth 流程。会话流程只负责登记 browser/page，统一由
``BrowserResources`` 做幂等清理，避免某个 page 关闭异常阻断 browser 回收。
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable


class BrowserSessionError(Exception):
    """带结构化结果状态的浏览器会话错误。"""

    def __init__(self, message: str, *, status: str = "error", detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.detail = detail


LogFn = Callable[[str], None]


def noop(_msg: str) -> None:
    """默认日志回调。"""


def env_headless() -> bool:
    """读取 CHECKIN_HEADLESS；CI 默认无头，本地默认有头。"""
    raw = os.getenv("CHECKIN_HEADLESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))


def browser_mode_label(headless: bool) -> str:
    """返回便于日志展示的浏览器运行模式。"""
    ci = bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))
    return f"{'headless' if headless else 'headful'} / {'CI' if ci else 'local'}"


DRIVER_CLOSED_MARKERS = (
    "connection closed",
    "target closed",
    "browser has been closed",
    "browser closed",
    "page closed",
    "socket.send()",
    "closed while reading from the driver",
    "playwright driver",
    "pipe closed by peer",
    "os.write(pipe",
    "cannot read properties of undefined (reading 'url')",
)


def is_driver_closed_error(exc: BaseException | str) -> bool:
    """判断异常是否表示 Playwright/Camoufox 驱动已经断开。"""
    text = str(exc).lower()
    return any(marker in text for marker in DRIVER_CLOSED_MARKERS)


async def safe_close_page(page: Any) -> None:
    """尽力关闭 page，不让普通清理错误覆盖业务结果。"""
    if page is None:
        return
    try:
        await page.close()
    except Exception:
        pass


async def safe_close_browser(browser: Any) -> None:
    """尽力关闭 browser，不让普通清理错误覆盖业务结果。"""
    if browser is None:
        return
    try:
        await browser.close()
    except Exception:
        pass


@dataclass(slots=True)
class BrowserResources:
    """一次浏览器流程持有的资源，提供幂等且完整的关闭路径。"""

    browser: Any = None
    page: Any = None
    _closed: bool = field(default=False, init=False, repr=False)

    def track_page(self, page: Any) -> Any:
        """登记主页面并原样返回，便于在赋值表达式中使用。"""
        self.page = page
        return page

    async def close(self) -> None:
        """先关主页面，再确保 browser 必定被关；重复调用无副作用。"""
        if self._closed:
            return
        self._closed = True
        page, browser = self.page, self.browser
        self.page = None
        self.browser = None
        try:
            await safe_close_page(page)
        finally:
            await safe_close_browser(browser)


async def safe_storage_state(context: Any, log: LogFn = noop) -> dict[str, Any]:
    """导出登录态；驱动断开时转换成结构化会话错误。"""
    del log  # 保留兼容参数，供调用方传入统一日志回调。
    try:
        return await context.storage_state()
    except Exception as exc:
        if is_driver_closed_error(exc):
            raise BrowserSessionError(
                "浏览器驱动已关闭，无法导出登录态；这通常是站点页面脚本触发了 Playwright Firefox 兼容问题，请重试。"
            ) from exc
        raise


async def safe_goto(
    page: Any,
    url: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout: int = 30000,
    log: LogFn = noop,
) -> bool:
    """容错导航；指定等待失败时降级到 commit，驱动断连继续抛出。"""
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except Exception as exc:
        if is_driver_closed_error(exc):
            raise
        if wait_until != "commit":
            try:
                await page.goto(url, wait_until="commit", timeout=min(timeout, 15000))
                log(f"导航等待 {wait_until} 失败，已降级到 commit：{url}")
                return True
            except Exception as retry_exc:
                if is_driver_closed_error(retry_exc):
                    raise
                log(f"导航失败（{type(exc).__name__}，降级也失败：{type(retry_exc).__name__}）：{url}")
                return False
        log(f"导航失败（{type(exc).__name__}）：{url}")
        return False


async def fetch_json_in_page(page: Any, url: str, timeout_ms: int = 15000) -> dict[str, Any] | None:
    """在页面上下文 fetch JSON，使用 AbortController 限制等待时间。"""
    try:
        return await page.evaluate(
            """async ([u, timeoutMs]) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const r = await fetch(u, {
                        credentials: 'include',
                        headers: { 'Accept': 'application/json' },
                        signal: controller.signal,
                    });
                    const t = await r.text();
                    try { return { ok: r.ok, status: r.status, body: JSON.parse(t) }; }
                    catch { return { ok: r.ok, status: r.status, body: t.slice(0, 200) }; }
                } catch (e) {
                    return { ok: false, status: 0, body: String(e && e.name === 'AbortError' ? 'fetch timeout' : e) };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            [url, timeout_ms],
        )
    except Exception:
        return None


def patch_windows_asyncio_finalizers() -> None:
    """静默 Windows Proactor 管道关闭后的 ``__del__`` 噪声。"""
    if os.name != "nt":
        return
    try:
        import asyncio.base_subprocess as base_subprocess
        import asyncio.proactor_events as proactor_events
    except Exception:
        return

    def _wrap(cls: type[Any]) -> None:
        if getattr(cls, "_checkin_safe_del", False):
            return
        original = getattr(cls, "__del__", None)
        if original is None:
            return

        def _safe_del(self: Any) -> None:
            try:
                original(self)
            except ValueError as exc:
                if "I/O operation on closed pipe" not in str(exc):
                    raise
            except Exception:
                pass

        cls.__del__ = _safe_del
        cls._checkin_safe_del = True

    _wrap(proactor_events._ProactorBasePipeTransport)
    _wrap(base_subprocess.BaseSubprocessTransport)


def _run_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """在指定事件循环运行协程，并可靠清理残留 task 和传输。"""
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        if sys.platform == "win32":
            try:
                loop.run_until_complete(asyncio.sleep(0.3))
            except Exception:
                pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def run_sync(coro: Any) -> Any:
    """同步执行协程；已有运行中事件循环时改用独立线程。"""
    patch_windows_asyncio_finalizers()

    try:
        running_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None

    if running_loop is not None:
        result_future: concurrent.futures.Future[Any] = concurrent.futures.Future()

        def _thread_target() -> None:
            if sys.platform == "win32":
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                result_future.set_result(_run_loop(new_loop, coro))
            except BaseException as exc:  # noqa: BLE001
                result_future.set_exception(exc)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(_thread_target)
        return result_future.result()

    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return _run_loop(loop, coro)


__all__ = [
    "BrowserResources",
    "BrowserSessionError",
    "DRIVER_CLOSED_MARKERS",
    "LogFn",
    "browser_mode_label",
    "env_headless",
    "fetch_json_in_page",
    "is_driver_closed_error",
    "noop",
    "patch_windows_asyncio_finalizers",
    "run_sync",
    "safe_close_browser",
    "safe_close_page",
    "safe_goto",
    "safe_storage_state",
]
