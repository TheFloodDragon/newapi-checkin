#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkin.new-api.abrdns.com 福利站 Linux DO OAuth 签到脚本。

该站是独立的 FastAPI 福利站，不提供 New API 的 /api/status、/api/oauth/state 或
/api/user/self 接口。脚本因此自行完成：

1. 复用运行器注入的共享 Linux DO storage state；
2. 访问本站 /auth/linuxdo/login，跟随正常 OAuth 回调建立本站 session；
3. 读取 /checkin 页面并提交真实签到表单；
4. 页面要求 hCaptcha 时调用仓库现有的 browser.hcaptcha 视觉求解器；
5. 对成功/已签到/需验证做明确分类，不把页面打开误报为签到成功。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from browser import oauth_flow, oauth_providers

SITE_LABEL = "ABR 福利站"
OWNS_HTTP_FLOW = True
LOGIN_PATH = "/auth/linuxdo/login"
CHECKIN_PATH = "/checkin"
HISTORY_PATH = "/history?page=1"

_LOGIN_MARKERS = (
    "使用 Linux DO 登录",
    "使用 LinuxDO 登录",
    "登录福利站",
)
_AUTHENTICATED_MARKERS = (
    "退出登录",
    "今日签到",
    "签到记录",
    "当前余额",
    "签到成功",
    "今日已签到",
    "已签到",
)
_ALREADY_MARKERS = (
    "今日已签到",
    "今日已完成",
    "已经签到",
    "已完成",
)
_SUCCESS_MARKERS = (
    "签到成功",
    "签到完成",
    "签到成功！",
    "签到成功。",
)
_CAPTCHA_MARKERS = (
    "hcaptcha",
    "h-captcha",
    "人机验证",
    "验证码",
    "验证失败",
    "请完成验证",
)
_AMOUNT_PATTERNS = (
    r"(?:获得|奖励|增加|到账|发放)[^\d$￥¥]{0,24}[$￥¥]?\s*([\d,]+(?:\.\d+)?)",
    r"[$￥¥]\s*([\d,]+(?:\.\d+)?)",
)


def _origin(site: Any, helpers: Any) -> str:
    base = str(getattr(site, "base_url", "") or "").strip()
    return helpers.resolve_url("/").rstrip("/") if base else ""


def _short_text(text: Any, limit: int = 500) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[:limit] + "…"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    return any(marker.casefold() in lowered for marker in markers)


def _extract_amount(text: str) -> float | None:
    """从成功页面的奖励文案中提取金额；没有明确奖励文案时返回 None。"""
    for pattern in _AMOUNT_PATTERNS:
        match = re.search(pattern, text or "", flags=re.IGNORECASE)
        if not match:
            continue
        try:
            return float(match.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
    return None


def _is_site_origin(url: str, origin: str) -> bool:
    try:
        left = urlsplit(str(url or ""))
        right = urlsplit(str(origin or ""))
    except ValueError:
        return False
    return bool(
        left.scheme.lower() == right.scheme.lower()
        and left.hostname
        and right.hostname
        and left.hostname.lower() == right.hostname.lower()
        and (left.port or (443 if left.scheme.lower() == "https" else 80))
        == (right.port or (443 if right.scheme.lower() == "https" else 80))
    )


async def _body_text(page: Any) -> str:
    try:
        body = page.locator("body").first
        return str(await body.inner_text() or "")
    except Exception:
        try:
            return str(await page.text_content("body") or "")
        except Exception:
            return ""


async def _has_visible_login(page: Any) -> bool:
    for marker in _LOGIN_MARKERS:
        try:
            locator = page.get_by_text(marker, exact=False).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue
    try:
        login_link = page.locator("a[href='/auth/linuxdo/login']").first
        return await login_link.count() > 0 and await login_link.is_visible()
    except Exception:
        return False


async def _has_checkin_form(page: Any) -> bool:
    selectors = (
        "form[action='/checkin']",
        "form[action$='/checkin']",
        "button:has-text('签到')",
        "input[type='submit']",
    )
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.count() > 0 and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _captcha_token(page: Any) -> str:
    """读取页面已经签发的 hCaptcha token，不主动解算或点击挑战。"""
    script = """() => {
        const selectors = [
            'textarea[name="h-captcha-response"]',
            'input[name="h-captcha-response"]',
        ];
        for (const selector of selectors) {
            for (const node of document.querySelectorAll(selector)) {
                const value = String(node.value || node.textContent || '').trim();
                if (value) return value;
            }
        }
        return '';
    }"""
    try:
        return str(await page.evaluate(script) or "").strip()
    except Exception:
        return ""


async def _challenge_present(page: Any) -> bool:
    try:
        iframe = page.locator("iframe[src*='hcaptcha.com'], iframe[title*='hCaptcha']").first
        if await iframe.count() > 0 and await iframe.is_visible():
            return True
    except Exception:
        pass
    text = await _body_text(page)
    return _contains_any(text, _CAPTCHA_MARKERS)


async def _site_session_cookie_present(context: Any, origin: str) -> bool:
    """判断本站 OAuth 回调是否已经创建会话 Cookie。"""
    if callable(context):
        try:
            context = context()
        except Exception:
            context = None
    if context is None:
        return False
    try:
        cookies = await context.cookies(origin)
    except Exception:
        try:
            cookies = await context.cookies()
        except Exception:
            return False
    if not isinstance(cookies, list):
        return False
    auth_names = {"session", "sessionid", "access_token", "auth_token", "token"}
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "").strip().casefold()
        value = str(cookie.get("value") or "").strip()
        domain = str(cookie.get("domain") or "").strip()
        if not value or (domain and not _is_site_origin(f"https://{domain.lstrip('.')}", origin)):
            continue
        if name in auth_names or any(marker in name for marker in ("session", "auth", "token")):
            return True
    return False


