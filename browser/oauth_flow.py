#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点 OAuth 触发、第三方授权与结果判定。"""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from config import Timeouts

from . import bypass, oauth_providers, popups
from .runtime_loop import LogFn, fetch_json_in_page, is_driver_closed_error, noop, safe_goto
from .site_messages import (
    add_site_error,
    attach_site_errors,
    install_site_error_collector,
    message_with_site_error,
    short_body,
    site_error_messages,
    site_success_message,
)
from .storage_scope import same_origin
from .waf import is_waf_html, solve_waf, waf_is_blocked, wait_for_ready

OAUTH_WAIT_SECONDS = Timeouts.OAUTH_WAIT

DEFAULT_LOGIN_SELECTORS = [
    "text=/linux.?do/i",
    "text=/使用.*登录/i",
    "text=/登录|登入|Sign in|Log in/i",
    "[href*='oauth']",
    "[href*='/login']",
    "button:has-text('Linux')",
    "button:has-text('GitHub')",
    "text=/github/i",
]

SITE_OAUTH_TOGGLE_SELECTORS = [
    "main a[href='/register']",
    "main a[href$='/register']",
    "main a:has-text('注册')",
    "main button:has-text('注册')",
    "main >> text=/没有账户|No account|Create account|Sign up|Register/i",
    "main a[href='/login']",
    "main a[href$='/login']",
    "main a:has-text('登录')",
    "main button:has-text('登录')",
    "main >> text=/已有账户|Already have|Sign in|Log in/i",
]


def _quota_to_usd(value: Any) -> str:
    from providers.base import format_usd

    return format_usd(value, is_usd=False, fallback=str(value))


async def api_get_json(page: Any, url: str) -> dict[str, Any] | None:
    """在页面上下文 GET JSON，自动携带同源 Cookie。"""
    return await fetch_json_in_page(page, url, timeout_ms=15000)


def extract_oauth_state(body: Any) -> str:
    """兼容不同 New API 派生站的 OAuth state 响应结构。"""
    if isinstance(body, dict):
        for key in ("data", "state", "oauth_state", "oauthState"):
            value = body.get(key)
            if isinstance(value, dict):
                nested = extract_oauth_state(value)
                if nested:
                    return nested
            elif value:
                return str(value)
    elif isinstance(body, str):
        text = body.strip()
        if text and not text.startswith("<") and len(text) <= 512:
            return text
    return ""


def oauth_landed(link: dict[str, Any]) -> bool:
    """授权是否完成；过程中出现过 CF 不代表最终失败。"""
    return bool(link.get("landed_back")) and not link.get("need_human") and not link.get("waf_blocked")


def oauth_checkin_result(quota_before: Any, quota_after: Any, link: dict[str, Any]) -> dict[str, Any]:
    """综合额度变化、OAuth 回跳状态和站点弹窗生成签到结果。"""
    result: dict[str, Any] = {
        "quota_before": quota_before,
        "quota_after": quota_after,
        "delta": None,
        "link": link,
    }

    if quota_before is not None and quota_after is not None and quota_after > quota_before:
        delta = quota_after - quota_before
        result["delta"] = delta
        result["status"] = "success"
        result["message"] = f"OAuth 重登成功，额度增加 {_quota_to_usd(delta)}（当前 {_quota_to_usd(quota_after)}）。"
        return result

    success_message = str(link.get("site_success_message") or "").strip()
    oauth_completed = oauth_landed(link)
    if oauth_completed and success_message:
        result["status"] = "success"
        result["message"] = f"签到成功（站点弹窗：{success_message}）。"
        return result

    if quota_before is None and quota_after is None:
        if link.get("waf_blocked"):
            result["status"] = "need_verification"
            result["message"] = message_with_site_error(
                "站点阿里云 WAF 持续拦截当前出口 IP（数据中心/CI IP 信誉过低），"
                "浏览器无法通过 JS 挑战，本次签到中止。登录态可能仍有效，无需重新捕获；"
                "请为该账号配置住宅代理（proxy 字段），或改用住宅 IP 环境运行。",
                link,
            )
        elif link.get("cloudflare"):
            result["status"] = "need_verification"
            result["message"] = message_with_site_error(
                "OAuth 过程命中 Cloudflare/WAF 人机验证，无法自动完成，请重新捕获登录态。",
                link,
            )
        else:
            result["status"] = "need_login"
            result["message"] = message_with_site_error("无法读取额度，登录态可能已失效，请重新捕获登录态。", link)
        return result

    if oauth_completed:
        current = quota_after if quota_after is not None else quota_before
        result["status"] = "already_done"
        result["message"] = f"OAuth 重登完成，额度无变化（当前 {_quota_to_usd(current)}，今日可能已发放）。"
        return result

    reason = (
        "停在第三方登录页（共享登录态可能已过期）"
        if link.get("need_human")
        else ("OAuth 授权未带 code 顺畅跳回站点" if not link.get("landed_back") else "OAuth 链路未顺畅完成")
    )
    result["status"] = "need_login"
    result["message"] = message_with_site_error(f"OAuth 自动重登未完成：{reason}。请重新捕获登录态。", link)
    return result


