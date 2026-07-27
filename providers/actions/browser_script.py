#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到方式：运行仓库内自定义异步浏览器脚本。"""

from __future__ import annotations

from typing import Any

import accounts_store

from ..base import (
    ApiError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    normalize_access_token,
    normalize_base_url,
)


def _load_runner():
    from browser import script_runner
    return script_runner


def _try_api_checkin(site: SiteConfig, profile: SiteProfile) -> CheckinResult | None:
    """浏览器脚本之前的首选方案：用配置的 access_token 直接调 HTTP 签到接口。

    仅在拿到明确结论（签到成功 / 今日已签到）时返回 CheckinResult；登录态失效
    （need_login / 401）或接口不可用一律返回 None，交由下方浏览器脚本降级处理
    （browser_state → 账密登录）。任何异常都吞掉返回 None，绝不影响后续降级。

    只对具备 HTTP 签到能力的 profile（如 sub2api）生效，且必须配置了 access_token。
    """
    token = normalize_access_token(getattr(site, "access_token", "") or "")
    if not token:
        return None
    build_client = getattr(profile, "build_client", None)
    if not callable(build_client):
        return None
    base_url = normalize_base_url(site.base_url)
    try:
        from ..base import AuthInfo

        client = build_client(site, AuthInfo(access_token=token))
        # 先读状态：今日已签到直接返回，避免重复 POST。
        try:
            status = client.fetch_status()
        except ApiError:
            status = None
        if status is not None and getattr(status, "checked_in_today", None):
            return CheckinResult(
                site.name, base_url, "already_done", "今日已签到。",
                detail={"checkin_source": "api", "api_first": True},
            )
        reward = client.do_checkin("")
        detail = {"checkin_source": "api", "api_first": True}
        if getattr(reward, "already_done", False):
            return CheckinResult(site.name, base_url, "already_done", "今日已签到。", detail=detail)
        # 标准 sub2api 无签到接口时 do_checkin 会返回 unsupported 标记——不算签到成功，
        # 交给浏览器脚本处理，避免把「只查了余额」误报成签到成功。
        raw = getattr(reward, "raw", None)
        if isinstance(raw, dict) and raw.get("unsupported_checkin"):
            return None
        return CheckinResult(site.name, base_url, "success", "签到成功。", detail=detail)
    except ApiError as exc:
        # 登录态失效（need_login）或需人机验证等：降级到浏览器脚本。
        kind = None
        classify = getattr(profile, "classify", None)
        if callable(classify):
            try:
                kind = classify(exc)
            except Exception:
                kind = None
        if kind == "already_done":
            return CheckinResult(
                site.name, base_url, "already_done", "今日已签到。",
                detail={"checkin_source": "api", "api_first": True},
            )
        return None
    except Exception:
        return None


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    """执行自定义浏览器脚本。"""
    base_url = normalize_base_url(site.base_url)
    if not str(getattr(site, "script", "") or "").strip():
        return CheckinResult(site.name, base_url, "need_config", "未配置 browser_script 脚本路径")

    # 首选方案：纯 API 签到（用配置的 access_token，不启动浏览器）。成功/已签到直接
    # 返回；登录态失效或接口不可用则降级到浏览器脚本（browser_state → 账密登录）。
    api_result = _try_api_checkin(site, profile)
    if api_result is not None:
        return api_result

    auth_method = (site.auth_method or "").strip().lower()
    fallback_provider = accounts_store.normalize_oauth_provider(
        getattr(site, "oauth_fallback_provider", "")
    )
    fallback_account = accounts_store.normalize_oauth_account(
        getattr(site, "oauth_fallback_account", "")
    )
    fallback_state = (
        accounts_store.oauth_state_text(fallback_provider, fallback_account)
        if fallback_provider else ""
    )

    if auth_method == "oauth":
        oauth_provider = accounts_store.normalize_oauth_provider(site.oauth_provider) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
        state_text = accounts_store.oauth_state_text(oauth_provider, oauth_account) or site.browser_state
        detail: dict[str, Any] = {
            "checkin_source": "browser_script",
            "auth_method": auth_method,
            "oauth_provider": oauth_provider,
            "oauth_account": oauth_account,
        }
        initial_oauth_provider = oauth_provider
    elif auth_method == "browser":
        state_text = site.browser_state
        detail = {"checkin_source": "browser_script", "auth_method": auth_method}
        initial_oauth_provider = ""
    else:
        return CheckinResult(
            site.name,
            base_url,
            "need_config",
            "browser_script 仅支持 auth_method=browser/oauth",
            detail={"checkin_source": "browser_script", "auth_method": auth_method},
        )

    use_fallback_first = not str(state_text or "").strip() and bool(fallback_provider)
    if use_fallback_first:
        state_text = fallback_state
        initial_oauth_provider = fallback_provider
        detail.update({
            "oauth_fallback_used": True,
            "oauth_provider": fallback_provider,
            "oauth_account": fallback_account,
        })

    if not str(state_text or "").strip():
        if fallback_provider:
            message = f"缺少可选 OAuth {fallback_provider}:{fallback_account} 登录态，签到失败"
        else:
            message = "站点登录态缓存不存在，且未配置 OAuth 兜底，签到失败"
        return CheckinResult(site.name, base_url, "error", message, detail=detail)

    try:
        runner = _load_runner()
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"加载 browser_script 运行器失败：{exc}", detail=detail)

    def _run(state_value: str, provider_value: str = ""):
        return runner.run_sync(
            site=site,
            browser_state_text=state_value,
            script_path=site.script,
            script_args=site.script_args,
            timeout=site.script_timeout,
            oauth_provider=provider_value,
        )

    try:
        result = _run(state_text, initial_oauth_provider)
        if (
            result.status == "need_login"
            and fallback_provider
            and not use_fallback_first
            and fallback_state.strip()
            and initial_oauth_provider != fallback_provider
        ):
            result = _run(fallback_state, fallback_provider)
            detail.update({
                "oauth_fallback_used": True,
                "oauth_provider": fallback_provider,
                "oauth_account": fallback_account,
            })
        elif result.status == "need_login" and auth_method == "browser" and not fallback_provider:
            result.status = "error"
            result.message = "站点登录态缓存已失效，且未配置 OAuth 兜底，签到失败"
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"浏览器脚本运行异常：{exc}", detail=detail)

    result_detail = result.detail
    if isinstance(result_detail, dict):
        merged_detail = dict(detail)
        merged_detail.update(result_detail)
        result_detail = merged_detail
    elif result_detail is None:
        result_detail = detail
    return CheckinResult(site.name, base_url, result.status, result.message, detail=result_detail)


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    """只读查询不运行脚本，避免刷新状态时误触发点击签到。

    首选纯 API 查额度：若配置了 access_token 且 profile 支持 HTTP 查询（如 sub2api），
    直接用 access_token 读取余额/签到状态，不启动浏览器。查询失败（登录态失效等）
    时回落到「需执行脚本」提示，由测试签到走浏览器降级。
    """
    if not str(getattr(site, "script", "") or "").strip():
        return QueryStatus(ok=False, message="未配置 browser_script 脚本路径", status="need_config")
    auth_method = (site.auth_method or "").strip().lower()
    if auth_method not in {"browser", "oauth"}:
        return QueryStatus(ok=False, message="browser_script 仅支持 auth_method=browser/oauth", status="need_config")

    # 纯 API 首选：用配置的 access_token 直接查额度（不启动浏览器）。
    token = normalize_access_token(getattr(site, "access_token", "") or "")
    build_client = getattr(profile, "build_client", None)
    if token and callable(build_client):
        try:
            from ..base import AuthInfo

            client = build_client(site, AuthInfo(access_token=token))
            user = client.fetch_user()
            quota_usd = client.quota_to_usd(user.quota_raw)
            checked_in: bool | None = None
            try:
                status = client.fetch_status()
                if status.checked_in_today is not None:
                    checked_in = status.checked_in_today
                if quota_usd is None and status.quota_usd is not None:
                    quota_usd = status.quota_usd
            except Exception:
                pass
            if quota_usd is not None:
                return QueryStatus(
                    ok=True,
                    quota_usd=quota_usd,
                    checked_in=checked_in,
                    message="查询成功（access_token 直查）",
                    status="success",
                )
        except Exception:
            # 登录态失效 / 接口不可用：回落到下方「需执行脚本」提示。
            pass

    return QueryStatus(ok=True, message="browser_script 站点需通过测试签到/定时签到执行脚本", status="success")
