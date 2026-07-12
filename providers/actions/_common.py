#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HTTP 签到动作（api / visit）的共享辅助：认证准备、客户端构造、额度格式化。"""

from __future__ import annotations

from typing import Any

import accounts_store

from ..auth import has_http_credentials, load_auth
from ..base import (
    AuthInfo,
    BrowserAuthError,
    ProfileClient,
    QUOTA_UNIT,
    SiteConfig,
    SiteProfile,
    normalize_access_token,
)


def usd_str(value: Any, *, is_usd: bool) -> str:
    """把额度数值格式化为 $x USD 字符串；非数字原样返回。

    is_usd=True 表示值本身已是美元（sub2api）；False 表示内部 quota，需 /500000（newapi）。
    """
    try:
        usd = float(value) if is_usd else float(value) / QUOTA_UNIT
        return f"${usd:.4g}"
    except (TypeError, ValueError):
        return str(value) if value is not None else ""


def oauth_state_text_for_site(site: SiteConfig) -> str:
    """读取当前站点显式选择的 OAuth provider/account 登录态。"""
    try:
        provider = accounts_store.normalize_oauth_provider(getattr(site, "oauth_provider", "")) or "linuxdo"
        account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
        return accounts_store.oauth_state_text(provider, account)
    except (OSError, accounts_store.ConfigError):
        return ""


def has_refresh_state(site: SiteConfig) -> bool:
    """是否存在当前 auth_method 明确允许使用的浏览器/OAuth 登录态。"""
    auth_method = (site.auth_method or "cookie").strip().lower()
    if auth_method == "browser":
        return bool((site.browser_state or "").strip())
    if auth_method == "oauth":
        return bool(oauth_state_text_for_site(site) or (site.browser_state or "").strip())
    return False


def persist_access_token(site: SiteConfig, token: str) -> None:
    """刷新出新 access_token 后尽力写回 ACCOUNTS.json。"""
    try:
        if accounts_store.update_account_access_token(site.name, site.base_url, token):
            site.access_token = token
    except (OSError, accounts_store.ConfigError):
        # 持久化失败（文件锁超时/损坏/IO 错误）不应影响本次签到；
        # 本次内存 token 仍可继续使用。
        site.access_token = token


def persist_refreshed_auth(site: SiteConfig, auth: AuthInfo) -> None:
    """把浏览器刷新出的认证（access_token 或 cookie）尽力写回 ACCOUNTS.json。"""
    if auth.access_token:
        persist_access_token(site, auth.access_token)


def build_http_client(site: SiteConfig, profile: SiteProfile) -> ProfileClient:
    """按 auth_method 准备 HTTP 凭据并构造 profile 客户端。

    - access_token / cookie：只加载对应 HTTP 凭据，不隐式读取 OAuth；
    - browser：只使用站点级 browser_state 刷新认证（cookie 或 token）；
    - oauth：只使用显式选择的 OAuth provider/account 登录态刷新认证。

    browser/oauth 的认证刷新只在这里执行，且每次 action 最多一次。浏览器刷新的
    确定性失败（如 WAF 持续风控）以 BrowserAuthError 向上传播，由 action 层翻译
    成 need_verification 等状态；刷新无结果时直接返回 need_login，不构造空凭据客户端。
    """
    auth_method = (site.auth_method or "cookie").strip().lower()
    build_lazy = getattr(profile, "build_lazy_refresh_client", None)
    # 独立的「可选 OAuth」只在明确选择时启用；未选择时完全沿用普通 Token/Cookie 流程。
    fallback_provider = accounts_store.normalize_oauth_provider(
        getattr(site, "oauth_fallback_provider", "")
    )
    if fallback_provider and auth_method == "access_token" and callable(build_lazy):
        lazy_client = build_lazy(site)
        if lazy_client is not None:
            return lazy_client
    if auth_method in {"browser", "oauth"}:
        # OAuth/浏览器登录方式本身仍支持缓存优先。
        lazy_client = build_lazy(site) if callable(build_lazy) else None
        if lazy_client is not None:
            return lazy_client
        if profile.supports_browser_refresh():
            auth = profile.refresh_auth_via_browser(site)
            if auth is not None:
                persist_refreshed_auth(site, auth)
                return profile.build_client(site, auth)
        if auth_method == "oauth":
            provider = accounts_store.normalize_oauth_provider(getattr(site, "oauth_provider", "")) or "linuxdo"
            account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
            message = (
                f"账号缓存已失效，且通过 {provider}:{account} OAuth 自动登录刷新失败；"
                "请在管理界面重新捕获对应 OAuth 登录态。"
            )
            detail = {
                "cache_expired": True,
                "auth_method": auth_method,
                "oauth_provider": provider,
                "oauth_account": account,
            }
        else:
            message = "账号缓存已失效，浏览器登录态自动刷新失败；请重新捕获该站点登录态。"
            detail = {"cache_expired": True, "auth_method": auth_method}
        raise BrowserAuthError("need_login", message, detail=detail)
    if auth_method in {"access_token", "cookie"}:
        return profile.build_client(site, load_auth(site))
    return profile.build_client(site, AuthInfo())


def credentials_ready(site: SiteConfig, profile: SiteProfile) -> bool:
    """是否具备执行 HTTP 动作的凭据；严格遵守当前 auth_method。"""
    auth_method = (site.auth_method or "cookie").strip().lower()
    if auth_method == "access_token":
        return bool(normalize_access_token(load_auth(site).access_token))
    if auth_method == "cookie":
        return bool(load_auth(site).cookie)
    if auth_method in {"browser", "oauth"}:
        return profile.supports_browser_refresh() and has_refresh_state(site)
    return has_http_credentials(load_auth(site))
