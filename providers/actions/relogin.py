#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""relogin 签到方式：用浏览器自动重放第三方 OAuth 登录，触发「登录即发额度」。

适用站点（如 AgentRouter https://agentrouter.org）特征：
- 没有任何独立签到接口，额度在「第三方 OAuth 登录回调」时发放；
- 第三方授权端点（Linux.do 等）带 Cloudflare / 阿里云 WAF，纯 HTTP 无法重放；
- 但只要浏览器持有有效第三方登录态，再次发起 OAuth 会自动放行跳回站点发额度。

核心浏览器逻辑集中在 browser/session.py（CLI 与 GUI 共享），本动作仅做适配——
把 SiteConfig 映射成参数、把结果映射成 CheckinResult。该动作要求 auth_method=oauth。

登录态来源：
- relogin 站点配置保存 oauth_provider + oauth_account；
- 第三方登录态从 ACCOUNTS.json 顶层 oauth_states[oauth_provider].accounts[oauth_account].state 读取；
- 若从 CLI/批量调度临时传入 CHECKIN_BROWSER_STATE，则仅在 auth_method=oauth 时作为显式注入兜底使用。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import accounts_store

from ..auth import has_http_credentials, load_auth
from ..base import (
    QUOTA_UNIT,
    ApiError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    normalize_base_url,
)
from ._common import build_http_client

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent


