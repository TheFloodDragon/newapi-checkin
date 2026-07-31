# -*- coding: utf-8 -*-
"""visit 额度基线必须按账号隔离（回归）。

同一 base_url 下配多个账号是常见用法（仓库其它缓存 status_key / task_key /
token_cache 都已按渠道区分）。visit 以前只用 base_url 做 key，后跑的账号会拿
前一个账号的余额当基线，于是虚报「额度增加」或把真实发放判成「无变化」。
"""

from __future__ import annotations

import json
from pathlib import Path

from providers.actions import visit
from providers.base import SiteConfig


def _isolate_state(tmp_path: Path, monkeypatch) -> Path:
    state_path = tmp_path / "cache" / "login_grant_state.json"
    monkeypatch.setattr(visit, "STATE_PATH", state_path)
    monkeypatch.setattr(visit, "LEGACY_STATE_PATH", tmp_path / "login_grant_state.json")
    return state_path


def _site(user_id: str = "", **kwargs) -> SiteConfig:
    return SiteConfig(
        name=kwargs.pop("name", "共享站点"),
        base_url="https://shared.invalid",
        site_profile="newapi",
        auth_method=kwargs.pop("auth_method", "cookie"),
        checkin_action="visit",
        user_id=user_id,
        **kwargs,
    )


def test_same_site_different_user_ids_get_separate_keys() -> None:
    first = visit._state_key("https://shared.invalid", visit._account_identity(_site("1001")))
    second = visit._state_key("https://shared.invalid", visit._account_identity(_site("1002")))
    assert first != second
    assert first == "https://shared.invalid|1001"


def test_account_identity_prefers_user_id_then_oauth_then_name() -> None:
    assert visit._account_identity(_site("1001")) == "1001"
    assert (
        visit._account_identity(
            _site("", auth_method="oauth", oauth_provider="linuxdo", oauth_account="alt")
        )
        == "linuxdo:alt"
    )
    # oauth_provider 有默认值 linuxdo，非 OAuth 站点不能因此被归到同一身份，
    # 否则同址多账号仍旧共用 base|linuxdo:default 一条基线。
    assert visit._account_identity(_site("", name="仅有名称")) == "仅有名称"
    assert visit._account_identity(_site("", name="甲")) != visit._account_identity(
        _site("", name="乙")
    )


def test_quota_baselines_do_not_cross_accounts(tmp_path: Path, monkeypatch) -> None:
    state_path = _isolate_state(tmp_path, monkeypatch)
    key_a = visit._state_key("https://shared.invalid", "1001")
    key_b = visit._state_key("https://shared.invalid", "1002")

    # A 账号先记录 100
    assert visit._record_state(key_a, {"quota": 100}) == {}
    # B 账号首次运行不得读到 A 的 100（否则会算出 -95 的假「额度变化」）
    assert visit._record_state(key_b, {"quota": 5}) == {}
    # 各自的基线互不影响
    assert visit._record_state(key_a, {"quota": 110})["quota"] == 100
    assert visit._record_state(key_b, {"quota": 6})["quota"] == 5

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted[key_a]["quota"] == 110
    assert persisted[key_b]["quota"] == 6


def test_legacy_site_key_is_read_once_then_superseded(tmp_path: Path, monkeypatch) -> None:
    """升级后第一次运行仍要读到历史基线，但只写新 key。"""
    state_path = _isolate_state(tmp_path, monkeypatch)
    legacy_key = visit._legacy_state_key("https://shared.invalid")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({legacy_key: {"quota": 42}}), encoding="utf-8")

    new_key = visit._state_key("https://shared.invalid", "1001")
    previous = visit._record_state(new_key, {"quota": 50}, legacy_key=legacy_key)
    assert previous["quota"] == 42, "首次升级必须能读到旧基线，否则会误报首次记录"

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted[new_key]["quota"] == 50
    assert persisted[legacy_key]["quota"] == 42, "旧记录只读不改，随过期自然淘汰"

    # 第二次起走自己的新 key，不再回落
    assert visit._record_state(new_key, {"quota": 55}, legacy_key=legacy_key)["quota"] == 50


def test_record_state_uses_timezone_aware_timestamps(tmp_path: Path, monkeypatch) -> None:
    """裸 datetime.now() 是无时区本地时间，CI 与本地无法正确比较先后。"""
    import time_utils

    state_path = _isolate_state(tmp_path, monkeypatch)
    visit._record_state(
        "k",
        {"quota": 1, "updated_at": time_utils.utc_iso(), "date": time_utils.business_date()},
    )
    entry = json.loads(state_path.read_text(encoding="utf-8"))["k"]
    assert entry["updated_at"].endswith("Z")
    assert time_utils.parse_timestamp(entry["updated_at"]) is not None
