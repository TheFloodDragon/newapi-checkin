#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SOTA Model（sotamodel.net）每日签到。

该站是 New API 派生站，但标准签到接口被关闭（``GET /api/user/checkin`` 固定回
``签到功能未启用``），签到被搬到 ``/agents`` 页面的独立端点：

- ``GET  /api/user/sota-agent-checkin`` → ``{checked_in_today, reward_credits, reward_quota}``
- ``POST /api/user/sota-agent-checkin`` → ``{reward_credits, quota_awarded, current_quota}``

因此声明 OWNS_HTTP_FLOW，让通用层不要再探测已被禁用的标准端点。请求一律走传入的
newapi 客户端 ``client.request()``：认证头、代理、TLS、HTTP 原始回执日志与 WAF
判定全部沿用既有实现，不在这里另拼一套 HTTP。

奖励金额由后台按星期配置（``checkin_setting.sota_agent_{monday..sunday}_credits``），
每天可不同，脚本只如实上报服务端返回值。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from providers.base import (  # noqa: E402
    QUOTA_UNIT,
    ApiError,
    CheckinReward,
    format_usd,
    unwrap_data,
)

SITE_LABEL = "SOTA Model"
# 状态查询、签到与结果确认全部由本脚本调用站点实际启用的端点完成；通用层不得再去
# 请求被站点关闭的 /api/user/checkin，否则只会制造「签到功能未启用」的噪声失败。
OWNS_HTTP_FLOW = True
CHECKIN_ROUTE = "/api/user/sota-agent-checkin"
# 站点当前 turnstile_check=false，但接口保留该校验；被要求人机验证时按需验证归类，
# 不能当成签到失败或谎报成功。
TURNSTILE_MARKERS = ("turnstile", "人机验证")
ALREADY_MARKERS = ("already", "已签到", "今日已", "重复签到")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _data(payload: Any) -> dict[str, Any]:
    value = unwrap_data(payload)
    return value if isinstance(value, dict) else {}


def _awarded_quota(data: dict[str, Any]) -> float | None:
    """本次/今日奖励，统一换算为站点内部 quota 单位。

    detail 的额度单位由 ``quota_is_usd`` 统一描述，newapi 客户端为内部 quota，
    因此这里不能混入美元数值：只在站点仅回 credits（美元）时才乘回 QUOTA_UNIT。
    """
    for key in ("quota_awarded", "reward_quota"):
        value = _number(data.get(key))
        if value is not None:
            return value
    credits = _number(data.get("reward_credits"))
    return credits * QUOTA_UNIT if credits is not None else None


def _amount_text(data: dict[str, Any]) -> str:
    """奖励金额的美元展示；拿不到数值时返回空串（不编造 $0）。"""
    credits = _number(data.get("reward_credits"))
    if credits is not None and credits != 0:
        return format_usd(credits, is_usd=True)
    quota = _awarded_quota(data)
    if quota is not None and quota != 0:
        return format_usd(quota, is_usd=False)
    return ""


def _message(data: dict[str, Any], *, already: bool) -> str:
    prefix = "今日已签到" if already else "签到成功"
    amount = _amount_text(data)
    if not amount:
        return prefix
    # 已签到时金额是「今日这一档的奖励」，不是本次新到账，措辞需要区分。
    return f"{prefix}，今日奖励 {amount}" if already else f"{prefix}，获得 {amount}"


def _current_quota(client: Any, data: dict[str, Any]) -> Any:
    """当前余额（站点内部 quota）。

    签到响应自带 current_quota 时直接用；否则读站点原生 ``/api/user/self``
    —— 那是该站仍然启用的标准端点，与被关闭的签到端点无关。读取失败按未知处理。
    """
    value = data.get("current_quota")
    if value is not None:
        return value
    fetch_user = getattr(client, "fetch_user", None)
    if not callable(fetch_user):
        return None
    try:
        return fetch_user().quota_raw
    except Exception:  # noqa: BLE001 - 余额只是补充信息，不能影响签到结论
        return None


