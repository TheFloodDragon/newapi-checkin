#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""New API 接口签到的内置验证机制路由器。

`verification_mode=auto` 根据公开配置自动分流；指定机制时先尝试该机制，只有确认
不适用（端点不存在、开关/协议不匹配）才回落自动分流。机制已经适用但验证失败时
保留原错误，避免重复挑战和误提交。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from accounts_store import normalize_verification_mode  # noqa: E402
from providers.base import ApiError, CheckinReward, contains_any, unwrap_data  # noqa: E402

_NOT_APPLICABLE_PATTERNS = (
    "invalid url",
    "404",
    "405",
    "not found",
    "page not found",
    "no route",
    "站点未提供已知的签到验证码端点",
    "挑战接口异常",
)


def _status_data(client: Any) -> dict[str, Any]:
    try:
        data = unwrap_data(client.request("GET", "/api/status"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _checkin_status(client: Any) -> dict[str, Any]:
    """取签到状态响应。

    优先复用 ``fetch_status`` 已取过的缓存：该响应在一次签到里必然被读过一次，
    重复请求既慢又可能触发站点限流。没有缓存时不额外发请求，返回空字典。
    """
    data = getattr(client, "_checkin_status_data", None)
    return data if isinstance(data, dict) else {}


def _code_required(checkin_status: dict[str, Any]) -> bool:
    """签到状态是否声明「必须提交每日口令」。

    实测 xingya.site：签到状态返回 ``code_required: true``，而 /api/status 里既没有
    验证码开关也没有 Turnstile。它**不是图形验证码**——前端只渲染一个「今日签到口令」
    文本框（"Code is valid for today only"），把用户输入按 ``{"code": ...}`` 提交到
    ``POST /api/user/checkin``，站点没有任何取图/取码端点。口令由站点站外发布，
    程序无法自行获得，因此只能由配置提供。
    """
    return bool(checkin_status.get("code_required"))


def _configured_checkin_code(client: Any) -> str:
    """读取用户为本站配置的今日签到口令；未配置返回空串。"""
    args = getattr(getattr(client, "site", None), "script_args", None)
    if not isinstance(args, dict):
        return ""
    for key in ("checkin_code", "daily_code", "code"):
        value = str(args.get(key) or "").strip()
        if value:
            return value
    return ""


def _auto_modes(options: dict[str, Any]) -> list[str]:
    modes: list[str] = []
    captcha_type = str(options.get("captcha_type") or "").strip().lower()
    captcha_enabled = bool(
        options.get("captcha_checkin_enabled")
        or options.get("checkin_captcha_enabled")
    )
    if captcha_type == "click-shape":
        modes.append("click_shape")
    if options.get("turnstile_check") and options.get("turnstile_site_key"):
        modes.append("turnstile")
    if captcha_enabled and captcha_type != "click-shape":
        # 两种字符图没有统一类型字段；按机制端点无副作用探测。
        modes.extend(("bitmap_code", "string_captcha"))
    return modes


def _not_applicable(error: ApiError) -> bool:
    text = f"{error.message} {error.payload}".lower()
    return error.status in {404, 405} or contains_any(text, _NOT_APPLICABLE_PATTERNS)


def _run_mode(
    client: Any,
    mode: str,
    options: dict[str, Any],
    log: Any = None,
) -> CheckinReward | None:
    if mode == "turnstile":
        from scripts import newapi_turnstile

        return newapi_turnstile.turnstile_checkin(
            client, status_data=options, log=log
        )
    if mode in {"bitmap_code", "string_captcha", "click_shape"}:
        from scripts import newapi_captcha

        return newapi_captcha.mode_checkin(client, mode, log=log)
    return None


def do_checkin(
    client: Any,
    log: Any = None,
    *,
    status_data: dict[str, Any] | None = None,
    preferred: str | None = None,
) -> CheckinReward | None:
    def _log(message: str) -> None:
        if log:
            log(message)

    options = status_data if isinstance(status_data, dict) else _status_data(client)
    try:
        setattr(client, "_captcha_status_options", options)
    except Exception:
        pass

    selected = normalize_verification_mode(
        preferred if preferred is not None else getattr(client.site, "verification_mode", "auto")
    )
    checkin_status = _checkin_status(client)
    code_flag = _code_required(checkin_status)
    detected = _auto_modes(options)
    order: list[str] = []
    if selected != "auto":
        order.append(selected)
    order.extend(mode for mode in detected if mode not in order)

    _log(
        f"验证路由：preferred={selected}，auto_detected={detected or ['none']}，"
        f"attempt_order={order or ['default_checkin']}"
        + ("，签到状态 code_required=true" if code_flag else "")
    )

    # 每日口令与图形验证码是两套机制：前者站点只发布在站外，程序拿不到。裸提交必被
    # 服务端拒（实测星芽回「签到验证码不正确」），所以要么用配置的口令提交，要么直接
    # 给出可操作结论，不再浪费一次写请求。
    if code_flag:
        code = _configured_checkin_code(client)
        if not code:
            raise ApiError(
                None,
                {"code_required": True},
                "本站签到需要「今日签到口令」（站点站外发布、当日有效），程序无法自动获取。"
                "请在该站点配置 script_args.checkin_code 填入今日口令，或关闭/容错该站点。",
                need_config=True,
            )
        _log("使用配置的今日签到口令提交（script_args.checkin_code）")
        return client._reward_from(client._legacy_checkin(code=code))
    if not order:
        return None

    for mode in order:
        _log(f"尝试验证机制 {mode}")
        try:
            reward = _run_mode(client, mode, options, log=log)
        except ApiError as exc:
            if _not_applicable(exc):
                _log(f"验证机制 {mode} 不适用（{exc.message}），继续自动分流")
                continue
            raise
        if reward is not None:
            extra = getattr(reward, "extra", None)
            if isinstance(extra, dict):
                extra.setdefault("verification_mode", mode)
            return reward
        _log(f"验证机制 {mode} 未声明适用，继续自动分流")
    return None
