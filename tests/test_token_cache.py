# -*- coding: utf-8 -*-
"""运行期 token 缓存：新 token 不得写回 ACCOUNTS.json。

背景：access_token 是短期 JWT（实测 sub2api 数小时即过期），每次续期都改写
ACCOUNTS.json 会让配置被后台任务反复重写，也让导出的 GitHub Secret 很快失效。
因此续期结果落在独立的 results/token_cache.json，配置只保留长期凭据。
"""

from __future__ import annotations

import json
from pathlib import Path

import accounts_store
from providers import token_cache


def _cache_to(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", path)
    return path


def test_tokens_persist_to_cache_not_accounts(tmp_path: Path, monkeypatch) -> None:
    cache = _cache_to(tmp_path, monkeypatch)
    accounts = tmp_path / "ACCOUNTS.json"
    accounts.write_text(
        json.dumps(
            {
                "accounts": [
                    {
                        "name": "s",
                        "base_url": "https://s.invalid",
                        "site_profile": "sub2api",
                        "auth_method": "browser",
                        "checkin_action": "browser_script",
                        "access_token": "OLD-TOKEN",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert token_cache.save_tokens("s", "https://s.invalid", "NEW-ACCESS", "NEW-REFRESH")

    # 缓存里是新值
    entry = token_cache.load_tokens("s", "https://s.invalid")
    assert entry["access_token"] == "NEW-ACCESS"
    assert entry["refresh_token"] == "NEW-REFRESH"
    assert cache.exists()

    # ACCOUNTS.json 未被改动
    raw = json.loads(accounts.read_text(encoding="utf-8"))
    assert raw["accounts"][0]["access_token"] == "OLD-TOKEN"
    assert "refresh_token" not in raw["accounts"][0]


def test_cache_overrides_config_token(tmp_path: Path, monkeypatch) -> None:
    """site_config_from_mapping 必须优先采用缓存里的新 token。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "FRESH", "FRESH-RT")

    site = accounts_store.site_config_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "site_profile": "sub2api",
            "access_token": "STALE",
        }
    )
    assert site.access_token == "FRESH"
    assert site.refresh_token == "FRESH-RT"


def test_config_token_used_when_cache_empty(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    site = accounts_store.site_config_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "site_profile": "sub2api",
            "access_token": "FROM-CONFIG",
        }
    )
    assert site.access_token == "FROM-CONFIG"


def test_cache_keyed_by_name_and_url(tmp_path: Path, monkeypatch) -> None:
    """同一 base_url 下的多账号必须各自独立，不能互相覆盖。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("a", "https://same.invalid", "TOKEN-A")
    token_cache.save_tokens("b", "https://same.invalid", "TOKEN-B")

    assert token_cache.load_tokens("a", "https://same.invalid")["access_token"] == "TOKEN-A"
    assert token_cache.load_tokens("b", "https://same.invalid")["access_token"] == "TOKEN-B"


def test_corrupt_cache_is_ignored(tmp_path: Path, monkeypatch) -> None:
    """缓存损坏不能影响签到：它只是加速用的运行期产物。"""
    cache = _cache_to(tmp_path, monkeypatch)
    cache.write_text("{broken", encoding="utf-8")
    assert token_cache.load_tokens("s", "https://s.invalid") == {}
    # 仍可正常写入（覆盖损坏内容）
    assert token_cache.save_tokens("s", "https://s.invalid", "NEW")
    assert token_cache.load_tokens("s", "https://s.invalid")["access_token"] == "NEW"


def test_empty_tokens_are_not_saved(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    assert token_cache.save_tokens("s", "https://s.invalid", "", "") is False
    assert token_cache.load_tokens("s", "https://s.invalid") == {}


# ── 手填凭据必须赢过旧缓存（「填了有效 token 仍显示没有」的根因）─────────────
def test_reconcile_clears_cache_conflicting_with_manual_token(tmp_path: Path, monkeypatch) -> None:
    """用户手填新 token 时，旧缓存条目必须被清掉，否则读取时缓存优先会盖掉新值。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "STALE-AT", "STALE-RT")

    assert token_cache.reconcile_with_config("s", "https://s.invalid", "a.b.c", "rt_new") is True
    assert token_cache.load_tokens("s", "https://s.invalid") == {}

    # 清掉缓存后，site_config 必须采用配置里手填的值
    site = accounts_store.site_config_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "site_profile": "sub2api",
            "access_token": "a.b.c",
            "refresh_token": "rt_new",
        }
    )
    assert site.access_token == "a.b.c"
    assert site.refresh_token == "rt_new"


def test_reconcile_keeps_cache_when_values_match(tmp_path: Path, monkeypatch) -> None:
    """配置与缓存一致时不得清缓存：那会白丢一次续期结果。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "a.b.c", "rt_same")
    assert token_cache.reconcile_with_config("s", "https://s.invalid", "a.b.c", "rt_same") is False
    assert token_cache.load_tokens("s", "https://s.invalid")["access_token"] == "a.b.c"


def test_reconcile_noop_without_manual_values(tmp_path: Path, monkeypatch) -> None:
    """配置里没填凭据时，缓存是唯一来源，绝不能清。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "FRESH", "rt_fresh")
    assert token_cache.reconcile_with_config("s", "https://s.invalid", "", "") is False
    assert token_cache.load_tokens("s", "https://s.invalid")["access_token"] == "FRESH"


def test_browser_state_goes_to_cache_not_accounts(tmp_path: Path, monkeypatch) -> None:
    """browser_state 是运行期产物：每次开站点都变，不得回写用户配置。"""
    cache = _cache_to(tmp_path, monkeypatch)
    assert token_cache.save_browser_state("s", "https://s.invalid", "STATE-B64")
    assert token_cache.load_tokens("s", "https://s.invalid")["browser_state"] == "STATE-B64"
    assert cache.exists()


def test_save_browser_state_keeps_existing_tokens(tmp_path: Path, monkeypatch) -> None:
    """只刷新登录态时不能把已有 token 抹掉。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "AT", "RT")
    token_cache.save_browser_state("s", "https://s.invalid", "STATE")
    got = token_cache.load_tokens("s", "https://s.invalid")
    assert got == {"access_token": "AT", "refresh_token": "RT", "browser_state": "STATE"}


def test_cached_browser_state_overrides_config(tmp_path: Path, monkeypatch) -> None:
    """站点运行时应优先使用缓存里较新的登录态。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_browser_state("s", "https://s.invalid", "FRESH-STATE")
    site = accounts_store.site_config_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "site_profile": "sub2api",
            "auth_method": "browser",
            "browser_state": "STALE-STATE",
        }
    )
    assert site.browser_state == "FRESH-STATE"


def test_config_browser_state_used_when_cache_empty(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    site = accounts_store.site_config_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "site_profile": "sub2api",
            "auth_method": "browser",
            "browser_state": "FROM-CONFIG",
        }
    )
    assert site.browser_state == "FROM-CONFIG"


def test_same_url_channels_have_separate_cache_entries(tmp_path: Path, monkeypatch) -> None:
    """同址多渠道的登录态必须各自独立，不能互相同步。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_browser_state("ch1", "https://same.invalid", "STATE-1")
    token_cache.save_browser_state("ch2", "https://same.invalid", "STATE-2")
    assert token_cache.load_tokens("ch1", "https://same.invalid")["browser_state"] == "STATE-1"
    assert token_cache.load_tokens("ch2", "https://same.invalid")["browser_state"] == "STATE-2"