def _load_session():
    sys.path.insert(0, str(SCRIPT_DIR))
    from browser import session as browser_session
    return browser_session


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    """浏览器自动 OAuth 重登签到入口（桥接 async browser_session.run_oauth_checkin）。"""
    base_url = normalize_base_url(site.base_url)

    try:
        browser_session = _load_session()
    except Exception as exc:  # pragma: no cover - 极少触发
        return CheckinResult(site.name, base_url, "error", f"加载 browser_session 失败：{exc}")

    def _log(msg: str) -> None:
        print(f"[relogin:{site.name}] {msg}", file=sys.stderr, flush=True)

    auth_method = (site.auth_method or "cookie").strip().lower()
    if auth_method != "oauth":
        return CheckinResult(
            site.name,
            base_url,
            "need_config",
            "OAuth 重登要求 auth_method=oauth；请把登录方式切换为“OAuth 登录”。",
            detail={"checkin_source": "relogin", "auth_method": auth_method},
        )

    oauth_provider = accounts_store.normalize_oauth_provider(site.oauth_provider) or "linuxdo"
    oauth_account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
    browser_state_text = accounts_store.oauth_state_text(oauth_provider, oauth_account)
    if not browser_state_text:
        return CheckinResult(
            site.name,
            base_url,
            "need_login",
            f"缺少共享 {oauth_provider}:{oauth_account} 登录态，请先在管理界面捕获该 OAuth 账号。",
            detail={"checkin_source": "relogin", "oauth_provider": oauth_provider, "oauth_account": oauth_account},
        )

    if accounts_store.state_contains_site_domain(browser_state_text, base_url):
        return CheckinResult(
            site.name,
            base_url,
            "need_login",
            f"{oauth_provider}:{oauth_account} 登录态包含站点 Cookie，像是旧的站点浏览器凭证；请删除该 OAuth 登录态后重新捕获纯第三方 OAuth 登录态。",
            detail={"checkin_source": "relogin", "oauth_provider": oauth_provider, "oauth_account": oauth_account},
        )

    try:
        outcome = browser_session.run_sync(
            browser_session.run_oauth_checkin(
                base_url=base_url,
                account_name=site.name,
                browser_state_text=browser_state_text,
                oauth_provider=oauth_provider,
                fallback_uid=site.user_id.strip(),
                proxy=site.proxy or "",
                log=_log,
            )
        )
    except browser_session.BrowserSessionError as exc:
        msg = str(exc)
        status = "error" if "camoufox" in msg.lower() else "need_login"
        return CheckinResult(site.name, base_url, status, msg)
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"浏览器自动 OAuth 异常：{exc}")

    status = outcome.get("status", "need_login")
    link = outcome.get("link") or {}
    quota_after = outcome.get("quota_after")
    quota_before = outcome.get("quota_before")
    detail: dict[str, Any] = {
        "checkin_source": "relogin",
        "oauth_provider": oauth_provider,
        "oauth_account": oauth_account,
        "current_quota": quota_after if quota_after is not None else quota_before,
        "oauth_landed_back": link.get("landed_back"),
        "oauth_frontend_entry": bool(link.get("frontend_entry")),
    }
    if link.get("site_error"):
        detail["site_error"] = link.get("site_error")
    if link.get("site_errors"):
        detail["site_errors"] = link.get("site_errors")
    if outcome.get("delta") is not None:
        detail["quota_awarded"] = outcome["delta"]

    return CheckinResult(site.name, base_url, status, outcome.get("message", ""), detail=detail)


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    """只读查询额度。

    额度查询优先走统一 HTTP 登录层（access_token/cookie）；这不触发 OAuth 发放，
    且 CI/GUI 中已有 token 时无需启动浏览器。只有没有 HTTP 凭据时，才回落到
    browser_state 的浏览器检测逻辑。
    """
    base_url = normalize_base_url(site.base_url)

    auth = load_auth(site)
    if has_http_credentials(auth):
        client = profile.build_client(site, auth)
        try:
            user = client.fetch_user()
            return QueryStatus(ok=True, quota_usd=client.quota_to_usd(user.quota_raw), checked_in=None, message="查询成功", status="success")
        except ApiError as exc:
            if exc.transient:
                return QueryStatus(ok=False, message=f"站点暂时不可达或接口限流：{exc.message}", status="network_error", detail=exc.payload)
            kind = client.classify(exc)
            if kind == "need_login":
                return QueryStatus(ok=False, message="登录态失效，请重新导出凭据", status="need_login", detail=exc.payload)
            if kind == "need_verification":
                return QueryStatus(ok=False, message=exc.message, status="need_verification", detail=exc.payload)
            return QueryStatus(ok=False, message=exc.message, status="error", detail=exc.payload)
        except Exception as exc:
            return QueryStatus(ok=False, message=f"查询异常：{exc}", status="error")

    if site.checkin_action == "relogin":
        return QueryStatus(ok=False, message="relogin 站点只读查询需要配置站点 Cookie/Token；共享 OAuth 登录态仅用于重登签到，不代表当前已登录。", status="need_config")

    try:
        browser_session = _load_session()
    except Exception as exc:
        return QueryStatus(ok=False, message=f"加载 browser_session 失败：{exc}", status="error")

    def _log(msg: str) -> None:
        print(f"[relogin:{site.name}] {msg}", file=sys.stderr, flush=True)

    try:
        outcome = browser_session.run_sync(
            browser_session.verify_state(
                base_url=base_url,
                browser_state_text=site.browser_state or "",
                fallback_uid=site.user_id.strip(),
                proxy=site.proxy or "",
                log=_log,
            )
        )
    except browser_session.BrowserSessionError as exc:
        msg = str(exc)
        status = "error" if "camoufox" in msg.lower() or "加载" in msg else "need_login"
        return QueryStatus(ok=False, message=msg, status=status)
    except Exception as exc:
        return QueryStatus(ok=False, message=f"查询异常：{exc}", status="error")

    if not outcome.get("ok"):
        msg = outcome.get("message", "登录态无效")
        low = str(msg).lower()
        if outcome.get("waf_blocked"):
            status = "need_verification"
        elif any(s in low for s in ("网络", "timeout", "timed out", "不可达", "限流", "429")):
            status = "network_error"
        else:
            status = "need_login"
        return QueryStatus(ok=False, message=msg, status=status)

    quota = outcome.get("quota")
    quota_usd = float(quota) / QUOTA_UNIT if isinstance(quota, (int, float)) else None
    return QueryStatus(ok=True, quota_usd=quota_usd, checked_in=None, message="查询成功", status="success")
