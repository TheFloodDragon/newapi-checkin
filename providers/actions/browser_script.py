#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到方式：运行仓库内自定义异步浏览器脚本。"""

from __future__ import annotations

from typing import Any

import accounts_store

from ..base import CheckinResult, QueryStatus, SiteConfig, SiteProfile, normalize_base_url


def _load_runner():
    from browser import script_runner
    return script_runner


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    """执行自定义浏览器脚本。"""
    base_url = normalize_base_url(site.base_url)
    if not str(getattr(site, "script", "") or "").strip():
        return CheckinResult(site.name, base_url, "need_config", "未配置 browser_script 脚本路径")

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
    """只读查询不运行脚本，避免刷新状态时误触发点击签到。"""
    if not str(getattr(site, "script", "") or "").strip():
        return QueryStatus(ok=False, message="未配置 browser_script 脚本路径", status="need_config")
    auth_method = (site.auth_method or "").strip().lower()
    if auth_method not in {"browser", "oauth"}:
        return QueryStatus(ok=False, message="browser_script 仅支持 auth_method=browser/oauth", status="need_config")
    return QueryStatus(ok=True, message="browser_script 站点需通过测试签到/定时签到执行脚本", status="success")