async def fetch_oauth_client_id(page: Any, base_url: str, provider: Any) -> tuple[str, bool]:
    """从站点状态接口读取 provider client_id 与启用开关。"""
    response = await api_get_json(page, base_url + "/api/status")
    body = response.get("body") if isinstance(response, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return "", False
    client_id = str(data.get(provider.status_client_id_field()) or "")
    raw_enabled = data.get(provider.status_oauth_field())
    if raw_enabled is None:
        enabled = bool(client_id)
    elif isinstance(raw_enabled, bool):
        enabled = raw_enabled
    else:
        enabled = str(raw_enabled).strip().lower() not in {"", "0", "false", "no", "off"}
    return client_id, enabled


async def fetch_oauth_state(page: Any, base_url: str, log: LogFn = noop) -> tuple[str, str]:
    """读取一次性 OAuth state，返回 ``(state, 诊断)``。"""
    last_diagnostic = "接口无响应"
    for attempt in range(3):
        response = await api_get_json(page, base_url + "/api/oauth/state")
        if not isinstance(response, dict):
            last_diagnostic = "接口无响应"
        else:
            status = response.get("status")
            body = response.get("body")
            oauth_state = extract_oauth_state(body)
            if oauth_state:
                return oauth_state, f"status={status}"
            last_diagnostic = f"status={status} body={short_body(body)}"
            if status not in (408, 425, 429, 500, 502, 503, 504):
                break
        if attempt < 2:
            delay = 5 * (attempt + 1)
            log(f"/api/oauth/state 暂不可用（{last_diagnostic}），等待 {delay}s 后重试...")
            await asyncio.sleep(delay)
    return "", last_diagnostic


def site_oauth_selectors(provider: Any) -> list[str]:
    """返回站点登录页上与 provider 对应的入口选择器。"""
    if provider.key == "linuxdo":
        return [
            "main button:has-text('使用 LinuxDO 继续')",
            "button:has-text('使用 LinuxDO 继续')",
            "button:has-text('Continue with LinuxDO')",
            "button:has-text('LinuxDO')",
            "button:has-text('Linux.do')",
            "button:has-text('Linux')",
            "button:has(#linuxdo_icon)",
            "text=/使用\\s*LinuxDO\\s*继续/i",
            "text=/Continue\\s+with\\s+LinuxDO/i",
            "text=/LinuxDO|Linux\\.do/i",
            "#linuxdo_icon",
        ]
    if provider.key == "github":
        return [
            "main button:has-text('使用 GitHub 继续')",
            "button:has-text('使用 GitHub 继续')",
            "button:has-text('GitHub')",
            "button:has([aria-label='github_logo'])",
            "text=/使用\\s*GitHub\\s*继续/i",
            "text=/GitHub/i",
        ]
    return DEFAULT_LOGIN_SELECTORS


async def maybe_click_with_popup(
    page: Any,
    locator: Any,
    log: LogFn,
    error_collector: dict[str, Any] | None = None,
    base_url: str = "",
) -> Any:
    """点击 OAuth 入口，并兼容新弹窗与当前页跳转。"""
    popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=10000))
    before_url = page.url

    async def _drain_popup_task() -> None:
        popup_task.cancel()
        try:
            await popup_task
        except BaseException:
            pass

    clicked = False
    click_attempts = (
        ("普通点击", lambda: locator.click(timeout=7000)),
        ("强制点击", lambda: locator.click(timeout=3000, force=True)),
        ("DOM dispatch", lambda: locator.dispatch_event("click")),
    )
    for label, click in click_attempts:
        try:
            await click()
            clicked = True
            break
        except Exception as exc:
            if is_driver_closed_error(exc):
                await _drain_popup_task()
                raise
            log(f"OAuth 入口{label}失败（{type(exc).__name__}）")

    if not clicked:
        log("OAuth 入口所有点击方式均失败，回退到直连授权 URL")
        await _drain_popup_task()
        return None

    popup = None
    try:
        popup = await popup_task
    except Exception:
        popup = None
    if popup:
        try:
            if error_collector is not None:
                install_site_error_collector(popup, base_url, error_collector)
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        log(f"站点前端已打开 OAuth 弹窗：{popup.url}")
        return popup

    await asyncio.sleep(2.5)
    if page.url != before_url:
        log(f"站点前端已跳转：{page.url}")
        return page
    log("点击后未检测到 OAuth 弹窗或跳转，可能 /api/oauth/state 被限流或按钮请求失败")
    return None


