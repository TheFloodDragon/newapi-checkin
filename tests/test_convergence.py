# -*- coding: utf-8 -*-
"""P0 收敛回归：normalize_base_url / VERIFICATION_PATTERNS / 额度格式化唯一实现。

守护 docs/OPTIMIZATION.md §二 P0-1/2/3 的收敛结果，防止再次漂移。
"""

from __future__ import annotations

import accounts_store
import checkin
import run__all_checkin as runner
from gui import core as gui_core
from providers import base
from providers.actions import api as api_action
from providers.actions import visit as visit_action
from providers.profiles import newapi, sub2api


def test_normalize_base_url_single_source() -> None:
    assert checkin.normalize_base_url is accounts_store.normalize_base_url
    assert base.normalize_base_url is accounts_store.normalize_base_url
    assert accounts_store.normalize_base_url(" example.com/ ") == "https://example.com"
    assert accounts_store.normalize_base_url(None) == ""  # 最防御版本：容忍 None


def test_verification_patterns_single_source() -> None:
    # api 动作层的词表是死代码，已删除
    assert not hasattr(api_action, "VERIFICATION_PATTERNS")
    # newapi 直接使用唯一词表（不追加宽泛词，见 classify 顺序注释）
    assert newapi.VERIFICATION_PATTERNS is base.VERIFICATION_PATTERNS
    # visit / sub2api 在唯一词表基础上按语境追加
    assert visit_action.VERIFICATION_PATTERNS[: len(base.VERIFICATION_PATTERNS)] == base.VERIFICATION_PATTERNS
    assert sub2api.VERIFICATION_PATTERNS[: len(base.VERIFICATION_PATTERNS)] == base.VERIFICATION_PATTERNS
    assert visit_action.VERIFICATION_PATTERNS[len(base.VERIFICATION_PATTERNS):] == ["验证"]
    assert sub2api.VERIFICATION_PATTERNS[len(base.VERIFICATION_PATTERNS):] == ["验证", "verify"]
    # 合并后的高置信标记进入唯一词表；宽泛词不进
    assert {"人机", "captcha", "turnstile"} <= set(base.VERIFICATION_PATTERNS)
    assert "验证" not in base.VERIFICATION_PATTERNS
    assert "verify" not in base.VERIFICATION_PATTERNS


def test_format_usd() -> None:
    assert base.format_usd(500000) == "$1.00"          # 内部 quota /500000
    assert base.format_usd(2.5, is_usd=True) == "$2.50"
    assert base.format_usd(1000) == "$0.0020"          # <0.01 → 四位小数
    assert base.format_usd("n/a", fallback="原样") == "原样"
    assert base.format_usd(None) == ""
    assert base.format_usd(True) == "True"             # bool 视为非数字


def test_detail_quota_helpers() -> None:
    nested = {"raw": {"data": {"remaining_quota": 1000000}}}
    assert base.detail_current_quota(nested) == 1000000
    assert base.detail_quota_usd(nested) == 2.0
    assert base.detail_quota_usd({"current_quota": 3.5, "quota_is_usd": True}) == 3.5
    assert base.detail_quota_usd(None) is None
    assert base.detail_quota_awarded({"detail": [{"quota_awarded": 5000}]}) == 5000
    assert base.detail_is_usd({"a": [{"quota_is_usd": True}]}) is True


def test_runner_aliases_delegate_to_base() -> None:
    assert runner.detail_is_usd is base.detail_is_usd
    assert runner.extract_quota_awarded is base.detail_quota_awarded
    assert runner.extract_current_quota is base.detail_current_quota
    assert runner.format_quota(500000) == "$1.00"
    assert runner.format_quota("") == ""               # 空值 → 空串（报表约定）
    assert runner.format_quota("oops") == "oops"       # 非数字 → value_to_text 回退


def test_gui_delegates_match_base() -> None:
    assert gui_core.format_usd(246.1) == base.format_usd(246.1, is_usd=True) == "$246.10"
    assert gui_core.detail_quota_usd({"current_quota": 500000}) == base.detail_quota_usd({"current_quota": 500000})


def test_browser_session_quota_display() -> None:
    from browser import session as browser_session

    assert browser_session.quota_to_usd(500000) == "$1.00"
    assert browser_session.quota_to_usd("x") == "x"
