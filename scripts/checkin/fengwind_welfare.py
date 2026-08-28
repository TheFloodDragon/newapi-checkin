#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fengwind API 福利站每日签到。

该站不是标准 NewAPI OAuth：登录需要 LinuxDO -> Fengwind 主站 -> 福利站的双层
SSO。脚本优先直接使用福利站原生 localStorage 键 welfare_token 调 /api；Token 失效时
由浏览器脚本复用共享 LinuxDO storage state 完成完整回跳。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

from browser import bypass, oauth_providers
from providers.base import (
    ApiError,
    CheckinReward,
    USER_AGENT,
    extract_message,
    http_request,
    normalize_access_token,
    normalize_base_url,
    unwrap_data,
)

SITE_LABEL = "Fengwind API 福利站"
# 状态、签到、历史与结果确认全部由本脚本调用站点原生 /api 完成；通用层不得
# 先用 Sub2API profile 猜测 /api/v1/* 端点。
OWNS_HTTP_FLOW = True
API_PREFIX = "/api"
STATUS_PATH = "/checkin/status"
CHECKIN_PATH = "/checkin"
HISTORY_PATH = "/checkin/history?limit=14"
ME_PATH = "/me"
LOGIN_URL_PATH = "/auth/login-url"
EXCHANGE_PATH = "/auth/sso/exchange"
# 运行器统一缓存键；Fengwind 页面仍要求 welfare_token，仅在页面边界转换。
INTERNAL_TOKEN_KEY = "auth_token"
SITE_TOKEN_KEY = "welfare_token"

# ── 双层 SSO 的每一跳 ────────────────────────────────────────────────────────
# 实测链路：福利站 /api/auth/login-url → 主站 /sso/continue → 主站 /login（未登录时）
# → 主站 /api/v1/auth/oauth/linuxdo/start → connect.linux.do/oauth2/authorize
# → linux.do/session/sso_provider（302 中转）→ connect.linux.do 授权同意页
# → 主站 callback → 福利站 /auth/callback?code=...
#
# 其中两跳需要动手：主站登录页要点「Continue with Linux.do」，LINUX DO Connect 的
# 同意页要点「允许」—— 实测它是 a[href^="/oauth2/approve"] 而不是 button。
MAIN_LOGIN_SELECTORS = (
    "button:has-text('Continue with Linux.do')",
    "button:has-text('使用 Linux.do 登录')",
    "button:has-text('使用 LinuxDO 登录')",
    "button:has-text('Linux.do')",
    "button:has-text('LinuxDO')",
    "[href*='/auth/oauth/linuxdo/start']",
)
CONNECT_APPROVE_SELECTORS = (
    "a[href^='/oauth2/approve']",
    "a:has-text('允许')",
)
STAGE_LABELS = {
    "welfare_callback": "福利站 callback（已带 code）",
    "welfare_home": "福利站页面（未带 code）",
    "main_login": "主站登录页",
    "main_other": "主站页面（可能已登录）",
    "connect": "LINUX DO Connect 授权页",
    "linuxdo": "linux.do SSO 中转/登录页",
    "other": "未知页面",
}
# 主站/福利站页面刚打开时 SPA 还在决定跳哪里，先给它这么久再判定「停住了」。
STAGE_SETTLE_SECONDS = 6.0
# 任一跳停滞超过这个时间就先解一次验证再刷新：实测 linux.do 的 302 中转跳会被
# Cloudflare Turnstile 挡住，干等只会耗完预算（旧实现就是这样白等 25 秒）。
STAGE_STALL_SECONDS = 8.0


