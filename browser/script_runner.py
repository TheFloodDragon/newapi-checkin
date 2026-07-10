#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到动作的脚本运行器。"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlparse

from . import bypass, popups, session, state
from .script_helpers import ScriptHelpers

CHECKIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CHECKIN_DIR
SCREENSHOT_DIR = CHECKIN_DIR / "results" / "browser_script"
VALID_STATUSES = {"success", "already_done", "need_login", "need_verification", "need_config", "network_error", "error"}


class BrowserScriptError(Exception):
    """browser_script 运行器错误。"""


@dataclass
class BrowserScriptResult:
    status: str
    message: str
    detail: Any = None


@dataclass(frozen=True)
class ScriptSiteView:
    """暴露给用户脚本的只读站点视图。"""

    name: str
    base_url: str
    site_profile: str
    auth_method: str
    checkin_action: str
    oauth_provider: str
    oauth_account: str
    proxy: str
    script: str
    script_args: dict[str, Any]
    script_timeout: int


def _env_headless() -> bool:
    raw = os.getenv("CHECKIN_HEADLESS", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))


def _origin_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def run_sync(*args: Any, **kwargs: Any) -> BrowserScriptResult:
    """同步运行 browser_script。

    支持两种调用：
    - run_sync(coro) 作为 session.run_sync 的薄封装；
    - run_sync(site=..., browser_state_text=..., script_path=..., script_args=..., timeout=...)
    """
    if args and len(args) == 1 and not kwargs:
        return session.run_sync(args[0])
    return session.run_sync(run_browser_script(**kwargs))


def resolve_script_path(script_path: str) -> Path:
    """校验并解析仓库内相对脚本路径。"""
    raw = (script_path or "").strip().replace("\\", "/")
    if not raw:
        raise BrowserScriptError("未配置 browser_script 脚本路径")
    parsed = urlparse(raw)
    if parsed.scheme or raw.startswith("//"):
        raise BrowserScriptError("脚本路径必须是仓库内相对路径，不能是 URL 或绝对路径")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise BrowserScriptError("脚本路径必须是仓库内相对路径，不能使用绝对路径或 ..")
    resolved = (REPO_ROOT / path).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise BrowserScriptError("脚本路径超出仓库目录") from exc
    if not resolved.exists() or not resolved.is_file():
        raise BrowserScriptError(f"脚本文件不存在：{raw}")
    if resolved.suffix.lower() != ".py":
        raise BrowserScriptError("browser_script 只支持 Python 脚本文件（.py）")
    return resolved