def _detail(data: dict[str, Any], *, already: bool, signal: str) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "result_message": _message(data, already=already),
        "checked_in_today": True,
        "completion_signal": signal,
        "checkin_route": CHECKIN_ROUTE,
    }
    credits = _number(data.get("reward_credits"))
    if credits is not None:
        # 已签到时不写 quota_awarded：那会让汇总层把「今日这一档的奖励」当成本次到账。
        detail["today_reward_credits" if already else "reward_credits"] = credits
    return detail


def _reward(
    client: Any,
    data: dict[str, Any],
    *,
    already: bool,
    signal: str,
    raw: Any,
) -> CheckinReward:
    return CheckinReward(
        already_done=already,
        quota_awarded=None if already else _awarded_quota(data),
        current_quota=_current_quota(client, data),
        raw=raw,
        extra=_detail(data, already=already, signal=signal),
    )


def _contains(exc: ApiError, markers: tuple[str, ...]) -> bool:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    text = " ".join(
        str(value or "")
        for value in (exc.message, payload.get("message"), payload.get("reason"))
    ).casefold()
    return any(marker in text for marker in markers)


def _turnstile_error(exc: ApiError) -> ApiError:
    """把服务端的 Turnstile 拒绝换成可操作提示（保留关键词以便统一分类）。"""
    return ApiError(
        exc.status,
        exc.payload,
        f"{SITE_LABEL} 签到要求 Cloudflare Turnstile 人机验证（服务端回执：{exc.message}）。"
        "纯 HTTP 无法自动完成，请用 --turnstile 传入令牌或在网页手动签到。",
    )


def _read_status(client: Any) -> tuple[dict[str, Any], Any]:
    raw = client.request("GET", CHECKIN_ROUTE)
    return _data(raw), raw


def do_checkin(client: Any, log: Any = None) -> CheckinReward:
    """执行 SOTA Model 的 agent 每日签到；由 api / API-first 链路调用。"""
    _log = log if callable(log) else (lambda _message: None)

    _log(f"读取 {SITE_LABEL} agent 签到状态（{CHECKIN_ROUTE}）")
    try:
        status, status_raw = _read_status(client)
    except ApiError as exc:
        if _contains(exc, TURNSTILE_MARKERS):
            raise _turnstile_error(exc) from exc
        raise

    if status.get("checked_in_today") is True:
        reward = _reward(
            client, status, already=True, signal="sota_agent_status", raw=status_raw
        )
        _log(str(reward.extra["result_message"]))
        return reward

    _log("今日尚未签到，调用 agent 签到接口")
    try:
        # 不做非幂等自动重试：重复 POST 在服务端是一次真实的签到写入。
        action_raw = client.request("POST", CHECKIN_ROUTE, retry_non_idempotent=False)
    except ApiError as exc:
        if _contains(exc, TURNSTILE_MARKERS):
            raise _turnstile_error(exc) from exc
        # POST 结果不确定（网络中断 / 服务端 5xx / 重复提交）时先回读状态：
        # 服务端已记账就按已签到返回，避免把已成功的签到报成失败。
        already = _contains(exc, ALREADY_MARKERS)
        after: dict[str, Any] = {}
        after_raw: Any = None
        try:
            after, after_raw = _read_status(client)
        except ApiError:
            after, after_raw = {}, None
        if after.get("checked_in_today") is True or already:
            reward = _reward(
                client,
                after or status,
                already=True,
                signal="sota_agent_status_after_post",
                raw=after_raw if after_raw is not None else exc.payload,
            )
            _log(str(reward.extra["result_message"]))
            return reward
        raise

    action = _data(action_raw)
    if _awarded_quota(action) is None:
        # 响应里没有任何金额证据时不谎报成功：回读状态确认服务端是否真的记账。
        after, after_raw = _read_status(client)
        if after.get("checked_in_today") is not True:
            raise ApiError(
                None,
                {"action": action, "status": after},
                f"{SITE_LABEL} 签到接口返回成功，但未给出奖励额度且状态接口未确认已签到",
                transient=True,
            )
        reward = _reward(
            client, after, already=True, signal="sota_agent_status_after_post", raw=after_raw
        )
        _log(str(reward.extra["result_message"]))
        return reward

    reward = _reward(
        client, action, already=False, signal="sota_agent_checkin_response", raw=action_raw
    )
    _log(str(reward.extra["result_message"]))
    return reward
