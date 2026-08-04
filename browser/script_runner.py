#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到动作的脚本运行器。"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from accounts_store import RESULTS_DIR_NAME
from checkin_core.enums import VALID_RESULT_STATUSES

from . import bypass, popups, runtime_loop, script_loader, session, state
from .script_contract import LoadedSiteScript
from .script_helpers import ScriptHelpers

CHECKIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = CHECKIN_DIR
SCREENSHOT_DIR = CHECKIN_DIR / RESULTS_DIR_NAME / "browser_script"
VALID_STATUSES = VALID_RESULT_STATUSES  # 兼容公开名称


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


def _make_log(site_name: str):
    """构造 browser_script 的进度日志回调。

    其它浏览器路径（relogin / newapi / sub2api）都会往 stderr 打进度，唯独
    browser_script 此前完全静默：脚本可能跑几十秒（等 SPA 渲染、过 Turnstile、
    账密登录），用户与 CI 日志里看不到任何过程，失败时只有一行最终结论，无从
    定位卡在哪一步。这里统一加上带站点名的前缀，并经 mask_secrets 脱敏。
    """
    label = str(site_name or "browser_script").strip() or "browser_script"

    def _log(message: str) -> None:
        try:
            from mask_utils import mask_secrets

            text = mask_secrets(str(message))
        except Exception:
            text = str(message)
        print(f"[browser_script:{label}] {text}", file=sys.stderr, flush=True)

    return _log


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
    """校验并解析仓库内相对脚本路径。

    实现在 script_loader（不依赖 playwright），这样纯 HTTP 路径也能加载站点脚本里
    与传输无关的部分（如极速蹬的答题题库），不必为此拉起浏览器依赖。
    """
    try:
        return script_loader.resolve_script_path(script_path)
    except script_loader.ScriptLoadError as exc:
        raise BrowserScriptError(str(exc)) from exc


def _load_module(script_file: Path) -> LoadedSiteScript:
    try:
        hooks = LoadedSiteScript.from_module(script_loader.load_script_module(script_file))
        hooks.require_browser_run()
    except script_loader.ScriptLoadError as exc:
        raise BrowserScriptError(str(exc)) from exc
    except TypeError as exc:
        raise BrowserScriptError(str(exc)) from exc
    return hooks


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
        script_timeout=int(timeout or 240),
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


# 明确表示「这次没登录成功」的脚本结论：这类结果下浏览器里的 storage_state
# 是登出态，续存它会毁掉上次还能用的缓存登录态。
_UNAUTHENTICATED_STATUSES = frozenset({"need_login", "need_verification", "need_config"})


async def _persist_session(site: Any, context: Any, log: Any, status: str = "") -> None:
    """脚本结束时把登录态与 token 写进运行期缓存（用真实 SiteConfig 算 basis）。

    为什么不能在脚本里做：脚本拿到的是脱敏的 ScriptSiteView，没有 access_token /
    browser_state 字段，token_cache 只能按「空凭据」算 basis，写出的缓存与配置
    basis 不一致，下次运行会被 resolve_cached_credentials 判为过期缓存直接忽略
    —— 等于登录态从未续存（实测极速蹬缓存里的 state_basis 一直是空串的摘要）。

    同时把 localStorage 里的 auth_token / refresh_token 单独存下来：Sub2API 系站点
    启用 Turnstile 后纯 HTTP 无法登录（服务端校验，实测 TURNSTILE_VERIFICATION_FAILED），
    只有这两个值能让下次运行走纯 HTTP、完全不启动浏览器。

    status 是脚本结论，用来挡掉「没登录成功却把登出态写进缓存」：本函数在 finally
    里调用，以前无条件执行。save_site_tokens 只更新非空字段，所以 token 不会被写空，
    但 browser_state 每次都非空 —— Turnstile 没过、账密登录被拒时，浏览器里是干净的
    登出态，续存它会覆盖上次还能用的登录态，下次运行只能从零再登一次（表现为「登录
    失败之后就再也复用不上缓存」）。

    任何失败都静默忽略：缓存写不进去只是下次要多开一次浏览器，不该影响本次结果。
    """
    if context is None:
        return
    if str(status or "").strip().lower() in _UNAUTHENTICATED_STATUSES:
        log(f"脚本结论为 {status}（未完成登录），跳过续存登录态以保留上次可用的缓存")
        return
    try:
        storage_state = await context.storage_state()
    except Exception:
        return
    try:
        encoded = state.encode_state(storage_state)
    except Exception:
        encoded = ""
    # 必须限定站点来源：脚本跑完的 storage_state 常含多个 origin（站点自身 +
    # 共享 OAuth provider + 第三方 iframe），而 auth_token / refresh_token 这两个
    # 键名各站通用。不限定就会把第一个同名值当成本站 token 写进缓存，下次运行
    # 拿着别站身份去请求，表现为「明明刚捕获成功却一直登录失效」。
    site_base = str(getattr(site, "base_url", "") or "")
    access = session.storage_access_token(storage_state, base_url=site_base)
    refresh = session.storage_refresh_token(storage_state, base_url=site_base)
    if not encoded and not access and not refresh:
        return
    try:
        from providers import token_cache

        token_cache.save_site_tokens(site, access, refresh, browser_state=encoded)
    except Exception:
        return
    hints = [name for name, value in (("state", encoded), ("token", access), ("refresh_token", refresh)) if value]
    log("已续存登录态到运行期缓存：" + "、".join(hints))