async def _site_logged_in(page: Any, origin: str, context: Any = None) -> bool:
    if not _is_site_origin(str(getattr(page, "url", "") or ""), origin):
        return False
    if await _has_visible_login(page):
        return False
    if await _has_checkin_form(page):
        return True
    text = await _body_text(page)
    if _contains_any(text, _AUTHENTICATED_MARKERS) and not _contains_any(text, _LOGIN_MARKERS):
        return True
    return await _site_session_cookie_present(context, origin)


async def _open_checkin(page: Any, helpers: Any, origin: str) -> str:
    await helpers.goto(CHECKIN_PATH, timeout=60000, wait_until="domcontentloaded")
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    return await _body_text(page)


async def _oauth_login(page: Any, helpers: Any, origin: str) -> dict[str, Any]:
    provider = oauth_providers.get_oauth_provider("linuxdo")
    helpers.log("当前本站会话不可用，打开 ABR 福利站 Linux DO 登录入口")
    try:
        await helpers.goto(LOGIN_PATH, timeout=60000, wait_until="domcontentloaded")
    except Exception as exc:
        return {"ok": False, "stage": "site_login_navigation", "error": type(exc).__name__}

    current = str(getattr(page, "url", "") or "")
    if _is_site_origin(current, origin):
        if await _site_logged_in(page, origin, getattr(page, "context", None)):
            return {"ok": True, "stage": "already_authenticated"}
        return {"ok": False, "stage": "site_login_not_redirected"}

    result: dict[str, Any] = {
        "clicked": False,
        "landed_back": False,
        "need_human": False,
        "cloudflare": False,
        "provider": provider.key,
    }
    try:
        result = await oauth_flow.finish_oauth_authorization(
            page,
            origin,
            provider,
            result,
            log=helpers.log,
        )
    except Exception as exc:
        return {
            "ok": False,
            "stage": "oauth_exception",
            "error": type(exc).__name__,
            **{key: value for key, value in result.items() if key in {"landed_back", "need_human", "cloudflare"}},
        }

    # 该站 callback 成功后会立即 302 到 /checkin，不保留 code/state，也不带
    # /oauth 或 /console 路径。通用 OAuth 判定因此可能显示 landed_back=False，
    # 这里以本站会话是否已经建立作为更可靠的最终判据。
    if not result.get("landed_back"):
        current_url = str(getattr(page, "url", "") or "")
        if _is_site_origin(current_url, origin):
            try:
                await helpers.goto(CHECKIN_PATH, timeout=60000, wait_until="domcontentloaded")
            except Exception:
                pass
            if await _site_logged_in(page, origin, getattr(page, "context", None)):
                result["landed_back"] = True
                result["callback_redirected_to_checkin"] = True
            else:
                return {"ok": False, "stage": "oauth_not_landed", **_safe_oauth_detail(result)}
        else:
            return {"ok": False, "stage": "oauth_not_landed", **_safe_oauth_detail(result)}
    try:
        await helpers.goto(CHECKIN_PATH, timeout=60000, wait_until="domcontentloaded")
    except Exception:
        pass
    verified = await _site_logged_in(page, origin, getattr(page, "context", None))
    return {
        "ok": verified,
        "stage": "oauth_callback_verified" if verified else "callback_session_missing",
        **_safe_oauth_detail(result),
    }