def _origin_of(url: str) -> str:
    parsed = urlsplit(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _linuxdo_start_url(login_url: str) -> str:
    """主站「Continue with Linux.do」按钮背后的 OAuth 起跳地址。

    直接导航而不是点按钮：那个按钮由主站 SPA 渲染，实测在 humanize 轨迹下三种点击
    方式都会 TimeoutError（元素被动画/遮挡判为不可操作），一旦点不动就会把整个脚本
    预算耗尽。按钮本身只是 302 到这个端点，导航是等价且确定的做法。
    """
    parsed = urlsplit(str(login_url or ""))
    if not parsed.scheme or not parsed.netloc:
        return ""
    redirect = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return (
        f"{parsed.scheme}://{parsed.netloc}/api/v1/auth/oauth/linuxdo/start"
        f"?redirect={quote(redirect, safe='')}"
    )


def _sso_stage(url: str, welfare_origin: str, main_origin: str) -> str:
    """判断当前 URL 处于双层 SSO 的哪一跳（纯函数，便于直接断言）。"""
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").casefold()
    if not host:
        return "other"
    path = (parsed.path or "").casefold()
    has_code = any(part.partition("=")[0] == "code" for part in (parsed.query or "").split("&"))

    if _origin_of(url) == _origin_of(welfare_origin):
        return "welfare_callback" if has_code else "welfare_home"
    main_host = (urlsplit(main_origin).hostname or "").casefold()
    if main_host and host == main_host:
        # 主站已经把 code 发回来时 URL 会带上它；否则 /login 表示还要点 LinuxDO。
        if has_code:
            return "main_other"
        return "main_login" if path.rstrip("/").endswith("/login") or path == "/login" else "main_other"
    if host == "connect.linux.do":
        return "connect"
    if host == "linux.do" or host.endswith(".linux.do"):
        return "linuxdo"
    return "other"


@dataclass(slots=True)
class _ClientView:
    base_url: str
    access_token: str
    site: Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _status_data(value: Any) -> dict[str, Any]:
    return _as_dict(unwrap_data(value))


def _history_items(value: Any) -> list[dict[str, Any]]:
    data = _as_dict(unwrap_data(value))
    items = data.get("items")
    return [item for item in (items or []) if isinstance(item, dict)]


def _history_excerpt(value: Any) -> list[dict[str, Any]]:
    """只保留签到历史的业务字段，避免把用户资料或原始响应扩散到结果。"""
    fields = ("id", "biz_date", "amount", "floor_amount", "bonus_actual", "status", "streak_after", "tier_name")
    out: list[dict[str, Any]] = []
    for item in _history_items(value)[:14]:
        out.append({key: item.get(key) for key in fields if key in item})
    return out


def _credit_label(value: Any) -> str:
    return {
        "credited": "已入账",
        "pending_credit": "入账中",
        "credit_failed": "入账失败",
    }.get(str(value or "").strip().casefold(), str(value or "").strip())


def _today(data: dict[str, Any], fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    today = data.get("today")
    if isinstance(today, dict):
        return today
    return dict(fallback or {})


def _amount(today: dict[str, Any]) -> float | None:
    for key in ("amount", "total_amount", "rebate_amount"):
        value = _number(today.get(key))
        if value is not None:
            return value
    return None


def _message(*, already: bool, today: dict[str, Any], fallback: dict[str, Any] | None = None) -> str:
    current = dict(today or fallback or {})
    amount = _amount(current)
    prefix = "今日已签到" if already else "签到成功"
    if amount is None:
        return prefix
    credit = _credit_label(current.get("status"))
    suffix = f"（{credit}）" if credit else ""
    return f"{prefix}，获得 ${amount:.2f}{suffix}"


def _safe_status_detail(
    status: dict[str, Any],
    level: dict[str, Any],
    history: Any,
    action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = _today(status, action)
    keys = (
        "enabled",
        "checked_in_today",
        "amount_floor",
        "amount_cap",
        "current_streak",
        "longest_streak",
        "biz_date",
        "next_reset_at",
        "checkin_eligible",
        "checkin_qualification",
        "linuxdo_trust_level",
    )
    detail: dict[str, Any] = {key: status.get(key) for key in keys if key in status}
    detail.update(
        {
            "today": {
                key: today.get(key)
                for key in (
                    "amount",
                    "floor_amount",
                    "bonus_actual",
                    "tier_name",
                    "status",
                )
                if key in today
            },
            "history": _history_excerpt(history),
        }
    )
    if level:
        detail.update(
            {
                "welfare_level": level.get("profile", {}).get("level")
                if isinstance(level.get("profile"), dict)
                else level.get("level"),
                "checkin_eligible": level.get("checkin_eligible"),
                "checkin_qualification": level.get("checkin_qualification"),
                "linuxdo_trust_level": level.get("linuxdo_trust_level"),
            }
        )
    return {key: value for key, value in detail.items() if value is not None}


def _request(client: Any, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    base_url = normalize_base_url(str(getattr(client, "base_url", "") or ""))
    token = normalize_access_token(str(getattr(client, "access_token", "") or ""))
    if not base_url:
        raise ApiError(None, None, f"{SITE_LABEL}未配置站点地址")
    if not token:
        raise ApiError(401, {"reason": "WELFARE_TOKEN_MISSING"}, f"{SITE_LABEL}缺少 welfare_token")

    url = f"{base_url}{API_PREFIX}{path}"
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {token}",
        "Referer": f"{base_url}/",
    }
    raw_body: bytes | None = None
    if method.upper() in {"POST", "PUT", "PATCH"}:
        headers["Content-Type"] = "application/json"
        raw_body = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    payload = http_request(
        url,
        method=method,
        headers=headers,
        body=raw_body,
        proxy=str(getattr(getattr(client, "site", None), "proxy", "") or ""),
        retry_non_idempotent=False,
        verify_ssl=bool(getattr(getattr(client, "site", None), "verify_ssl", True)),
    )
    if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
        raise ApiError(None, payload, extract_message(payload))
    return payload


def _optional_request(client: Any, method: str, path: str) -> Any:
    try:
        return _request(client, method, path)
    except ApiError:
        return None


def _already_from_status(
    status: dict[str, Any],
    level: dict[str, Any],
    history: Any,
    *,
    raw: Any = None,
) -> CheckinReward:
    today = _today(status)
    return CheckinReward(
        already_done=True,
        raw=raw if raw is not None else status,
        extra={
            "result_message": _message(already=True, today=today),
            "completion_signal": "welfare_status",
            "welfare_status": _safe_status_detail(status, level, history),
        },
    )


def _is_already_error(exc: ApiError) -> bool:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    text = " ".join(
        str(value or "")
        for value in (exc.message, payload.get("message"), payload.get("reason"))
    ).casefold()
    return any(marker in text for marker in ("already", "已签到", "今日已", "checked_in_today"))


def do_checkin(client: Any, log: Any = None) -> CheckinReward:
    """执行 Fengwind 福利站每日签到并返回可展示的状态记录。"""
    _log = log if callable(log) else (lambda _message: None)
    _log("读取 Fengwind 福利站签到状态")
    before_raw = _request(client, "GET", STATUS_PATH)
    before = _status_data(before_raw)
    level = _status_data(_optional_request(client, "GET", "/level"))
    history_raw = _optional_request(client, "GET", HISTORY_PATH)

    if before.get("checked_in_today") is True:
        reward = _already_from_status(before, level, history_raw, raw=before_raw)
        _log(str(reward.extra["result_message"]))
        return reward
    if before.get("enabled") is False:
        raise ApiError(400, before_raw, f"{SITE_LABEL}签到功能暂未开放")
    if level.get("checkin_eligible") is False:
        qualification = level.get("checkin_qualification") or "资格不足"
        raise ApiError(403, level, f"{SITE_LABEL}当前不具备签到资格（{qualification}）")

    _log("今日尚未签到，调用 Fengwind 福利站签到接口")
    try:
        action_raw = _request(client, "POST", CHECKIN_PATH, {})
    except ApiError as exc:
        # POST 发生网络/服务端不确定错误时，先读状态确认服务端是否已经记账。
        after_raw = _optional_request(client, "GET", STATUS_PATH)
        after = _status_data(after_raw)
        if after.get("checked_in_today") is True or _is_already_error(exc):
            reward = _already_from_status(after or before, level, history_raw, raw=after_raw or before_raw)
            _log(str(reward.extra["result_message"]))
            return reward
        raise

    action = _as_dict(unwrap_data(action_raw))
    after_raw = _request(client, "GET", STATUS_PATH)
    after = _status_data(after_raw)
    history_after = _optional_request(client, "GET", HISTORY_PATH)
    today = _today(after, action)
    amount = _amount(today)
    action_status = str(action.get("status") or today.get("status") or "").casefold()
    confirmed = after.get("checked_in_today") is True or action_status in {
        "credited",
        "pending_credit",
        "credit_failed",
    }
    if not confirmed:
        raise ApiError(
            None,
            {"status": after, "action": action},
            f"{SITE_LABEL}签到接口返回成功，但状态接口未确认签到结果",
            transient=True,
        )

    detail = _safe_status_detail(after, level, history_after, action)
    detail.update(
        {
            "result_message": _message(already=False, today=today, fallback=action),
            "completion_signal": "welfare_checkin_response",
            "response_status": action.get("status"),
        }
    )
    reward = CheckinReward(
        already_done=False,
        quota_awarded=amount,
        raw=action_raw,
        extra=detail,
    )
    _log(str(detail["result_message"]))
    return reward


async def _page_token(page: Any) -> str:
    """读取统一 auth_token，并在福利站页面边界转换为 welfare_token。"""
    js = f"""() => {{
        const internal = String(localStorage.getItem({INTERNAL_TOKEN_KEY!r}) || '');
        const site = String(localStorage.getItem({SITE_TOKEN_KEY!r}) || '');
        if (!internal && site) localStorage.setItem({INTERNAL_TOKEN_KEY!r}, site);
        if (internal && !site) localStorage.setItem({SITE_TOKEN_KEY!r}, internal);
        return internal || site || '';
    }}"""
    try:
        value = await page.evaluate(js)
    except Exception:
        return ""
    return normalize_access_token(str(value or ""))


async def _verify_page_token(page: Any, origin: str) -> bool:
    js = """async ([baseUrl, token]) => {
        try {
            const response = await fetch(baseUrl + '/api/me', {
                credentials: 'include',
                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
            });
            return Boolean(response.ok);
        } catch (_) {
            return false;
        }
    }"""
    token = await _page_token(page)
    if not token:
        return False
    try:
        return bool(await page.evaluate(js, [origin, token]))
    except Exception:
        return False


async def _fetch_login_url(page: Any, origin: str, state_value: str) -> str:
    js = """async ([baseUrl, state]) => {
        try {
            const response = await fetch(baseUrl + '/api/auth/login-url?state=' + encodeURIComponent(state), {
                credentials: 'include',
                headers: { Accept: 'application/json' },
            });
            const raw = await response.json();
            const data = raw && raw.data ? raw.data : raw;
            return response.ok && data ? String(data.login_url || '') : '';
        } catch (_) {
            return '';
        }
    }"""
    try:
        result = await page.evaluate(js, [origin, state_value])
    except Exception:
        return ""
    return str(result or "").strip()


async def _exchange_callback(page: Any, origin: str, expected_state: str) -> dict[str, Any]:
    js = """async ([baseUrl, expectedState]) => {
        try {
            const url = new URL(location.href);
            const code = String(url.searchParams.get('code') || '');
            const state = String(url.searchParams.get('state') || '');
            if (!code) return { ok: false, stage: 'missing_code' };
            if (state && state !== expectedState) return { ok: false, stage: 'state_mismatch' };
            const response = await fetch(baseUrl + '/api/auth/sso/exchange', {
                method: 'POST',
                credentials: 'include',
                headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                body: JSON.stringify({ code, state: state || expectedState }),
            });
            const raw = await response.json();
            const data = raw && raw.data ? raw.data : raw;
            const token = data && String(data.access_token || '');
            if (!response.ok || !token) {
                return {
                    ok: false,
                    stage: 'exchange_rejected',
                    status: response.status,
                    message: String(raw && raw.message || '').slice(0, 120),
                };
            }
            // 福利站前端要求 welfare_token；运行器统一缓存键使用 auth_token。
            localStorage.setItem('welfare_token', token);
            localStorage.setItem('auth_token', token);
            return { ok: true, stage: 'exchanged' };
        } catch (error) {
            return { ok: false, stage: String(error && error.name || 'exchange_error') };
        }
    }"""
    try:
        result = await page.evaluate(js, [origin, expected_state])
    except Exception as exc:
        return {"ok": False, "stage": type(exc).__name__}
    return result if isinstance(result, dict) else {"ok": False, "stage": "invalid_result"}


async def _wait_for_welfare_token(
    page: Any,
    origin: str,
    state_value: str,
    log: Any = None,
    timeout_ms: int = 30000,
) -> str:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1000, timeout_ms) / 1000
    last_stage = ""
    while loop.time() < deadline:
        token = await _page_token(page)
        if token and await _verify_page_token(page, origin):
            return token
        exchange = await _exchange_callback(page, origin, state_value)
        stage = str(exchange.get("stage") or "")
        if stage and stage != last_stage and callable(log):
            log(f"Fengwind callback Token 交换阶段：{stage}")
            if exchange.get("message"):
                log(f"Fengwind callback 返回：{str(exchange['message'])[:120]}")
            last_stage = stage
        await page.wait_for_timeout(500)
    return ""


async def _click_first_visible(page: Any, selectors: tuple[str, ...], log: Any) -> str:
    """点击第一个可见的候选元素，返回命中的选择器；都不可见返回空串。"""
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() <= 0 or not await locator.is_visible():
                continue
        except Exception:
            continue
        before_url = str(getattr(page, "url", "") or "")
        for label, click in (
            ("普通点击", lambda: locator.click(timeout=5000)),
            ("强制点击", lambda: locator.click(timeout=3000, force=True)),
            ("DOM dispatch", lambda: locator.dispatch_event("click")),
        ):
            try:
                await click()
                return selector
            except Exception as exc:
                try:
                    await page.wait_for_timeout(400)
                    moved = str(getattr(page, "url", "") or "") != before_url
                except Exception:
                    moved = False
                if moved:
                    if callable(log):
                        log(f"{selector} {label}报超时但页面已跳转")
                    return selector
                if callable(log):
                    log(f"{selector} {label}失败（{type(exc).__name__}）")
    return ""


async def _has_any_selector(page: Any, selectors: tuple[str, ...] | list[str]) -> bool:
    for selector in selectors:
        try:
            if await page.locator(selector).first.count() > 0:
                return True
        except Exception:
            continue
    return False


async def _drive_sso_chain(
    page: Any,
    helpers: Any,
    origin: str,
    login_url: str,
    state_value: str,
    budget_seconds: float,
) -> dict[str, Any]:
    """按当前所处的一跳逐步推进双层 SSO，直到福利站回调带回 code。

    返回 ``{"token": str, "reason": str, "stage": str}``：reason 为空表示成功，
    ``provider_login`` 表示停在 linux.do 登录页（共享登录态失效），``timeout``
    表示预算内没能走完，stage 指出最后停在哪一跳 —— 这是排查的关键信息，
    旧实现只会回一句「SSO 未完成」。
    """
    provider = oauth_providers.get_oauth_provider("linuxdo")
    main_origin = _origin_of(login_url)
    start_url = _linuxdo_start_url(login_url)
    approve_selectors = tuple(provider.approve_selectors) + CONNECT_APPROVE_SELECTORS
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(20.0, budget_seconds)

    last_stage = ""
    stale_url = ""
    stale_since = loop.time()
    reopen_left = 3      # 重新发起 /sso/continue 的次数（主站登录完成后要靠它带回 code）
    nudge_left = 2       # 对卡住的 302 中转跳做「解 CF + 刷新」的次数
    start_left = 2       # 直连主站 linuxdo start 端点的次数
    exchange_tried = 0

    while loop.time() < deadline:
        url = str(getattr(page, "url", "") or "")
        stage = _sso_stage(url, origin, main_origin)
        if stage != last_stage:
            helpers.log(f"SSO 当前位置：{STAGE_LABELS.get(stage, stage)}")
            last_stage = stage
            stale_url = url
            stale_since = loop.time()
        elif url != stale_url:
            stale_url = url
            stale_since = loop.time()
        stale_for = loop.time() - stale_since

        if stage == "welfare_callback":
            exchange_tried += 1
            token = await _wait_for_welfare_token(
                page, origin, state_value, log=helpers.log, timeout_ms=15000
            )
            if token:
                return {"token": token, "reason": "", "stage": stage}
            if exchange_tried >= 2 or reopen_left <= 0:
                return {"token": "", "reason": "exchange_failed", "stage": stage}
            reopen_left -= 1
            helpers.log("callback 未能换出 welfare_token，重新发起主站 SSO")
            await _safe_goto(page, login_url, helpers.log)
            continue

        if stage == "main_login":
            # 主站要求登录：直连 OAuth 起跳端点，点按钮只作兜底。
            if start_url and start_left > 0:
                start_left -= 1
                helpers.log("主站要求登录，直连 LinuxDO OAuth 起跳端点")
                await _safe_goto(page, start_url, helpers.log)
                await page.wait_for_timeout(1200)
                continue
            selector = await _click_first_visible(page, MAIN_LOGIN_SELECTORS, helpers.log)
            if selector:
                helpers.log(f"已点击主站 LinuxDO 登录入口：{selector}")
                await page.wait_for_timeout(1500)
                continue
            await page.wait_for_timeout(600)
            continue

        if stage in {"welfare_home", "main_other"}:
            # 主站页面刚打开时 SPA 还没决定跳哪里，先给它几秒；确实停住了才重新发起
            # /sso/continue —— 主站已登录时靠这一步把 code 发回福利站，旧实现完全没有，
            # 主站登录成功后链路就断在这里。
            if stale_for < STAGE_SETTLE_SECONDS:
                await page.wait_for_timeout(700)
                continue
            if reopen_left <= 0:
                return {"token": "", "reason": "timeout", "stage": stage}
            reopen_left -= 1
            helpers.log("重新发起主站 /sso/continue 以换取福利站 code")
            await _safe_goto(page, login_url, helpers.log)
            await page.wait_for_timeout(1500)
            continue

        if stage == "connect":
            selector = await _click_first_visible(page, approve_selectors, helpers.log)
            if selector:
                helpers.log(f"已在 LINUX DO Connect 点击授权：{selector}")
                await page.wait_for_timeout(1500)
                continue
            await page.wait_for_timeout(600)

        if stage == "linuxdo":
            if await _has_any_selector(page, provider.login_markers):
                return {"token": "", "reason": "provider_login", "stage": stage}

        # 同一个页面长时间没有进展：先给 Cloudflare 一次机会，再刷新这一跳。
        # linux.do 的 /session/sso_provider 正常只是 302，停在那里说明这一跳被
        # 挡住或超时，干等到底只会耗完预算。
        if stale_for > STAGE_STALL_SECONDS:
            if nudge_left > 0:
                nudge_left -= 1
                helpers.log(f"{STAGE_LABELS.get(stage, stage)} 停滞，尝试解验证并重试该跳")
                try:
                    await bypass.solve_cloudflare(page, log=helpers.log)
                except Exception:
                    pass
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=30000)
                except Exception:
                    pass
                stale_since = loop.time()
                continue
            if stage in {"linuxdo", "connect", "other"} and reopen_left > 0:
                reopen_left -= 1
                helpers.log(f"{STAGE_LABELS.get(stage, stage)} 仍无进展，从主站 SSO 重新开始")
                await _safe_goto(page, login_url, helpers.log)
                stale_since = loop.time()
                continue

        await page.wait_for_timeout(700)

    return {"token": "", "reason": "timeout", "stage": last_stage or "other"}


async def _safe_goto(page: Any, url: str, log: Any) -> None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        if callable(log):
            log(f"打开 {url[:80]} 失败：{type(exc).__name__}")


async def _login_with_linuxdo(page: Any, helpers: Any, origin: str) -> dict[str, Any]:
    """完成 福利站 → 主站 → LINUX DO Connect → linux.do 的双层 SSO。"""
    state_value = "fengwind-" + __import__("secrets").token_urlsafe(18)
    login_url = await _fetch_login_url(page, origin, state_value)
    if not login_url:
        helpers.log("Fengwind SSO 登录地址获取失败")
        return {"token": "", "reason": "login_url_missing", "stage": "welfare_home"}
    helpers.log("已获取 Fengwind SSO 地址，打开主站登录页")
    await _safe_goto(page, login_url, helpers.log)
    try:
        await bypass.solve_cloudflare(page, log=helpers.log)
    except Exception:
        pass

    remaining = helpers.remaining_seconds()
    # 留出签到 API 的时间预算；拿不到剩余预算时按 100s 走（脚本默认超时 240s）。
    budget = 100.0 if remaining is None else max(20.0, min(150.0, remaining - 45.0))
    return await _drive_sso_chain(page, helpers, origin, login_url, state_value, budget)


SSO_FAILURE_MESSAGES = {
    "provider_login": (
        "共享 linuxdo 登录态已失效（浏览器停在 linux.do 登录页），"
        "请在管理界面重新捕获 linuxdo:default 登录态"
    ),
    "login_url_missing": (
        "Fengwind 福利站未下发 SSO 登录地址（/api/auth/login-url 无响应或被拦截），"
        "站点可能临时不可用，请稍后重试"
    ),
    "exchange_failed": (
        "已回到 Fengwind 福利站 callback，但 code 换取 welfare_token 失败；"
        "请确认账号已在 Fengwind 主站完成 LinuxDO 绑定"
    ),
}


def _sso_failure_message(outcome: dict[str, Any]) -> str:
    reason = str(outcome.get("reason") or "timeout")
    stage = STAGE_LABELS.get(str(outcome.get("stage") or ""), "未知位置")
    known = SSO_FAILURE_MESSAGES.get(reason)
    if known:
        return known
    return (
        f"Fengwind 双层 SSO 未在预算内走完（最后停在{stage}）。"
        "该链路需要先登录 Fengwind 主站再换取福利站 Token；"
        "若反复停在同一跳，请重新捕获 linuxdo:default 登录态或稍后重试"
    )


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """复用共享 LinuxDO 状态，完成 Fengwind 双层 SSO 后签到。"""
    origin = helpers.resolve_url("/").rstrip("/")
    await helpers.goto("/", timeout=60000, wait_until="domcontentloaded")

    token = await _page_token(page)
    verified = bool(token and await _verify_page_token(page, origin))
    outcome: dict[str, Any] = {}
    if not verified:
        helpers.log("Fengwind welfare_token 不可用，开始双层 LinuxDO SSO")
        outcome = await _login_with_linuxdo(page, helpers, origin)
        token = str(outcome.get("token") or "")
        verified = bool(token and await _verify_page_token(page, origin))
    if not verified:
        # 失败原因必须落到具体那一跳：旧实现无论卡在主站登录页、授权同意页还是
        # linux.do 中转，都只回同一句「请重新捕获登录态」，而这三种情况该做的事完全不同。
        detail = {
            "oauth_provider": "linuxdo",
            "target_url": origin,
            "sso_stage": outcome.get("stage") or "",
            "sso_reason": outcome.get("reason") or "",
        }
        return helpers.need_login(_sso_failure_message(outcome), detail)

    helpers.log("Fengwind 福利站登录态验证成功，执行签到 API")
    client = _ClientView(base_url=origin, access_token=token, site=site)
    try:
        reward = do_checkin(client, log=helpers.log)
    except ApiError as exc:
        status = int(exc.status or 0)
        detail = {"auth_verified": True, "response_status": status, "target_url": origin}
        if status in {401, 403} and "资格" not in str(exc.message):
            return helpers.need_login(str(exc.message), detail)
        return helpers.error(str(exc.message), detail)
    detail = dict(reward.extra)
    detail.update({"auth_verified": True, "oauth_provider": "linuxdo", "checkin_source": "browser_api"})
    message = str(detail.get("result_message") or "签到完成")
    if reward.already_done:
        return helpers.already_done(message, detail, quota_is_usd=True)
    return helpers.success(
        message,
        detail,
        awarded=reward.quota_awarded,
        quota_is_usd=True,
    )
