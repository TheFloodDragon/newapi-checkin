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

from typing import Any, Callable

from ..base import (
    QUOTA_UNIT,
    ApiError,
    BrowserAuthError,
    CheckinReward,
    CheckinResult,
    ProfileClient,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    StatusInfo,
    format_usd,
    has_awarded_amount,
    normalize_base_url,
)
from ._common import build_http_client, credentials_ready


def _need_login_message(site: SiteConfig) -> str:
    """按认证方式生成可操作的登录失效提示。"""
    auth_method = (site.auth_method or "").strip().lower()
    fallback_provider = str(getattr(site, "oauth_fallback_provider", "") or "").strip()
    if fallback_provider:
        account = str(getattr(site, "oauth_fallback_account", "") or "default").strip()
        return (
            f"账号缓存已失效，可选 OAuth {fallback_provider}:{account} 自动登录刷新未成功；"
            "请检查对应 OAuth 登录态，或在站点登录状态中取消可选 OAuth。"
        )
    if auth_method == "oauth":
        provider = str(getattr(site, "oauth_provider", "") or "linuxdo").strip()
        account = str(getattr(site, "oauth_account", "") or "default").strip()
        return (
            f"账号缓存已失效，已尝试通过 {provider}:{account} OAuth 自动登录刷新但未成功；"
            "请在管理界面重新捕获对应 OAuth 登录态。"
        )
    if auth_method == "browser":
        return "账号缓存已失效，浏览器登录态自动刷新未成功；请重新捕获该站点登录态。"
    return "登录态无效或已过期，请重新导出凭据。"


def _build_detail(client: ProfileClient, reward: CheckinReward) -> dict[str, Any]:
    """把 reward 摊平成 detail。

    reward 的附加字段一律按「可缺失」处理：站点脚本的 do_checkin 钩子（见
    _script_checkin）由用户维护，只保证给出签到结论，不保证填满 CheckinReward
    的每个可选字段。硬取属性会让一次缺字段变成整站签到异常。
    """
    detail: dict[str, Any] = {"checkin_source": "api", "quota_is_usd": getattr(client, "quota_is_usd", False)}
    extra = getattr(reward, "extra", None)
    if isinstance(extra, dict):
        detail.update(extra)
    awarded = getattr(reward, "quota_awarded", None)
    if awarded is not None:
        detail["quota_awarded"] = awarded
    current = getattr(reward, "current_quota", None)
    if current is not None:
        detail["current_quota"] = current
    raw = getattr(reward, "raw", None)
    if isinstance(raw, dict):
        # 保留原始字段（如 checked_in_today），便于聚合层识别
        for key, value in raw.items():
            detail.setdefault(key, value)
    return detail


def _read_quota(client: ProfileClient) -> float | None:
    """读取当前余额（美元）；失败返回 None。用于签到前后的交叉验证。"""
    try:
        user = client.fetch_user()
    except Exception:
        return None
    return client.quota_to_usd(user.quota_raw)


def _quota_increased(
    client: ProfileClient,
    quota_before: float | None,
    detail: dict[str, Any],
) -> float | None:
    """签到后余额是否真的增长；返回增量（美元），无法确认返回 None。

    签到接口没给奖励字段时，余额增长是「确实发放了」的最可靠证据。
    """
    if quota_before is None:
        return None
    current = detail.get("current_quota")
    quota_after = (
        client.quota_to_usd(current)
        if current is not None and not detail.get("quota_is_usd")
        else current
    )
    if not isinstance(quota_after, (int, float)) or isinstance(quota_after, bool):
        quota_after = _read_quota(client)
    if not isinstance(quota_after, (int, float)) or isinstance(quota_after, bool):
        return None
    delta = float(quota_after) - float(quota_before)
    # 浮点余额比较留一点容差，避免把计费抖动当成签到到账。
    return delta if delta > 1e-9 else None


def _checked_in_after(client: ProfileClient) -> bool:
    """重新读状态接口，确认站点是否已把今日标记为已签到。"""
    try:
        return bool(client.fetch_status().checked_in_today)
    except Exception:
        return False


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


