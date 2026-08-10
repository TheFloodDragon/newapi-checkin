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

from browser import bypass, oauth_flow, oauth_providers
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


async def _click_main_linuxdo(page: Any, log: Any, timeout_ms: int = 20000) -> Any | None:
    """等待主站 SPA 渲染 LinuxDO 按钮后再点击。"""
    selectors = (
        "button:has-text('使用 Linux.do 登录')",
        "button:has-text('使用 LinuxDO 登录')",
        "button:has-text('Linux.do')",
        "button:has-text('LinuxDO')",
        "[href*='/auth/oauth/linuxdo/start']",
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(1000, timeout_ms) / 1000
    while loop.time() < deadline:
        current_url = str(getattr(page, "url", "") or "").casefold()
        # 已经进入 LinuxDO/连接授权页时，不必继续等待主站按钮，交给授权处理器。
        if "linux.do" in current_url and "api.fengwind.com" not in current_url:
            return None
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if await locator.count() <= 0 or not await locator.is_visible():
                    continue
                log(f"点击 Fengwind 主站 LinuxDO 登录入口：{selector}")
                before_url = str(getattr(page, "url", "") or "")
                strategies = (
                    ("普通点击", lambda: locator.click(timeout=7000)),
                    ("强制点击", lambda: locator.click(timeout=3000, force=True)),
                    ("DOM dispatch", lambda: locator.dispatch_event("click")),
                )
                for label, click in strategies:
                    try:
                        await click()
                        return page
                    except Exception as exc:
                        try:
                            await page.wait_for_timeout(500)
                            current_url = str(getattr(page, "url", "") or "")
                        except Exception:
                            current_url = ""
                        if current_url and current_url != before_url:
                            log(f"LinuxDO 入口{label}虽等待超时，但已触发页面跳转")
                            return page
                        log(f"LinuxDO 入口{label}失败（{type(exc).__name__}）")
                continue
            except Exception:
                continue
        await page.wait_for_timeout(400)
    log("Fengwind 主站 LinuxDO 登录入口在等待窗口内未出现")
    return None


async def _login_with_linuxdo(page: Any, helpers: Any, origin: str) -> str:
    state_value = "fengwind-" + __import__("secrets").token_urlsafe(18)
    login_url = await _fetch_login_url(page, origin, state_value)
    if not login_url:
        helpers.log("Fengwind SSO 登录地址获取失败")
        return ""
    helpers.log("已获取 Fengwind SSO 地址，打开主站登录页")
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        helpers.log(f"打开 Fengwind 主站 SSO 失败：{type(exc).__name__}")
        return ""
    helpers.log("已打开 Fengwind 主站 SSO，等待 LinuxDO 登录入口")
    try:
        await bypass.solve_cloudflare(page, log=helpers.log)
    except Exception:
        pass

    # 主站可能已经有登录态并直接回跳；否则点击主站的 LinuxDO 按钮。
    entry_page = await _click_main_linuxdo(page, helpers.log)
    if entry_page is not None:
        page_for_oauth = entry_page
    else:
        page_for_oauth = page
    provider = oauth_providers.get_oauth_provider("linuxdo")
    oauth_result = {
        "clicked": False,
        "landed_back": False,
        "need_human": False,
        "cloudflare": False,
        "provider": "linuxdo",
    }
    if not str(getattr(page_for_oauth, "url", "") or "").startswith(origin):
        oauth_result = await oauth_flow.finish_oauth_authorization(
            page_for_oauth,
            origin,
            provider,
            oauth_result,
            log=helpers.log,
        )
    if oauth_result.get("need_human"):
        return ""
    # LinuxDO 页面可能被 ClickSolver 标记为 Cloudflare，但只要已经严格回跳到
    # 福利站 callback，就仍应继续交换一次 code；未回跳时才把挑战视为失败。
    if oauth_result.get("cloudflare") and not oauth_result.get("landed_back"):
        return ""
    return await _wait_for_welfare_token(page_for_oauth, origin, state_value, log=helpers.log)


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """复用共享 LinuxDO 状态，完成 Fengwind 双层 SSO 后签到。"""
    origin = helpers.resolve_url("/").rstrip("/")
    await helpers.goto("/", timeout=60000, wait_until="domcontentloaded")

    token = await _page_token(page)
    verified = bool(token and await _verify_page_token(page, origin))
    if not verified:
        helpers.log("Fengwind welfare_token 不可用，开始双层 LinuxDO SSO")
        token = await _login_with_linuxdo(page, helpers, origin)
        verified = bool(token and await _verify_page_token(page, origin))
    if not verified:
        return helpers.need_login(
            "Fengwind 福利站 LinuxDO SSO 未完成，请重新捕获 linuxdo:default 登录态",
            {"oauth_provider": "linuxdo", "target_url": origin},
        )

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