async def run_browser_script(
    *,
    site: Any,
    browser_state_text: str,
    script_path: str,
    script_args: dict[str, Any] | None = None,
    timeout: int = 240,
    oauth_provider: str = "",
) -> BrowserScriptResult:
    """启动 Camoufox、恢复登录态、按需完成 OAuth，然后执行用户脚本。"""
    try:
        script_file = resolve_script_path(script_path)
    except BrowserScriptError as exc:
        return BrowserScriptResult("need_config", str(exc), {"checkin_source": "browser_script"})
    timeout = max(1, int(timeout or 240))

    # 空登录态是合法输入：站点没有 browser_state、但脚本自带账密登录兜底时，
    # action 层会显式传空串，让脚本在干净浏览器里自行登录（见 providers/actions/
    # browser_script.py 的 _script_can_self_login）。此时跳过解码，不能报 need_login。
    if str(browser_state_text or "").strip():
        try:
            storage_state = state.decode_state(browser_state_text)
        except state.BrowserStateError as exc:
            return BrowserScriptResult("need_login", f"登录态解码失败：{exc}", {"checkin_source": "browser_script"})
    else:
        storage_state = {"cookies": [], "origins": []}

    try:
        hooks = _load_module(script_file)
    except BrowserScriptError as exc:
        return BrowserScriptResult("need_config", str(exc), {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/")})
    except Exception as exc:
        return BrowserScriptResult("error", f"加载浏览器脚本异常：{exc}", {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/")})
    run_func = hooks.require_browser_run()
    site_view = _site_view(site, str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"), script_args, timeout)

    log = _make_log(site_view.name)

    browser = None
    page = None
    context = None
    # 记录最终结论供 finally 判断是否该续存登录态：need_login / need_verification
    # 这类结果意味着浏览器里是登出态，写进缓存会覆盖上次还能用的登录态。
    # error（脚本异常/超时）仍然续存 —— 登录可能已经成功，只是签到那步失败了，
    # 这份快照下次还能省一次登录。
    outcome_status = ""

    def _record(result: BrowserScriptResult) -> BrowserScriptResult:
        nonlocal outcome_status
        outcome_status = result.status
        return result

    try:
        log(f"启动浏览器执行脚本 {site_view.script}（超时 {timeout}s）")
        browser, context = await bypass.launch_camoufox(
            headless=_env_headless(),
            humanize=True,
            geoip=True,
            proxy=str(getattr(site, "proxy", "") or "") or None,
        )
        await state.restore_storage_state(context, storage_state)
        page = await context.new_page()
        log("浏览器已就绪，已恢复登录态" if storage_state.get("cookies") else "浏览器已就绪（无登录态，脚本需自行登录）")
        allowed_origin = _origin_from_url(site_view.base_url)
        await popups.setup_popup_guard(page, allowed_origin=allowed_origin)

        if oauth_provider:
            log(f"先完成 {oauth_provider} OAuth 登录回跳...")
            # 共享 OAuth state 只包含第三方 provider 登录态；先完成站点 OAuth 回跳，
            # 再把已认证的站点页面交给自定义脚本。
            await page.goto(site_view.base_url, wait_until="domcontentloaded", timeout=60000)
            await popups.dismiss_popups(page)
            oauth_link = await session._trigger_oauth(page, site_view.base_url.rstrip("/"), oauth_provider)
            if not oauth_link.get("landed_back") or oauth_link.get("need_human") or oauth_link.get("cloudflare"):
                detail = {
                    "checkin_source": "browser_script",
                    "oauth_provider": oauth_provider,
                    "oauth_landed_back": bool(oauth_link.get("landed_back")),
                    "oauth_need_human": bool(oauth_link.get("need_human")),
                    "oauth_cloudflare": bool(oauth_link.get("cloudflare")),
                }
                detail.update({key: value for key, value in oauth_link.items() if key not in detail})
                log("OAuth 自动登录未完成")
                return _record(BrowserScriptResult("need_login", "OAuth 自动登录未完成，请检查共享登录态或站点 OAuth 配置。", detail))
            log("OAuth 登录已回跳站点")
            await popups.dismiss_popups(page)

        helpers = ScriptHelpers(page, context, site_view, SCREENSHOT_DIR, log=log)

        log("开始执行脚本 run()")
        maybe_result = run_func(page, context, site_view, helpers)
        if inspect.isawaitable(maybe_result):
            raw_result = await asyncio.wait_for(maybe_result, timeout=timeout)
        else:
            raw_result = maybe_result
        outcome = _normalize_result(raw_result, script_file=script_file)
        log(f"脚本结束：{outcome.status} - {outcome.message}")
        return _record(outcome)
    except asyncio.TimeoutError:
        screenshot = ""
        if page is not None:
            try:
                helpers = ScriptHelpers(page, getattr(page, "context", None), site_view, SCREENSHOT_DIR, log=log)
                screenshot = await helpers.screenshot("browser_script-timeout.png")
            except Exception:
                screenshot = ""
        detail = {"checkin_source": "browser_script", "script": str(script_file.relative_to(REPO_ROOT)).replace("\\", "/"), "timeout": timeout}
        if screenshot:
            detail["screenshot"] = screenshot
        log(f"脚本执行超时（{timeout}s）")
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
                helpers = ScriptHelpers(page, getattr(page, "context", None), site_view, SCREENSHOT_DIR, log=log)
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
        await _persist_session(site, context, log, status=outcome_status)
        await runtime_loop.safe_close_page(page)
        await runtime_loop.safe_close_browser(browser)
