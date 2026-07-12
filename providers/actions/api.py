#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""api 签到方式：调站点签到接口触发发额度。

通用流程（profile 无关）：
1. 准备认证（access_token / cookie，或在首次 HTTP 请求前刷新一次 browser/oauth）；
2. 读签到状态：今日已签 → already_done；需 Turnstile 但未提供 → need_verification；
3. 调签到接口，解析获得额度；
4. 注入当前余额（current_quota）。

browser / oauth 每次 action 最多刷新一次；刷新后的 HTTP 请求不再二次启动浏览器。
"""

from __future__ import annotations

from typing import Any

from ..base import (
    ApiError,
    BrowserAuthError,
    CheckinReward,
    CheckinResult,
    ProfileClient,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    StatusInfo,
)
from ._common import build_http_client, credentials_ready, usd_str

VERIFICATION_PATTERNS = ["Turnstile", "Cloudflare", "Just a moment", "安全验证", "challenge-platform", "人机", "验证", "captcha"]


def _build_detail(client: ProfileClient, reward: CheckinReward) -> dict[str, Any]:
    detail: dict[str, Any] = {"checkin_source": "api", "quota_is_usd": client.quota_is_usd}
    detail.update(reward.extra)
    if reward.quota_awarded is not None:
        detail["quota_awarded"] = reward.quota_awarded
    if reward.current_quota is not None:
        detail["current_quota"] = reward.current_quota
    if isinstance(reward.raw, dict):
        # 保留原始字段（如 checked_in_today），便于聚合层识别
        for key, value in reward.raw.items():
            detail.setdefault(key, value)
    return detail


def _inject_current_quota(client: ProfileClient, detail: dict[str, Any]) -> None:
    """补全 current_quota（签到返回里没有时，读 user/self）。"""
    if detail.get("current_quota") is not None:
        return
    try:
        user = client.fetch_user()
    except Exception:
        return
    if user.quota_raw is not None:
        detail["current_quota"] = user.quota_raw


def _checkin_once(site: SiteConfig, client: ProfileClient, turnstile: str) -> CheckinResult:
    base_url = client.base_url
    # 1) 读签到状态
    try:
        status = client.fetch_status()
    except ApiError as exc:
        if exc.transient:
            return CheckinResult(
                site.name,
                base_url,
                "network_error",
                f"签到状态查询暂时失败：{exc.message}",
                detail=exc.payload,
            )
        kind = client.classify(exc)
        if kind == "already_done":
            return CheckinResult(site.name, base_url, "already_done", exc.message, detail=exc.payload)
        if kind == "need_login":
            return CheckinResult(site.name, base_url, "need_login", "登录态无效或已过期，请重新导出凭据。", detail=exc.payload)
        if kind == "need_verification":
            return CheckinResult(site.name, base_url, "need_verification", exc.message, detail=exc.payload)
        # 状态接口失败不致命：继续尝试签到
        status = StatusInfo()

    # 2) 今日已签到
    if status.checked_in_today:
        detail: dict[str, Any] = {"checkin_source": "api", "quota_is_usd": client.quota_is_usd}
        if status.quota_usd is not None:
            detail["current_quota"] = status.quota_usd
            detail["quota_is_usd"] = True
        result = CheckinResult(site.name, base_url, "already_done", "今日已签到。", detail=detail)
        _inject_current_quota(client, detail)
        return result

    # 3) 需要人机验证（Cloudflare Turnstile 或图形验证码）但未提供
    if status.turnstile_required and not turnstile:
        return CheckinResult(
            site.name, base_url, "need_verification",
            "签到需要人机验证（Cloudflare Turnstile 或图形验证码），纯 HTTP 无法自动识别，"
            "请在浏览器手动完成签到，或传入 --turnstile。",
            detail=status.raw,
        )

    # 4) 执行签到
    try:
        reward = client.do_checkin(turnstile)
    except ApiError as exc:
        if exc.transient:
            return CheckinResult(
                site.name,
                base_url,
                "network_error",
                f"签到请求暂时失败：{exc.message}",
                detail=exc.payload,
            )
        kind = client.classify(exc)
        if kind == "already_done":
            return CheckinResult(site.name, base_url, "already_done", exc.message, detail=exc.payload)
        if kind == "need_login":
            return CheckinResult(site.name, base_url, "need_login", "登录态无效或已过期，请重新导出凭据。", detail=exc.payload)
        if kind == "need_verification":
            return CheckinResult(site.name, base_url, "need_verification", exc.message, detail=exc.payload)
        return CheckinResult(site.name, base_url, "error", exc.message, detail=exc.payload)

    if reward.already_done:
        detail = _build_detail(client, reward)
        return CheckinResult(site.name, base_url, "already_done", "今日已签到。", detail=detail)

    detail = _build_detail(client, reward)
    _inject_current_quota(client, detail)
    if detail.get("unsupported_checkin"):
        return CheckinResult(site.name, base_url, "success", "站点未提供签到接口，已完成余额查询。", detail=detail)
    if reward.quota_awarded is not None:
        awarded = usd_str(reward.quota_awarded, is_usd=client.quota_is_usd)
        return CheckinResult(site.name, base_url, "success", f"签到成功，获得额度：{awarded}", detail=detail)
    return CheckinResult(site.name, base_url, "success", "签到成功。", detail=detail)


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    if not credentials_ready(site, profile):
        return CheckinResult(site.name, site.base_url, "need_login", f"未找到 auth_method={site.auth_method} 所需的有效凭据，请先配置。")

    try:
        client = build_http_client(site, profile)
    except BrowserAuthError as exc:
        return CheckinResult(site.name, site.base_url, exc.status, exc.message, detail=exc.detail)
    return _checkin_once(site, client, turnstile)


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    if not credentials_ready(site, profile):
        return QueryStatus(ok=False, message="未配置有效凭据", status="need_config")

    try:
        client = build_http_client(site, profile)
    except BrowserAuthError as exc:
        return QueryStatus(ok=False, message=exc.message, status=exc.status, detail=exc.detail)

    def _read() -> QueryStatus:
        quota_usd: float | None = None
        checked_in: bool | None = None
        try:
            user = client.fetch_user()
            quota_usd = client.quota_to_usd(user.quota_raw)
        except ApiError as exc:
            if exc.transient:
                return QueryStatus(ok=False, message=f"站点暂时不可达或接口限流：{exc.message}", status="network_error", detail=exc.payload)
            kind = client.classify(exc)
            if kind == "need_login":
                return QueryStatus(ok=False, message="登录态无效或已过期", status="need_login", detail=exc.payload)
            if kind == "need_verification":
                return QueryStatus(ok=False, message=exc.message, status="need_verification", detail=exc.payload)
            return QueryStatus(ok=False, message=exc.message, status="error", detail=exc.payload)
        except Exception as exc:
            return QueryStatus(ok=False, message=f"查询异常：{exc}", status="error")
        status_message = "查询成功"
        try:
            status = client.fetch_status()
            if status.checked_in_today is not None:
                checked_in = status.checked_in_today
            if quota_usd is None and status.quota_usd is not None:
                quota_usd = status.quota_usd
        except ApiError as exc:
            # 用户额度已读到时，签到状态接口失败不应把整体查询判失败；只在提示中保留原因。
            status_message = f"查询成功；签到状态读取失败：{exc.message}"
        except Exception as exc:
            status_message = f"查询成功；签到状态读取异常：{exc}"
        return QueryStatus(ok=True, quota_usd=quota_usd, checked_in=checked_in, message=status_message, status="success")

    return _read()
