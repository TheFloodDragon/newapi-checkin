#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时间戳工具：统一写 UTC，兼容读取旧的无时区 ISO。

为什么需要：状态缓存此前用本地时间的 naive ISO 字符串，并且直接按字符串比较新旧。
CI 用 Asia/Shanghai、GUI 用用户本地时区，字典序不等于真实先后顺序；「今日已签到」
也无法在跨日后失效。这里统一时间语义：

- 落盘统一 UTC，形如 ``2026-07-29T03:20:15Z``；
- 读取兼容 ``Z``、``+08:00`` 与旧的无时区值（按本地时区解释）；
- 业务日按固定业务时区（默认 Asia/Shanghai，与 workflow 的 TZ 一致）计算，
  避免 UTC 跨日导致「昨天/今天」判断与用户直觉不符。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

BUSINESS_TZ_NAME = os.environ.get("CHECKIN_BUSINESS_TZ", "Asia/Shanghai")
_FALLBACK_BUSINESS_TZ = timezone(timedelta(hours=8))


def business_timezone() -> timezone:
    """业务时区；zoneinfo 不可用时回落到固定 +08:00。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(BUSINESS_TZ_NAME)  # type: ignore[return-value]
    except Exception:
        return _FALLBACK_BUSINESS_TZ


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(moment: datetime | None = None) -> str:
    """带时区的 UTC ISO 字符串（秒精度，以 Z 结尾）。"""
    value = moment or utc_now()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: object) -> datetime | None:
    """解析 ISO 时间戳；无时区值按本地时区解释。无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # 旧数据是本地时间：按本地时区解释，而不是直接判为损坏。
        parsed = parsed.astimezone()
    return parsed


def business_date(moment: datetime | None = None) -> str:
    """业务时区下的日期（YYYY-MM-DD）。"""
    value = moment or utc_now()
    if value.tzinfo is None:
        value = value.astimezone()
    return value.astimezone(business_timezone()).date().isoformat()


def business_date_of(value: object) -> str:
    """从时间戳推导业务日；无法解析返回空串。"""
    parsed = parse_timestamp(value)
    return business_date(parsed) if parsed is not None else ""


def is_today(value: object, *, today: date | None = None) -> bool:
    """时间戳是否属于业务时区的今天；无法解析视为否。"""
    stamp = business_date_of(value)
    if not stamp:
        return False
    reference = today.isoformat() if today is not None else business_date()
    return stamp == reference
