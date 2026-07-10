#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到方式：运行仓库内自定义异步浏览器脚本。"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import accounts_store

from ..base import CheckinResult, QueryStatus, SiteConfig, SiteProfile, normalize_base_url

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent


def _load_runner():
    sys.path.insert(0, str(SCRIPT_DIR))
    from browser import script_runner
    return script_runner


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    """执行自定义浏览器脚本。"""
    base_url = normalize_base_url(site.base_url)
    if not str(getattr(site, "script", "") or "").strip():
        return CheckinResult(site.name, base_url, "need_config", "未配置 browser_script 脚本路径")

    auth_method = (site.auth_method or "").strip().lower()
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
    elif auth_method == "browser":
        state_text = site.browser_state
        detail = {"checkin_source": "browser_script", "auth_method": auth_method}
    else:
        return CheckinResult(
            site.name,
            base_url,
            "need_config",
            "browser_script 仅支持 auth_method=browser/oauth",
            detail={"checkin_source": "browser_script", "auth_method": auth_method},
        )

    if not str(state_text or "").strip():
        return CheckinResult(site.name, base_url, "need_login", "缺少浏览器/OAuth 登录态", detail=detail)

    try:
        runner = _load_runner()
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"加载 browser_script 运行器失败：{exc}", detail=detail)

    try:
        result = runner.run_sync(
            site=site,
            browser_state_text=state_text,
            script_path=site.script,
            script_args=site.script_args,
            timeout=site.script_timeout,
        )
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
