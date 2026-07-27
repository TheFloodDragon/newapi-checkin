# -*- coding: utf-8 -*-
"""后台执行层。

- TaskRunner   ：QThreadPool 统一执行 providers.query_status / run_checkin。
  信号定义在长寿命 runner 上，任务对象只负责计算后经 runner 转发回主线程，
  消除旧版 BatchTask.setAutoDelete(False) + 手动持引用的整套 workaround。
- BrowserWorker：QThread，仅保留需要人工交互/长时驻留的 capture（登录捕获）
  与 verify（登录态检测）；providers 调用一律走 TaskRunner。
"""

from __future__ import annotations

import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThread, QThreadPool, Signal

import accounts_store

from . import core

TaskCallback = Callable[[dict[str, Any]], None]


def _task_context(action: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "site": params.get("name") or params.get("base_url"),
        "base_url": params.get("base_url", ""),
        "site_profile": params.get("site_profile") or params.get("type", ""),
        "auth_method": params.get("auth_method", ""),
        "checkin_action": params.get("checkin_action", ""),
    }


class _ProviderTask(QRunnable):
    """在线程池里执行一次 providers 调用，结果经 runner 的信号送回主线程。"""

    def __init__(self, action: str, params: dict[str, Any], callback: TaskCallback, done_signal: Signal):
        super().__init__()
        self.action = action
        self.params = params
        self.callback = callback
        self._done = done_signal

    def run(self) -> None:
        context = _task_context(self.action, self.params)
        started = time.perf_counter()
        core.bg_log("INFO", "后台任务开始", **context)
        try:
            import providers

            site = accounts_store.site_config_from_mapping(self.params)
            if self.action == "query":
                qs = providers.query_status(site)
                result = {
                    "ok": qs.ok,
                    "query": True,
                    "status": qs.status,
                    "quota_usd": qs.quota_usd,
                    "checked_in": qs.checked_in,
                    "message": qs.message,
                    "detail": qs.detail,
                }
            else:  # checkin
                cr = providers.run_checkin(site)
                result = {
                    "ok": cr.status in ("success", "already_done"),
                    "status": cr.status,
                    "message": cr.message,
                    "detail": cr.detail,
                }
            core.bg_log(
                "INFO" if result.get("ok") else "WARN",
                "后台任务完成",
                duration=f"{time.perf_counter() - started:.2f}s",
                result=result,
                **context,
            )
        except Exception as exc:
            result = core.error_result("后台任务异常", exc, duration=f"{time.perf_counter() - started:.2f}s", **context)
            result["query"] = self.action == "query"
        self._done.emit(self.callback, result)


class TaskRunner(QObject):
    """providers 调用的统一入口；回调保证在主线程执行。"""

    _done = Signal(object, dict)

    def __init__(self, parent: QObject | None = None, max_threads: int = 5):
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max_threads)
        self._done.connect(self._dispatch)

    def submit(self, action: str, params: dict[str, Any], callback: TaskCallback) -> None:
        self._pool.start(_ProviderTask(action, params, callback, self._done))

    def _dispatch(self, callback: object, result: dict) -> None:
        try:
            callback(result)  # type: ignore[operator]
        except Exception as exc:
            core.bg_log("ERROR", "任务回调异常", error=exc)

    def clear_pending(self) -> None:
        """清空尚未开始的排队任务；已在飞的任务无法安全中断。"""
        self._pool.clear()


class BrowserWorker(QThread):
    """在后台线程跑 Playwright 交互操作，避免阻塞 UI。

    action ∈ {"capture", "verify"}：
      - capture：有头浏览器人工登录捕获登录态，结束返回 base64 state；
      - verify ：无头检测登录态是否有效。
    通过信号把进度/结果回传主线程（Qt 信号跨线程安全）。
    """

    progress = Signal(str)
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, action: str, params: dict[str, Any], parent=None):
        super().__init__(parent)
        self.action = action
        self.params = params
        self._close_requested = False

    def request_close(self) -> None:
        """capture 模式：用户确认登录完成后置位，让 wait_for_close 返回。"""
        self._close_requested = True

    def _fail(self, message: str, exc: BaseException | None = None) -> None:
        import traceback

        from mask_utils import mask_secrets

        tb = traceback.format_exc() if exc is not None else ""
        text = f"{message}：{exc}" if exc is not None else message
        core.bg_log("ERROR", text, traceback=tb, **_task_context(self.action, self.params))
        self.failed.emit(mask_secrets(text))

    def run(self) -> None:  # noqa: D401 - QThread 入口
        log = self.progress.emit
        p = self.params
        started = time.perf_counter()
        core.bg_log("INFO", "浏览器任务开始", **_task_context(self.action, self.params))

        try:
            import asyncio

            from browser import session as browser_session
        except Exception as exc:
            self._fail("加载 browser_session 失败", exc)
            return

        async def _wait_for_close_async() -> None:
            waited = 0.0
            while not self._close_requested and waited < 600.0:
                await asyncio.sleep(0.2)
                waited += 0.2

        try:
            if self.action == "capture":
                if p.get("auth_method") == "oauth":
                    result = browser_session.run_sync(
                        browser_session.capture_oauth_state(
                            oauth_provider=p.get("oauth_provider", "linuxdo"),
                            proxy=p.get("proxy", ""),
                            log=log,
                            wait_for_close=_wait_for_close_async,
                        )
                    )
                elif (p.get("site_profile") or p.get("type")) == "sub2api":
                    result = browser_session.run_sync(
                        browser_session.capture_sub2api_login(
                            base_url=p["base_url"],
                            proxy=p.get("proxy", ""),
                            log=log,
                            wait_for_close=_wait_for_close_async,
                        )
                    )
                else:
                    result = browser_session.run_sync(
                        browser_session.capture_login(
                            base_url=p["base_url"],
                            fallback_uid=p.get("fallback_uid", ""),
                            proxy=p.get("proxy", ""),
                            log=log,
                            wait_for_close=_wait_for_close_async,
                        )
                    )
            elif self.action == "verify":
                if (p.get("site_profile") or p.get("type")) == "sub2api":
                    # sub2api 无 /api/user/self，用 browser_state 刷新 token 检测有效性
                    token = browser_session.run_sync(
                        browser_session.capture_sub2api_token(
                            base_url=p["base_url"],
                            browser_state_text=p.get("browser_state", ""),
                            proxy=p.get("proxy", ""),
                            log=log,
                        )
                    )
                    if token:
                        result = {"ok": True, "message": f"登录态有效，已刷新 auth_token（{len(token)} 字符）"}
                    else:
                        result = {"ok": False, "message": "登录态无效或无法刷新 token，请重新捕获。"}
                else:
                    result = browser_session.run_sync(
                        browser_session.verify_state(
                            base_url=p["base_url"],
                            browser_state_text=p.get("browser_state", ""),
                            fallback_uid=p.get("fallback_uid", ""),
                            proxy=p.get("proxy", ""),
                            log=log,
                        )
                    )
            else:
                self._fail(f"未知操作：{self.action}")
                return
        except browser_session.BrowserSessionError as exc:
            self._fail("浏览器会话失败", exc)
            return
        except Exception as exc:
            self._fail("浏览器操作异常", exc)
            return

        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        core.bg_log(
            "INFO" if ok else "WARN",
            "浏览器任务完成",
            ok=ok,
            duration=f"{time.perf_counter() - started:.2f}s",
            result=result,
            **_task_context(self.action, self.params),
        )
        self.finished_ok.emit(result)