def _load_module(script_file: Path) -> ModuleType:
    module_name = f"checkin_browser_script_{abs(hash(str(script_file)))}"
    spec = importlib.util.spec_from_file_location(module_name, script_file)
    if spec is None or spec.loader is None:
        raise BrowserScriptError(f"无法加载脚本：{script_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    run_func = getattr(module, "run", None)
    if not callable(run_func):
        raise BrowserScriptError("脚本必须定义 async def run(page, context, site, helpers)")
    return module


async def _restore_storage_state(context: Any, storage_state: dict[str, Any]) -> None:
    cookies = storage_state.get("cookies") or []
    if cookies:
        await context.add_cookies(cookies)

    origin_map: dict[str, dict[str, str]] = {}
    for origin_data in storage_state.get("origins", []) or []:
        origin = str(origin_data.get("origin") or "")
        if not origin:
            continue
        pairs: dict[str, str] = {}
        for item in origin_data.get("localStorage", []) or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            pairs[name] = str(item.get("value") or "")
        if pairs:
            origin_map[origin] = pairs
    if origin_map:
        init_js = """
        (() => {
          const states = %s;
          const pairs = states[location.origin] || {};
          for (const [key, value] of Object.entries(pairs)) {
            try { localStorage.setItem(key, value); } catch (_) {}
          }
        })();
        """ % json.dumps(origin_map, ensure_ascii=False, separators=(",", ":"))
        await context.add_init_script(init_js)


def _site_view(site: Any, script_path: str, script_args: dict[str, Any] | None, timeout: int) -> ScriptSiteView:
    return ScriptSiteView(
        name=str(getattr(site, "name", "") or ""),
        base_url=str(getattr(site, "base_url", "") or ""),
        site_profile=str(getattr(site, "site_profile", "") or ""),
        auth_method=str(getattr(site, "auth_method", "") or ""),
        checkin_action=str(getattr(site, "checkin_action", "") or ""),
        oauth_provider=str(getattr(site, "oauth_provider", "") or ""),
        oauth_account=str(getattr(site, "oauth_account", "") or ""),
        proxy=str(getattr(site, "proxy", "") or ""),
        script=script_path,
        script_args=dict(script_args or {}),
        script_timeout=int(timeout or 120),
    )


def _normalize_result(raw: Any, *, script_file: Path) -> BrowserScriptResult:
    if isinstance(raw, BrowserScriptResult):
        return raw
    if not isinstance(raw, dict):
        return BrowserScriptResult(
            "error",
            "脚本返回值无效：应返回 dict 或 helpers.*() 结果",
            {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT))},
        )

    status = str(raw.get("status") or "success").strip().lower()
    if status not in VALID_STATUSES:
        status = "error"
    message = str(raw.get("message") or status)
    detail = raw.get("detail")
    if isinstance(detail, dict):
        detail = dict(detail)
    elif detail is None:
        detail = {}
    else:
        detail = {"script_detail": detail}
    detail.setdefault("checkin_source", "browser_script")
    detail.setdefault("script", str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"))
    return BrowserScriptResult(status, message, detail)


async def run_browser_script(
    *,
    site: Any,
    browser_state_text: str,
    script_path: str,
    script_args: dict[str, Any] | None = None,
    timeout: int = 120,
) -> BrowserScriptResult:
    """启动 Camoufox、恢复登录态、执行用户脚本并归一化结果。"""
    try:
        script_file = resolve_script_path(script_path)
    except BrowserScriptError as exc:
        return BrowserScriptResult("need_config", str(exc), {"checkin_source": "browser_script"})
    timeout = max(1, int(timeout or 120))

    try:
        storage_state = state.decode_state(browser_state_text)
    except state.BrowserStateError as exc:
        return BrowserScriptResult("need_login", f"登录态解码失败：{exc}", {"checkin_source": "browser_script"})

    try:
        module = _load_module(script_file)
    except BrowserScriptError as exc:
        return BrowserScriptResult("need_config", str(exc), {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/")})
    except Exception as exc:
        return BrowserScriptResult("error", f"加载浏览器脚本异常：{exc}", {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/")})
    run_func = getattr(module, "run")
    site_view = _site_view(site, str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"), script_args, timeout)

    browser = None
    page = None
    try:
        browser, context = await bypass.launch_camoufox(
            headless=_env_headless(),
            humanize=True,
            geoip=True,
            proxy=str(getattr(site, "proxy", "") or "") or None,
        )
        await _restore_storage_state(context, storage_state)
        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(site_view.base_url))
        helpers = ScriptHelpers(page, context, site_view, SCREENSHOT_DIR)

        maybe_result = run_func(page, context, site_view, helpers)
        if inspect.isawaitable(maybe_result):
            raw_result = await asyncio.wait_for(maybe_result, timeout=timeout)
        else:
            raw_result = maybe_result
        return _normalize_result(raw_result, script_file=script_file)
    except asyncio.TimeoutError:
        screenshot = ""
        if page is not None:
            try:
                helpers = ScriptHelpers(page, getattr(page, "context", None), site_view, SCREENSHOT_DIR)
                screenshot = await helpers.screenshot("browser_script-timeout.png")
            except Exception:
                screenshot = ""
        detail = {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"), "timeout": timeout}
        if screenshot:
            detail["screenshot"] = screenshot
        return BrowserScriptResult("error", f"浏览器脚本执行超时（{timeout}s）", detail)
    except BrowserScriptError as exc:
        return BrowserScriptResult("need_config", str(exc), {"checkin_source": "browser_script"})
    except Exception as exc:
        if session._is_driver_closed_error(exc):
            return BrowserScriptResult(
                "error",
                "浏览器驱动已关闭或页面脚本触发 Playwright Firefox 兼容问题，请重试。",
                {
                    "checkin_source": "browser_script",
                    "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"),
                    "driver_crashed": True,
                    "error": str(exc),
                },
            )
        screenshot = ""
        if page is not None:
            try:
                helpers = ScriptHelpers(page, getattr(page, "context", None), site_view, SCREENSHOT_DIR)
                screenshot = await helpers.screenshot("browser_script-error.png")
            except Exception:
                screenshot = ""
        detail = {
            "checkin_source": "browser_script",
            "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"),
            "error": str(exc),
            "traceback": traceback.format_exc(limit=5),
        }
        if screenshot:
            detail["screenshot"] = screenshot
        return BrowserScriptResult("error", f"浏览器脚本异常：{exc}", detail)
    finally:
        await session._safe_close_page(page)
        await session._safe_close_browser(browser)