def _script_log(site: SiteConfig, message: str) -> None:
    """站点脚本的诊断输出（stderr；worker 的 stdout 是机器协议通道）。"""
    import sys

    from mask_utils import mask_secrets

    print(f"[api:{site.name}] {mask_secrets(str(message))}", file=sys.stderr, flush=True)


def _script_checkin(site: SiteConfig, client: ProfileClient, turnstile: str) -> CheckinReward | None:
    """把签到交给站点脚本；脚本不接管（或未配置脚本）时返回 None。

    这是「站点私改玩法」的唯一入口：图形验证码这类只服务于个别 fork 的流程都写成
    脚本（如 scripts/newapi_captcha.py），由用户在管理界面填脚本路径启用，
    通用适配器不必为它们背分支。

    脚本约定：``do_checkin(client, log=None) -> CheckinReward | None``
    —— 返回 None 表示「本站不需要我接管」，抛 ApiError 由下游按 classify 归类。
    加载失败只记日志并回落默认流程：配置写错不该让签到直接失败。
    """
    script_path = str(getattr(site, "script", "") or "").strip()
    if not script_path:
        return None
    try:
        from browser import script_loader

        hooks = script_loader.load_script_hooks(script_path)
    except Exception as exc:  # noqa: BLE001
        _script_log(site, f"加载站点脚本失败，改用默认签到流程：{type(exc).__name__}: {exc}")
        return None
    if hooks.do_checkin is None:
        return None
    reward = hooks.do_checkin(client, log=lambda message: _script_log(site, message))
    return reward if isinstance(reward, CheckinReward) else None


