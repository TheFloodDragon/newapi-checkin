#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sub2API 协议语义层：响应解析、错误归类、端点与词表。

与 ``sub2api.py`` 的分工：
- 本模块回答「Sub2API 的响应/错误**意味着什么**」，全部是纯函数，不做任何 I/O；
- ``sub2api.py`` 回答「**怎么**和站点通信」（会话、cookie jar、token 续期、重试）。

为什么这样切而不是按 client/profile 切：各 fork 的响应形态差异（余额藏在
``balance`` / ``remaining`` / ``items[].user.balance``、签到端点是 ``/check-in``
还是 ``/play/checkin``）是这个 profile 里最容易出错、也最需要独立回归的部分，
但它们与网络栈毫无关系。把它们摘出来后可以直接对着字典断言，不必构造客户端。

注意：``sub2api.py`` 会 re-export 本模块的名字（含下划线别名），历史调用与
测试中的 ``monkeypatch.setattr(sub2api, ...)`` 保持有效。
"""

from __future__ import annotations

import json
from typing import Any

from ..base import (
    USER_AGENT,
    ApiError,
    CheckinReward,
    contains_any,
)
from ..base import VERIFICATION_PATTERNS as _BASE_VERIFICATION_PATTERNS

API_PREFIX = "/api/v1"

# 各 Sub2API fork 的签到端点不统一，按顺序探测（第一个可用的会被缓存复用）：
# - /check-in        ：100xLabs 等 fork 的签到扩展
# - /play/checkin    ：极速蹬（jisudeng）把签到挂在 play 模块下
# 每项为 (签到 POST 路径, 状态 GET 路径)；状态路径为 None 表示该 fork 无状态接口。
CHECKIN_ENDPOINTS: tuple[tuple[str, str | None], ...] = (
    ("/check-in", "/check-in/status"),
    ("/play/checkin", "/play/checkin/status"),
)

LOGIN_PATTERNS = ["unauthorized", "登录", "token", "expired", "invalid", "forbidden", "无效", "过期"]
# 在唯一词表基础上追加「验证 / verify」：sub2api 的 classify 先判 LOGIN 再判验证，
# token 失效类消息已被 need_login 拦截，宽泛词在此语境安全（保持既有行为）。
VERIFICATION_PATTERNS = [*_BASE_VERIFICATION_PATTERNS, "验证", "verify"]
ALREADY_DONE_PATTERNS = ["already", "已签到", "今日已", "已领取"]
UNSUPPORTED_CHECKIN_PATTERNS = [
    "404", "405", "not found", "no route", "route not found",
    "method not allowed", "不存在", "未找到",
]

# 「今日已签到」在各 fork 里的字段名。100xLabs 用 checked_in_today，
# 极速蹬的 play/checkin/status 用 today_checked / has_checked_in。
CHECKED_IN_KEYS = (
    "checked_in_today",
    "checked_in",
    "today_checked",
    "has_checked_in",
    "is_checked_in",
    "checked",
)

# 余额字段的候选名（按优先级）。
BALANCE_KEYS = ("balance", "remaining", "credit", "credits", "quota")


# ── 请求头 ───────────────────────────────────────────────────────────────────
def base_headers(base_url: str) -> dict[str, str]:
    """Sub2API 的公共请求头。

    唯一实现：客户端请求与 profile 的纯 HTTP 账密登录此前各写了一份完全相同的
    5 个头，改 User-Agent 或 Referer 时必须同步改两处，漏改会让两条路径呈现
    不同指纹（部分 fork 会因此判会话异常）。
    """
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": base_url,
        "Referer": base_url + "/",
    }


# ── 文本 / 错误描述 ──────────────────────────────────────────────────────────
def brief(value: Any, limit: int = 160) -> str:
    """把响应体压成一行短文本，供失败日志引用（不脱敏，调用方统一走 mask_secrets）。"""
    if value is None:
        return "<空响应>"
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:
        text = str(value)
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def describe_api_error(exc: ApiError, endpoint: str) -> str:
    """把 ApiError 摊平成可诊断的一行：端点 + 状态码 + 服务端 message/reason。

    以前失败只剩「refresh_token 已失效」一句结论，状态码和服务端判据（如
    REFRESH_TOKEN_INVALID）全被吞掉，排查时只能另写脚本手打端点才看得到真实原因。
    """
    parts = [f"{endpoint} 请求失败"]
    status = getattr(exc, "status", None)
    if status:
        parts.append(f"HTTP {status}")
    message = str(getattr(exc, "message", "") or "").strip()
    if message:
        parts.append(message)
    # 响应体在 ApiError.payload（见 providers/base.py 的 ApiError.__init__）。
    # reason 是 sub2api 区分「凭据被服务端作废」与「请求本身有问题」的关键字段，
    # 它只在响应体里，不在 message 中。
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        reason = str(payload.get("reason") or payload.get("code") or "").strip()
        if reason and reason not in message:
            parts.append(f"reason={reason}")
    elif payload:
        parts.append(brief(payload))
    if getattr(exc, "transient", False):
        parts.append("（可重试的临时故障）")
    return "；".join(parts)


# ── 数值 / 余额提取 ──────────────────────────────────────────────────────────
def to_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def pick_first_number(
    data: Any,
    keys: tuple[str, ...] = ("remaining", "balance", "quota"),
) -> float | int | None:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                nested = pick_first_number(value, keys)
                if nested is not None:
                    return nested
            number = to_number(value)
            if number is not None:
                return number
        for value in data.values():
            nested = pick_first_number(value, keys)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = pick_first_number(item, keys)
            if nested is not None:
                return nested
    return None


def extract_usage_user_balance(data: Any) -> float | int | None:
    """优先从 /api/v1/usage 的 items[].user.balance 提取余额。

    用量记录里还会嵌套 api_key.quota、group.daily_limit_usd 等数字字段；不能做
    盲目递归，否则可能把 API Key 配额 0 误当成用户余额。
    """
    items: Any = None
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        balance = pick_first_number(user, BALANCE_KEYS)
        if balance is not None:
            return balance
    return None


def extract_standard_balance(data: Any) -> float | int | None:
    """从标准 Sub2API JWT 接口返回中提取余额。

    源码中：
    - /api/v1/user/profile 直接返回 user，含 balance；
    - /api/v1/auth/me 直接返回当前用户，含 balance；
    - /api/v1/usage 返回分页 items，单条记录里包含 user.balance。
    """
    if not isinstance(data, (dict, list)):
        return None
    usage_balance = extract_usage_user_balance(data)
    if usage_balance is not None:
        return usage_balance
    return pick_first_number(data, BALANCE_KEYS)


def extract_username(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("username", "name", "email", "id", "user_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    user = data.get("user")
    if isinstance(user, dict):
        return extract_username(user)
    return ""


def extract_api_key_usage(payload: Any) -> tuple[bool, float | int | None, str] | None:
    """解析 API Key 网关的 ``GET /v1/usage``（不是前端 auth_token 接口）。

    返回 (是否有效, 余额, 单位)；识别不出余额返回 None。
    """
    if not isinstance(payload, dict):
        return None
    quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
    remaining = to_number(payload.get("remaining"))
    if remaining is None:
        remaining = to_number(quota.get("remaining"))
    if remaining is None:
        remaining = to_number(payload.get("balance"))
    if remaining is None:
        return None
    unit = str(payload.get("unit") or quota.get("unit") or "USD")
    if "is_active" in payload:
        is_valid = bool(payload.get("is_active"))
    elif "isValid" in payload:
        is_valid = bool(payload.get("isValid"))
    else:
        is_valid = True
    return is_valid, remaining, unit


def checked_in_flag(data: Any) -> bool | None:
    """从签到状态响应里读「今日是否已签」；无法判断返回 None。"""
    if not isinstance(data, dict):
        return None
    for key in CHECKED_IN_KEYS:
        if key in data:
            value = data.get(key)
            if value is not None:
                return bool(value)
    return None


# ── 错误归类 ─────────────────────────────────────────────────────────────────
def classify_error(error: ApiError) -> str:
    """把 ApiError 归类为 already_done / not_open / need_login / need_verification / error。"""
    if error.not_open:
        return "not_open"
    if contains_any(error.message, ALREADY_DONE_PATTERNS):
        return "already_done"
    if (
        error.status == 401
        or contains_any(error.message, LOGIN_PATTERNS)
        or contains_any(str(error.payload), ["unauthorized"])
    ):
        return "need_login"
    if contains_any(error.message, VERIFICATION_PATTERNS):
        return "need_verification"
    return "error"


def is_unsupported_checkin_error(error: ApiError) -> bool:
    """该错误是否表示「这个 fork 没有这个签到端点」（可继续试下一个）。"""
    return (
        error.status in {404, 405}
        or contains_any(error.message, UNSUPPORTED_CHECKIN_PATTERNS)
        or contains_any(str(error.payload), UNSUPPORTED_CHECKIN_PATTERNS)
    )


# ── 签到响应 → CheckinReward ─────────────────────────────────────────────────
def reward_from(data: Any) -> CheckinReward:
    """把签到响应解析为归一化奖励；无正面证据时标记 checkin_unconfirmed。"""
    if not isinstance(data, dict):
        # 非 dict 响应（如 HTML、纯文本 "ok"）不构成签到成立的证据。
        return CheckinReward(raw=data, checkin_unconfirmed=True)
    reward = data.get("reward_amount")
    if reward is None:
        reward = data.get("today_reward")
    extra: dict[str, Any] = {}
    if data.get("total_reward") is not None:
        extra["total_reward"] = data["total_reward"]
    if data.get("current_streak") is not None:
        extra["consecutive_days"] = data["current_streak"]
    if data.get("total_check_in_days") is not None:
        extra["total_checkins"] = data["total_check_in_days"]

    already = bool(data.get("already_checked_in"))
    # 签到成立需要正面证据。曾出现过的误报链条：某些 fork 对未生效的签到请求
    # 也回 HTTP 200 且 body 里没有任何奖励字段（例如 {} 或 {"data":null}），
    # 旧实现把它当成 CheckinReward() 空成功，最终报「签到成功」但额度未到账。
    # 因此这里要求至少命中一项可信信号，否则标记 checkin_unconfirmed，
    # 交由 action 层改判（见 providers/actions/api.py）。
    confirmed = (
        already
        or reward is not None
        or data.get("balance") is not None
        or bool(extra)
        or bool(data.get("checked_in_today"))
        or bool(data.get("today_checked"))
        or bool(data.get("success") is True)
        or bool(data.get("checkin_date") or data.get("checked_at") or data.get("check_in_at"))
    )
    return CheckinReward(
        already_done=already,
        quota_awarded=reward,
        current_quota=data.get("balance"),
        raw=data,
        extra=extra,
        checkin_unconfirmed=not confirmed,
    )


__all__ = [
    "API_PREFIX",
    "CHECKIN_ENDPOINTS",
    "LOGIN_PATTERNS",
    "VERIFICATION_PATTERNS",
    "ALREADY_DONE_PATTERNS",
    "UNSUPPORTED_CHECKIN_PATTERNS",
    "CHECKED_IN_KEYS",
    "BALANCE_KEYS",
    "base_headers",
    "brief",
    "describe_api_error",
    "to_number",
    "pick_first_number",
    "extract_usage_user_balance",
    "extract_standard_balance",
    "extract_username",
    "extract_api_key_usage",
    "checked_in_flag",
    "classify_error",
    "is_unsupported_checkin_error",
    "reward_from",
]
