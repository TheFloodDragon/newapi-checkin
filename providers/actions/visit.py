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
from pathlib import Path
from typing import Any

import accounts_store
import time_utils

from ..base import (
    ApiError,
    BrowserAuthError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    contains_any,
    format_usd,
    normalize_base_url,
)
from ..base import VERIFICATION_PATTERNS as _BASE_VERIFICATION_PATTERNS
from ._common import build_http_client, credentials_ready

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
STATE_PATH = accounts_store.RESULTS_DIR / "login_grant_state.json"
LEGACY_STATE_PATH = SCRIPT_DIR / "login_grant_state.json"

# 在唯一词表基础上追加宽泛的「验证」：保活响应多为完整页面/提示文案，
# 语境里出现「验证」基本就是人机验证，误伤登录报错的风险低（保持既有行为）。
VERIFICATION_PATTERNS = [*_BASE_VERIFICATION_PATTERNS, "验证"]


# ── 本地状态持久化（跨次运行对比额度变化）────────────────────────────────────

def _load_state() -> dict[str, Any]:
    """优先读取新缓存目录状态；新文件缺失时兼容旧根目录文件。"""
    source = STATE_PATH if STATE_PATH.exists() else LEGACY_STATE_PATH
    if not source.exists():
        return {}
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise accounts_store.ConfigError(f"额度状态文件 {source.name} 损坏或不可读：{exc}") from exc
    if not isinstance(data, dict):
        raise accounts_store.ConfigError(f"额度状态文件 {source.name} 顶层必须是 JSON 对象")
    return data


def _record_state(key: str, record: dict[str, Any], legacy_key: str = "") -> dict[str, Any]:
    """在共享文件锁内完成额度状态的读-改-写。

    legacy_key 用于一次性平滑迁移：新 key 尚无记录时，把旧的按站点归档的记录当作
    本次基线读出来（只读不改）。这样升级后第一次运行不会因为「查无历史」而把已有
    额度误报成首次记录，同时写入只落新 key，旧记录随过期自然淘汰。
    """
    with accounts_store.file_lock(STATE_PATH):
        state = _load_state()
        previous = state.get(key) if isinstance(state.get(key), dict) else {}
        if not previous and legacy_key and legacy_key != key:
            legacy = state.get(legacy_key)
            if isinstance(legacy, dict):
                previous = legacy
        state[key] = record
        accounts_store.atomic_write_text(
            STATE_PATH,
            json.dumps(state, ensure_ascii=False, indent=2),
        )
    return dict(previous)


def _account_identity(site: SiteConfig) -> str:
    """站点内区分账号的稳定标识；全都拿不到时返回空串。

    优先 user_id（站点自己的账号编号，最稳定），其次「确实按 OAuth 登录」时的
    provider:account，最后才退到站点名。不用凭据本身做标识：token 会续期、cookie
    会轮换，那样每次刷新都换 key，历史额度直接断档。

    注意 oauth_provider 在 SiteConfig 里有默认值 "linuxdo"，所以必须先确认
    auth_method/checkin_action 真的走 OAuth，否则所有没填 user_id 的站点都会得到
    同一个 linuxdo:default，同址多账号照旧互相污染。
    """
    user_id = str(getattr(site, "user_id", "") or "").strip()
    if user_id:
        return user_id
    auth_method = str(getattr(site, "auth_method", "") or "").strip().lower()
    action = str(getattr(site, "checkin_action", "") or "").strip().lower()
    if auth_method == "oauth" or action == "relogin":
        provider = accounts_store.normalize_oauth_provider(getattr(site, "oauth_provider", "")) or "linuxdo"
        account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
        return f"{provider}:{account}"
    return str(getattr(site, "name", "") or "").strip()


def _legacy_state_key(base_url: str) -> str:
    """旧版按站点归档的 key（仅用于首次读取时回落，不再写入）。"""
    return normalize_base_url(base_url)


def _state_key(base_url: str, user_id: str = "") -> str:
    """额度历史按「站点链接 + 账号」归档。

    只用 base_url 会让同一站点下的多个账号共用一条额度基线：后跑的账号拿前一个
    账号的余额做对比，于是虚报「额度增加」或把真实发放判成「无变化」。仓库其它
    缓存（status_key / task_key / token_cache）早已按渠道区分，这里必须对齐。

    仍然不把站点名放在第一位：名称可改，改名不该让历史额度断档。
    """
    base = normalize_base_url(base_url)
    account = str(user_id or "").strip()
    return f"{base}|{account}" if base and account else base or account


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
    # key 带账号身份：同一 base_url 下的多个账号必须各自维护基线，否则互相污染。
    key = _state_key(base_url, _account_identity(site))
    # 时间戳统一走 time_utils：裸 datetime.now() 是无时区本地时间，CI（Asia/Shanghai）
    # 与本地运行写出的值无法可靠比较先后，业务日也会在 UTC 跨日时与用户直觉不符。
    prev = _record_state(
        key,
        {
            "quota": quota,
            "username": username,
            "updated_at": time_utils.utc_iso(),
            "date": time_utils.business_date(),
        },
        legacy_key=_legacy_state_key(base_url),
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
            f"保活成功，额度增加：{format_usd(quota_delta, is_usd=is_usd)}（登录已触发发放）",
            detail=detail,
        )

    if prev_quota is None:
        return CheckinResult(
            site.name, base_url, "already_done",
            f"保活成功，已记录当前额度：{format_usd(quota, is_usd=is_usd)}（下次运行可对比增量）",
            detail=detail,
        )

    return CheckinResult(
        site.name, base_url, "already_done",
        f"保活成功，额度无变化：{format_usd(quota, is_usd=is_usd)}（该站靠 OAuth 登录发放，额度可能已于近期登录时发放）",
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