def _safe_oauth_detail(result: dict[str, Any]) -> dict[str, Any]:
    allowed = ("clicked", "landed_back", "need_human", "cloudflare", "provider", "frontend_entry")
    return {key: result.get(key) for key in allowed if key in result}


async def _submit_checkin(page: Any, helpers: Any) -> dict[str, Any]:
    token = await _captcha_token(page)
    solve_detail: dict[str, Any] = {}
    if await _challenge_present(page) and not token:
        helpers.log("ABR 福利站检测到 hCaptcha，调用内置视觉求解器")
        try:
            # 本站 widget 挂载偏慢（实测 iframe 内部控件需 10s 以上才可定位），
            # 放宽挂载与单轮预算，否则会在挑战出现前就按超时结束。
            #
            # round_timeout_ms 必须容纳一次完整视觉请求：求解器按「单轮预算 - 1s」
            # 设置模型请求超时，若单轮预算小于视觉端点自身超时（默认 60s），远端还
            # 没返回就会被切断，结果报成「单轮求解超时」而掩盖真实错误（如 HTTP 424）。
            solve = await helpers.solve_hcaptcha(
                options={
                    "presence_timeout_ms": 20_000,
                    "widget_mount_timeout_ms": 40_000,
                    "post_action_wait_ms": 12_000,
                    "round_timeout_ms": 75_000,
                    "total_timeout_ms": 180_000,
                }
            )
        except Exception as exc:
            solve = None
            solve_detail["solver_error"] = type(exc).__name__
        if solve is not None:
            solve_detail.update(
                {
                    "captcha": "hcaptcha",
                    "captcha_status": str(getattr(solve, "status", "") or ""),
                    "captcha_rounds": int(getattr(solve, "rounds", 0) or 0),
                    "captcha_type": str(getattr(solve, "challenge_type", "") or ""),
                }
            )
            failure_stage = str(getattr(solve, "failure_stage", "") or "")
            if failure_stage:
                solve_detail["captcha_failure_stage"] = failure_stage
            error_type = str(getattr(solve, "error_type", "") or "")
            if error_type:
                solve_detail["captcha_error_type"] = error_type
            http_status = getattr(solve, "http_status", None)
            if isinstance(http_status, int):
                solve_detail["captcha_http_status"] = http_status
            screenshot = str(getattr(solve, "screenshot", "") or "")
            if screenshot:
                solve_detail["screenshot"] = screenshot
            token = str(getattr(solve, "token", "") or "").strip() or await _captcha_token(page)
        if not token:
            solve_detail["completion_signal"] = "captcha_required"
            return {
                "status": "need_verification",
                "message": f"{SITE_LABEL} hCaptcha 未能完成：{str(getattr(solve, 'message', '') or '未取得验证令牌')}",
                "detail": solve_detail,
            }

    # 只接受明确指向 /checkin 的表单。该页还有一个 action=/auth/logout 的表单，
    # 宽松的 "form" 回退会先命中它，点下去等于把账号登出（实测该表单确实存在）。
    form = None
    for selector in ("form[action='/checkin']", "form[action$='/checkin']"):
        try:
            candidate = page.locator(selector).first
            if await candidate.count() > 0 and await candidate.is_visible():
                form = candidate
                break
        except Exception:
            continue
    if form is None:
        return {"status": "error", "message": "未找到 ABR 福利站签到表单", "detail": {}}

    submit = None
    for selector in ("#checkin-submit", "button[type='submit']", "input[type='submit']"):
        try:
            candidate = form.locator(selector).first
            if await candidate.count() > 0 and await candidate.is_visible():
                submit = candidate
                break
        except Exception:
            continue
    if submit is None:
        return {"status": "error", "message": "未找到 ABR 福利站签到按钮", "detail": {}}

    # 按钮在验证完成前是 disabled（页面显示「请先进行验证」）。此时点击不会提交，
    # 必须明确报 need_verification，否则会把「点了但没提交」误判为签到已提交。
    try:
        if await submit.is_disabled():
            return {
                "status": "need_verification",
                "message": f"{SITE_LABEL}签到按钮在验证完成前不可用，需要先完成 hCaptcha",
                "detail": {**solve_detail, "completion_signal": "submit_disabled"},
            }
    except Exception:
        pass

    try:
        await submit.click(timeout=10000)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"点击 ABR 福利站签到按钮失败：{type(exc).__name__}",
            "detail": {"completion_signal": "submit_click"},
        }
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await page.wait_for_timeout(800)
    text = await _body_text(page)
    return {"status": "page_result", "text": text, "token_used": bool(token), "detail": solve_detail}


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """复用当前 Linux DO OAuth storage state，完成 ABR 福利站签到。"""
    origin = _origin(site, helpers)
    if not origin:
        return helpers.need_config("ABR 福利站未配置有效站点地址")

    text = await _open_checkin(page, helpers, origin)
    oauth_detail: dict[str, Any] = {}
    if not await _site_logged_in(page, origin, context):
        login = await _oauth_login(page, helpers, origin)
        oauth_detail = _safe_oauth_detail(login)
        if not login.get("ok"):
            detail = {"oauth_provider": "linuxdo", "auth_verified": False, **oauth_detail}
            return helpers.need_login(
                "ABR 福利站 Linux DO OAuth 登录未完成，请检查当前 OAuth 登录态",
                detail,
            )
        text = await _open_checkin(page, helpers, origin)

    auth_detail = {
        "auth_verified": True,
        "oauth_provider": "linuxdo",
        "checkin_source": "browser_script",
        "target_url": origin,
        **oauth_detail,
    }

    if _contains_any(text, _ALREADY_MARKERS) and not await _has_checkin_form(page):
        amount = _extract_amount(text)
        if amount is not None:
            auth_detail["today_reward"] = amount
        auth_detail["completion_signal"] = "already_text"
        return helpers.already_done("ABR 福利站今日已签到", auth_detail, quota=amount, quota_is_usd=True)

    if not await _has_checkin_form(page):
        if _contains_any(text, _CAPTCHA_MARKERS):
            screenshot = await helpers.screenshot("abrdns-welfare-hcaptcha-required.png")
            if screenshot:
                auth_detail["screenshot"] = screenshot
            auth_detail["completion_signal"] = "captcha_required"
            return helpers.need_verification(
                f"{SITE_LABEL}页面需要 hCaptcha，请在浏览器中完成验证后重试",
                auth_detail,
            )
        screenshot = await helpers.screenshot("abrdns-welfare-checkin-page-unrecognized.png")
        if screenshot:
            auth_detail["screenshot"] = screenshot
        return helpers.error("ABR 福利站登录成功，但未识别到签到页面", auth_detail)

    submitted = await _submit_checkin(page, helpers)
    if submitted.get("status") == "need_verification":
        auth_detail.update(submitted.get("detail") or {})
        return helpers.need_verification(str(submitted.get("message") or "需要 hCaptcha 验证"), auth_detail)
    if submitted.get("status") != "page_result":
        auth_detail.update(submitted.get("detail") or {})
        return helpers.error(str(submitted.get("message") or "ABR 福利站签到失败"), auth_detail)

    auth_detail.update(submitted.get("detail") or {})
    result_text = str(submitted.get("text") or "")
    amount = _extract_amount(result_text)
    if _contains_any(result_text, _SUCCESS_MARKERS):
        auth_detail.update({"completion_signal": "success_text", "result_text": _short_text(result_text)})
        if amount is not None:
            auth_detail["quota_awarded"] = amount
            auth_detail["current_quota"] = amount
        return helpers.success(
            "ABR 福利站签到成功" + (f"，获得 ${amount:.2f}" if amount is not None else ""),
            auth_detail,
            awarded=amount,
            quota=amount,
            quota_is_usd=True,
        )
    if _contains_any(result_text, _ALREADY_MARKERS):
        auth_detail.update({"completion_signal": "already_text", "result_text": _short_text(result_text)})
        return helpers.already_done(
            "ABR 福利站今日已签到" + (f"，今日奖励 ${amount:.2f}" if amount is not None else ""),
            auth_detail,
            quota=amount,
            quota_is_usd=True,
        )
    if _contains_any(result_text, _CAPTCHA_MARKERS) and not await _captcha_token(page):
        auth_detail.update({"completion_signal": "captcha_rejected"})
        screenshot = await helpers.screenshot("abrdns-welfare-hcaptcha-rejected.png")
        if screenshot:
            auth_detail["screenshot"] = screenshot
        return helpers.need_verification(
            f"{SITE_LABEL}拒绝签到请求，需要完成 hCaptcha 验证",
            auth_detail,
        )

    auth_detail.update({"completion_signal": "unconfirmed", "result_text": _short_text(result_text)})
    screenshot = await helpers.screenshot("abrdns-welfare-checkin-unconfirmed.png")
    if screenshot:
        auth_detail["screenshot"] = screenshot
    return helpers.error("ABR 福利站签到请求已提交，但未检测到明确完成信号", auth_detail)


__all__ = ["OWNS_HTTP_FLOW", "run", "_extract_amount", "_is_site_origin"]
