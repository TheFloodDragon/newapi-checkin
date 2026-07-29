# -*- coding: utf-8 -*-
"""带时区时间戳与业务日语义。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import time_utils


def test_utc_iso_is_timezone_aware_and_z_suffixed() -> None:
    text = time_utils.utc_iso(datetime(2026, 7, 29, 3, 20, 15, tzinfo=timezone.utc))
    assert text == "2026-07-29T03:20:15Z"


def test_parse_handles_z_and_offset_forms() -> None:
    a = time_utils.parse_timestamp("2026-07-29T03:20:15Z")
    b = time_utils.parse_timestamp("2026-07-29T11:20:15+08:00")
    assert a is not None and b is not None
    assert a == b


def test_parse_treats_naive_value_as_local_time() -> None:
    parsed = time_utils.parse_timestamp("2026-07-29T11:20:15")
    assert parsed is not None and parsed.tzinfo is not None


def test_parse_rejects_garbage() -> None:
    assert time_utils.parse_timestamp("not-a-time") is None
    assert time_utils.parse_timestamp("") is None
    assert time_utils.parse_timestamp(None) is None


def test_business_date_uses_business_timezone_not_utc() -> None:
    # UTC 前一天 18:00 == 北京次日 02:00：业务日必须按业务时区判定
    moment = datetime(2026, 7, 28, 18, 0, 0, tzinfo=timezone.utc)
    assert time_utils.business_date(moment) == "2026-07-29"


def test_is_today_matches_same_business_day_across_offsets() -> None:
    today = time_utils.business_date()
    assert time_utils.is_today(time_utils.utc_iso()) is True
    assert time_utils.is_today(f"{today}T00:30:00+08:00") is True
    assert time_utils.is_today("not-a-time") is False


def test_is_today_rejects_yesterday() -> None:
    yesterday = time_utils.utc_now() - timedelta(days=1)
    assert time_utils.is_today(time_utils.utc_iso(yesterday)) is False