async def click_site_oauth_entry(
    page: Any,
    base_url: str,
    provider: Any,
    log: LogFn = noop,
    error_collector: dict[str, Any] | None = None,
) -> Any:
    """通过站点登录/注册界面点击 OAuth 入口。"""
    selectors = site_oauth_selectors(provider)

    async def _first_visible(selectors_to_try: list[str]) -> tuple[str, Any]:
        for selector in selectors_to_try:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0:
                    continue
                try:
                    visible = await locator.is_visible()
                except Exception as visibility_error:
                    if is_driver_closed_error(visibility_error):
                        raise
                    visible = True
                if visible:
                    return selector, locator
            except Exception as exc:
                if is_driver_closed_error(exc):
                    raise
        return "", None

    async def _dismiss_current_popups() -> None:
        closed = await popups.dismiss_popups(page)
        if closed:
            log(f"已关闭 {closed} 个公告/弹窗")
            await asyncio.sleep(0.5)

    async def _click_oauth_if_visible() -> Any:
        selector, locator = await _first_visible(selectors)
        if locator is None:
            return None
        log(f"点击站点前端 OAuth 登录入口：{selector}")
        return await maybe_click_with_popup(page, locator, log, error_collector, base_url)

    async def _try_switch_auth_panel() -> bool:
        for selector in SITE_OAUTH_TOGGLE_SELECTORS:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0:
                    continue
                try:
                    visible = await locator.is_visible()
                except Exception as visibility_error:
                    if is_driver_closed_error(visibility_error):
                        raise
                    visible = True
                if not visible:
                    continue
                before_url = page.url
                log(f"切换站点登录/注册面板以显示 OAuth 入口：{selector}")
                await locator.click(timeout=7000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(1.2)
                if page.url != before_url:
                    log(f"站点登录/注册页已切换：{page.url}")
                await wait_for_ready(page, timeout_ms=8000, log=log)
                await _dismiss_current_popups()
                return True
            except Exception as exc:
                if is_driver_closed_error(exc):
                    raise
        return False

    root = base_url.rstrip("/")
    targets = [root + "/login", root + "/register", root]
    seen: set[str] = set()
    for target in targets:
        if target in seen:
            continue
        if waf_is_blocked(page):
            log("WAF 熔断，停止逐个打开站点登录页兜底")
            break
        seen.add(target)
        try:
            current_url = page.url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            target_url = target.rstrip("/")
            if current_url != target_url:
                log(f"打开站点登录页兜底：{target}")
                await safe_goto(page, target, wait_until="domcontentloaded", timeout=30000, log=log)
            await wait_for_ready(page, timeout_ms=15000, log=log)
        except Exception as exc:
            if is_driver_closed_error(exc):
                raise
            log(f"打开登录页失败（继续尝试当前页）：{type(exc).__name__}")
        await _dismiss_current_popups()

        entry_page = await _click_oauth_if_visible()
        if entry_page is not None:
            return entry_page

        for _ in range(2):
            if not await _try_switch_auth_panel():
                break
            entry_page = await _click_oauth_if_visible()
            if entry_page is not None:
                return entry_page

    log("未找到可点击的站点前端 OAuth 登录入口")
    return None


def attach_oauth_completion_messages(
    result: dict[str, Any],
    messages: list[str],
    log: LogFn = noop,
) -> None:
    """成功回跳只保留成功提示；失败时保留完整诊断。"""
    if result.get("landed_back"):
        success = site_success_message(messages)
        if success:
            result.setdefault("site_success_message", success)
            log(f"站点成功提示：{success}")
        result.pop("site_error", None)
        result.pop("site_errors", None)
        return
    attach_site_errors(result, messages, log)


def is_oauth_callback_url(url: str, base_url: str) -> bool:
    """严格按同源与回跳特征判断 URL，拒绝字符串包含造成的伪回跳。

    「已经回到本站」本身就是回跳成立的充分条件：同源是硬门槛（provider 页永远
    不同源，不会被误判）。不能再额外要求 /console、/oauth 或 code= —— 有的站点
    callback 成功后直接 302 到业务页并把 code 去掉，实测 ABR 福利站落在
    `/checkin`，旧判据因此把一次成功的回跳报成「未跳回站点」。

    唯一要排除的是仍停留在本站的登录入口：那说明还没真正走完授权。
    """
    if not same_origin(url, base_url):
        return False
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return False
    path = parsed.path.casefold()
    # 本站的登录入口不算回跳终点（例如 /auth/<provider>/login）。
    if path.endswith("/login") or "/auth/" in path:
        query = parsed.query.casefold()
        has_code = any(part.partition("=")[0] == "code" for part in query.split("&"))
        # 但带 code 的 /auth/... 正是标准 callback，必须视为回跳成功。
        return has_code
    return True


async def finish_oauth_authorization(
    page: Any,
    base_url: str,
    provider: Any,
    result: dict[str, Any],
    log: LogFn = noop,
    error_collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """完成 provider 授权页：解验证、点击授权并等待严格同源回跳。"""
    if not await bypass.solve_cloudflare(page, log=log):
        result["cloudflare"] = True

    for marker in provider.login_markers:
        try:
            if await page.query_selector(marker):
                result["need_human"] = True
                log(f"停在 {provider.key} 登录页：共享登录态失效，请在 GUI 重新捕获 {provider.key} 登录态")
                attach_site_errors(result, await site_error_messages(page, error_collector), log)
                return result
        except Exception as exc:
            if is_driver_closed_error(exc):
                raise

    for selector in provider.approve_selectors:
        try:
            await page.wait_for_selector(selector, timeout=8000)
            button = await page.query_selector(selector)
            if button:
                log(f"点击授权按钮：{selector}")
                await button.click()
                result["clicked"] = True
                await asyncio.sleep(2)
                await bypass.solve_cloudflare(page, log=log)
                break
        except Exception as exc:
            if is_driver_closed_error(exc):
                raise
    if not result["clicked"]:
        log("未见授权按钮（可能已自动授权），继续等待回跳...")

    try:
        await page.wait_for_url(lambda url: is_oauth_callback_url(url, base_url), timeout=OAUTH_WAIT_SECONDS * 1000)
        result["landed_back"] = True
        log(f"OAuth 已跳回站点：{page.url}")
        if await is_waf_html(page):
            await solve_waf(page, base_url, log, rounds=2)
    except Exception:
        try:
            current_url = page.url
        except Exception:
            current_url = ""
        if is_oauth_callback_url(current_url, base_url):
            result["landed_back"] = True
            log(f"OAuth 回跳（超时但已在站点）：{current_url}")
        else:
            content_lower = ""
            try:
                content_lower = (await page.content()).lower()
            except Exception:
                pass
            # 不能用裸 "cloudflare" 判定：受 CF 保护的正常页面（如 Linux DO 授权页）
            # 都含该字样，会把「停在授权页」误报成「被 Cloudflare 拦截」，掩盖真实原因。
            # 统一复用 bypass 的挑战页判据（标题 / CF 容器 / 可见拦截文案）。
            title_lower = ""
            try:
                title_lower = (await page.title() or "").lower()
            except Exception:
                pass
            from .bypass import _is_cf_challenge

            if _is_cf_challenge(title_lower, content_lower):
                result["cloudflare"] = True
                log("OAuth 被 Cloudflare 拦截")
            else:
                log(f"OAuth 未跳回站点，停在：{current_url}")

    attach_oauth_completion_messages(result, await site_error_messages(page, error_collector), log)
    return result


async def trigger_oauth(
    page: Any,
    base_url: str,
    oauth_provider: str,
    log: LogFn = noop,
    error_collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """以前端入口为主、直连授权 URL 为兜底触发 OAuth。"""
    provider = oauth_providers.get_oauth_provider(oauth_provider)
    result: dict[str, Any] = {
        "clicked": False,
        "landed_back": False,
        "need_human": False,
        "cloudflare": False,
        "provider": provider.key,
    }

    if await is_waf_html(page):
        if not waf_is_blocked(page):
            await solve_waf(page, base_url, log, rounds=2)
        if waf_is_blocked(page):
            result["waf_blocked"] = True
            log("WAF 熔断，跳过 OAuth 触发（出口 IP 被持续风控）")
            attach_site_errors(result, await site_error_messages(page, error_collector), log)
            return result

    log(f"尝试通过站点前端登录页触发 {provider.key} OAuth...")
    entry_page = await click_site_oauth_entry(page, base_url, provider, log, error_collector)
    if entry_page is not None:
        result["frontend_entry"] = True
        return await finish_oauth_authorization(entry_page, base_url, provider, result, log, error_collector)
    log("站点前端 OAuth 入口未触发，回退到直连授权 URL")

    client_id, enabled = await fetch_oauth_client_id(page, base_url, provider)
    if not client_id:
        log(f"未能从 /api/status 获取 {provider.key}_client_id（站点未开启该 OAuth 或被 WAF 拦截）")
        attach_site_errors(result, await site_error_messages(page, error_collector), log)
        return result
    if not enabled:
        log(f"站点未开启 {provider.key} OAuth 登录")
        attach_site_errors(result, await site_error_messages(page, error_collector), log)
        return result
    log(f"已获取 {provider.key} client_id={client_id}")

    oauth_state, state_diagnostic = await fetch_oauth_state(page, base_url, log)
    if not oauth_state:
        result["state_error"] = state_diagnostic
        log(f"未能获取 /api/oauth/state（{state_diagnostic}）")
        attach_site_errors(result, await site_error_messages(page, error_collector), log)
        return result

    authorize_url = provider.build_authorize_url(client_id, oauth_state)
    log(f"导航到 {provider.key} 授权页：{provider.authorize_endpoint}")
    try:
        await safe_goto(page, authorize_url, wait_until="domcontentloaded", timeout=30000, log=log)
    except Exception as exc:
        if is_driver_closed_error(exc):
            result["driver_crashed"] = True
            log(f"浏览器驱动崩溃：{exc}")
        else:
            log(f"导航授权页失败：{exc}")
        add_site_error(error_collector, "exception", exc)
        attach_site_errors(result, await site_error_messages(page, error_collector), log)
        return result

    return await finish_oauth_authorization(page, base_url, provider, result, log, error_collector)


__all__ = [
    "DEFAULT_LOGIN_SELECTORS",
    "OAUTH_WAIT_SECONDS",
    "SITE_OAUTH_TOGGLE_SELECTORS",
    "api_get_json",
    "attach_oauth_completion_messages",
    "click_site_oauth_entry",
    "extract_oauth_state",
    "fetch_oauth_client_id",
    "fetch_oauth_state",
    "finish_oauth_authorization",
    "is_oauth_callback_url",
    "maybe_click_with_popup",
    "oauth_checkin_result",
    "oauth_landed",
    "site_oauth_selectors",
    "trigger_oauth",
]