def run_http_flow(
    site: SiteConfig,
    client: ProfileClient,
    turnstile: str = "",
    *,
    allow_site_hook: bool = True,
    observer: Callable[[str, Any], None] | None = None,
) -> CheckinResult:
    """执行唯一的 HTTP 签到状态机；供 api 与 browser_script API-first 复用。

    结果里的 base_url 以站点配置为准（client 的 base_url 仅作补充）：两条链路都按
    站点身份汇总结果，而 profile 客户端不保证暴露该属性。
    """
    base_url = normalize_base_url(site.base_url) or str(getattr(client, "base_url", "") or "")

    def notify(event: str, payload: Any = None) -> None:
        if observer is not None:
            observer(event, payload)
    # 1) 读签到状态
    try:
        status = client.fetch_status()
        notify("status", status)
    except ApiError as exc:
        notify("status_error", exc)
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
            return CheckinResult(site.name, base_url, "need_login", _need_login_message(site), detail=exc.payload)
        if kind == "need_verification":
            return CheckinResult(site.name, base_url, "need_verification", exc.message, detail=exc.payload)
        # 状态接口失败不致命：继续尝试签到
        status = StatusInfo()

    # 2) 今日已签到
    # 状态对象按 StatusInfo 契约读取，但用 getattr 兜底：站点脚本与 profile 可返回
    # 只带部分字段的等价对象，缺字段应当按「未知」处理，而不是抛 AttributeError
    # 把一次可完成的签到变成 error。
    status_quota = getattr(status, "quota_usd", None)
    if getattr(status, "checked_in_today", None):
        detail: dict[str, Any] = {
            "checkin_source": "api",
            "quota_is_usd": bool(getattr(client, "quota_is_usd", False)),
        }
        if status_quota is not None:
            detail["current_quota"] = status_quota
            detail["quota_is_usd"] = True
        result = CheckinResult(site.name, base_url, "already_done", "今日已签到。", detail=detail)
        _inject_current_quota(client, detail)
        return result

    # 3) 需要人机验证（Cloudflare Turnstile 或图形验证码）但未提供
    if getattr(status, "turnstile_required", False) and not turnstile:
        return CheckinResult(
            site.name, base_url, "need_verification",
            "签到需要人机验证（Cloudflare Turnstile 或图形验证码），纯 HTTP 无法自动识别，"
            "请在浏览器手动完成签到，或传入 --turnstile。",
            detail=status.raw,
        )

    # 4) 执行签到
    # 先记下签到前余额：部分 fork 的签到接口不返回奖励字段，只能靠前后余额差
    # 判断是否真的到账（避免把「HTTP 200 但未发放」误报成成功）。状态接口已给出
    # 余额时直接复用，省一次请求。
    quota_before = status.quota_usd if status.quota_usd is not None else _read_quota(client)
    try:
        notify("checkin_start")
        # 站点脚本优先：它可能实现了该 fork 私改的签到流程（如图形验证码）。
        # browser_script 的 API-first 调用关闭此 hook，避免把同一浏览器脚本误当 HTTP hook。
        reward = _script_checkin(site, client, turnstile) if allow_site_hook else None
        if reward is None:
            reward = client.do_checkin(turnstile)
        notify("reward", reward)
    except ApiError as exc:
        notify("checkin_error", exc)
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
            return CheckinResult(site.name, base_url, "need_login", _need_login_message(site), detail=exc.payload)
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
    # 只有确实拿到非零金额才写进消息：站点签到成功但不回具体金额时常给 0，
    # 拼成「获得额度：$0.0000」既不是事实也让人以为签到失败。
    if has_awarded_amount(reward.quota_awarded, is_usd=client.quota_is_usd):
        awarded = format_usd(reward.quota_awarded, is_usd=client.quota_is_usd)
        return CheckinResult(site.name, base_url, "success", f"签到成功，获得额度：{awarded}", detail=detail)
    if reward.quota_awarded is not None and not reward.checkin_unconfirmed:
        return CheckinResult(site.name, base_url, "success", "签到成功（站点未返回本次获得额度）。", detail=detail)

    # 响应里没有任何签到成立的正面证据（无奖励额度、无连续天数、无已签标记）。
    # 这类响应过去被无条件报成「签到成功」，但实测存在 HTTP 200 却未真正发放的情况
    # （站点静默拒绝/端点非签到接口），导致「显示成功但额度没到账」。
    # 此时用签到前后的余额差与状态接口做交叉验证，拿不到证据就不谎报成功。
    if reward.checkin_unconfirmed:
        confirmed_quota = _quota_increased(client, quota_before, detail)
        if confirmed_quota is not None:
            # _quota_increased 返回的 delta **已经是美元**（内部走过 quota_to_usd）。
            # 之前这里跟着 client.quota_is_usd 传给 format_usd，newapi 站点
            # （quota_is_usd=False）会被再除一次 500000，$0.50 的真实增量显示成
            # $0.0000——与「0 值」是两个独立成因，症状却一样。
            awarded = format_usd(confirmed_quota, is_usd=True)
            # detail 里的额度单位由 detail["quota_is_usd"] 统一描述（汇总层只认这一个
            # 开关），因此存回站点原始单位，而不是新造一个只有这里会写的标记键。
            detail["quota_awarded"] = (
                confirmed_quota if client.quota_is_usd else confirmed_quota * QUOTA_UNIT
            )
            return CheckinResult(site.name, base_url, "success", f"签到成功，获得额度：{awarded}", detail=detail)
        if _checked_in_after(client):
            return CheckinResult(site.name, base_url, "success", "签到成功（站点已标记今日已签到）。", detail=detail)
        detail["checkin_unconfirmed"] = True
        return CheckinResult(
            site.name,
            base_url,
            "error",
            "签到接口返回成功但未发放额度，站点也未标记今日已签到；"
            "该站点可能需要在网页手动签到，或签到接口已变更。",
            detail=detail,
        )
    return CheckinResult(site.name, base_url, "success", "签到成功。", detail=detail)


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    if not credentials_ready(site, profile):
        return CheckinResult(site.name, site.base_url, "need_login", f"未找到 auth_method={site.auth_method} 所需的有效凭据，请先配置。")

    try:
        client = build_http_client(site, profile)
    except BrowserAuthError as exc:
        return CheckinResult(site.name, site.base_url, exc.status, exc.message, detail=exc.detail)
    return run_http_flow(site, client, turnstile)


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
                return QueryStatus(ok=False, message=_need_login_message(site), status="need_login", detail=exc.payload)
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
