#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""visit 签到方式：访问 /api/user/self 保活监控额度（不主动触发发放）。

适用「无签到接口、登录即发额度」类站点（如 AgentRouter）的保活式签到：
1. 用已存凭据读 user/self 保活并读额度；
2. 额度持久化到 login_grant_state.json，跨次对比增量；
3. 额度增长 → success（detail.quota_awarded=增量）；无变化 → already_done；
   登录态失效 → need_login；Cloudflare/人机验证 → need_verification。

它不触发发放，真正领取仍需在浏览器手动登录一次（或用 relogin 方式）。
"""

from __future__ import annotations

import json
from datetime import datetime, date
from pathlib import Path
from typing import Any

import accounts_store

from ..base import (
    ApiError,
    BrowserAuthError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    contains_any,
    normalize_base_url,
)
from ._common import build_http_client, credentials_ready, usd_str

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
STATE_PATH = SCRIPT_DIR / "login_grant_state.json"

VERIFICATION_PATTERNS = ["turnstile", "cloudflare", "just a moment", "安全验证", "challenge-platform", "人机", "验证"]


# ── 本地状态持久化（跨次运行对比额度变化）────────────────────────────────────

def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise accounts_store.ConfigError(f"额度状态文件 {STATE_PATH.name} 损坏或不可读：{exc}") from exc
    if not isinstance(data, dict):
        raise accounts_store.ConfigError(f"额度状态文件 {STATE_PATH.name} 顶层必须是 JSON 对象")
    return data


def _record_state(key: str, record: dict[str, Any]) -> dict[str, Any]:
    """在共享文件锁内完成额度状态的读-改-写。"""
    with accounts_store.file_lock(STATE_PATH):
        state = _load_state()
        previous = state.get(key) if isinstance(state.get(key), dict) else {}
        state[key] = record
        accounts_store.atomic_write_text(
            STATE_PATH,
            json.dumps(state, ensure_ascii=False, indent=2),
        )
    return dict(previous)


def _state_key(base_url: str, user_id: str = "") -> str:
    """额度历史按站点链接归档。

    站点名称可能会改，同一链接才是同一额度来源；user_id 只保留在签名里兼容旧调用，
    不再参与新 key。这样 GUI/CI 改名后也能继续对比同站点额度。
    """
    return normalize_base_url(base_url)


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    if not credentials_ready(site, profile):
        return CheckinResult(
            site.name, site.base_url, "need_login",
            "未找到 Cookie 或 Access token，请在浏览器完成 OAuth 登录后重新导出凭据。",
        )

    try:
        client = build_http_client(site, profile)
    except BrowserAuthError as exc:
        return CheckinResult(site.name, site.base_url, exc.status, exc.message, detail=exc.detail)
    base_url = client.base_url
    try:
        user = client.fetch_user()
    except ApiError as exc:
        if exc.transient:
            return CheckinResult(
                site.name,
                base_url,
                "network_error",
                f"站点暂时不可达或接口限流：{exc.message}",
                detail=exc.payload,
            )
        if contains_any(exc.message, VERIFICATION_PATTERNS):
            return CheckinResult(site.name, base_url, "need_verification", exc.message, detail=exc.payload)
        if client.classify(exc) == "need_login":
            return CheckinResult(
                site.name, base_url, "need_login",
                "登录态已失效（session/token 过期）。该站靠 OAuth 登录发放额度，"
                "请在浏览器重新登录后重新导出凭据。",
                detail=exc.payload,
            )
        return CheckinResult(site.name, base_url, "error", exc.message, detail=exc.payload)

    quota = user.quota_raw
    username = user.username

    # 跨次对比额度变化；读-改-写必须在同一文件锁内完成。
    key = _state_key(base_url, site.user_id.strip())
    today = date.today().isoformat()
    prev = _record_state(
        key,
        {
            "quota": quota,
            "username": username,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "date": today,
        },
    )
    prev_quota = prev.get("quota")

    detail: dict[str, Any] = {
        "checkin_source": "visit",
        "current_quota": quota,
        "quota_is_usd": client.quota_is_usd,
        "username": username,
    }

    quota_delta: float | None = None
    if isinstance(quota, (int, float)) and isinstance(prev_quota, (int, float)):
        quota_delta = float(quota) - float(prev_quota)

    is_usd = client.quota_is_usd
    if quota_delta is not None and quota_delta > 0:
        detail["quota_awarded"] = quota_delta
        return CheckinResult(
            site.name, base_url, "success",
            f"保活成功，额度增加：{usd_str(quota_delta, is_usd=is_usd)}（登录已触发发放）",
            detail=detail,
        )

    if prev_quota is None:
        return CheckinResult(
            site.name, base_url, "already_done",
            f"保活成功，已记录当前额度：{usd_str(quota, is_usd=is_usd)}（下次运行可对比增量）",
            detail=detail,
        )

    return CheckinResult(
        site.name, base_url, "already_done",
        f"保活成功，额度无变化：{usd_str(quota, is_usd=is_usd)}（该站靠 OAuth 登录发放，额度可能已于近期登录时发放）",
        detail=detail,
    )


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    if not credentials_ready(site, profile):
        return QueryStatus(ok=False, message="未配置 Cookie / Access token", status="need_config")

    try:
        client = build_http_client(site, profile)
    except BrowserAuthError as exc:
        return QueryStatus(ok=False, message=exc.message, status=exc.status, detail=exc.detail)
    try:
        user = client.fetch_user()
    except ApiError as exc:
        if exc.transient:
            return QueryStatus(ok=False, message=f"站点暂时不可达或接口限流：{exc.message}", status="network_error", detail=exc.payload)
        if contains_any(exc.message, VERIFICATION_PATTERNS) or client.classify(exc) == "need_verification":
            return QueryStatus(ok=False, message=exc.message, status="need_verification", detail=exc.payload)
        if client.classify(exc) == "need_login":
            return QueryStatus(ok=False, message="登录态失效，请重新导出凭据", status="need_login", detail=exc.payload)
        return QueryStatus(ok=False, message=exc.message, status="error", detail=exc.payload)
    except Exception as exc:
        return QueryStatus(ok=False, message=f"查询异常：{exc}", status="error")

    quota_usd = client.quota_to_usd(user.quota_raw)
    # visit 类站点无独立签到状态接口，checked_in 无法判断
    return QueryStatus(ok=True, quota_usd=quota_usd, checked_in=None, message="查询成功", status="success")
