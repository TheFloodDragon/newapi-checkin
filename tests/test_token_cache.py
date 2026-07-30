# -*- coding: utf-8 -*-
"""运行期 token 缓存：新 token 不得写回 ACCOUNTS.json。

背景：access_token 是短期 JWT（实测 sub2api 数小时即过期），每次续期都改写
ACCOUNTS.json 会让配置被后台任务反复重写，也让导出的 GitHub Secret 很快失效。
因此续期结果落在独立的 <缓存目录>/token_cache.json，配置只保留长期凭据。
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
    """配置种子未变时，带 basis 的运行缓存优先采用。"""
    _cache_to(tmp_path, monkeypatch)
    basis = token_cache.credential_basis("STALE", "", group="token")
    token_cache.save_tokens(
        "s", "https://s.invalid", "FRESH", "FRESH-RT", token_basis=basis
    )

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
def test_reconcile_marks_cache_conflicting_with_manual_token(tmp_path: Path, monkeypatch) -> None:
    """用户手填新 token 时，旧缓存不得再被应用；但值只标记不删除。

    删除不可逆：用户改错又改回来、或只是改名，本来仍有效的运行期凭据就永久丢了。
    """
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "STALE-AT", "STALE-RT")

    assert token_cache.reconcile_with_config("s", "https://s.invalid", "a.b.c", "rt_new") is True
    assert token_cache.load_tokens("s", "https://s.invalid")["access_token"] == "STALE-AT"
    assert token_cache.resolve_cached_credentials("s", "https://s.invalid") == {}

    # 标记后，site_config 必须采用配置里手填的值
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
    """配置 state 种子未变时，带 basis 的新登录态优先采用。"""
    _cache_to(tmp_path, monkeypatch)
    basis = token_cache.credential_basis(browser_state="STALE-STATE", group="state")
    token_cache.save_browser_state(
        "s", "https://s.invalid", "FRESH-STATE", state_basis=basis
    )
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


# ── 来源感知缓存：新 Secret / 显式任务输入必须赢过旧缓存 ────────────────────
def test_configured_factory_never_applies_runtime_cache(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "CACHED")

    site = accounts_store.configured_site_from_mapping(
        {"name": "s", "base_url": "https://s.invalid", "access_token": "CONFIG"}
    )

    assert site.access_token == "CONFIG"


def test_basis_mismatch_ignores_cache_after_secret_change(tmp_path: Path, monkeypatch) -> None:
    """缓存由旧 Secret 生成时，新 Secret 的配置值必须生效。"""
    _cache_to(tmp_path, monkeypatch)
    old_basis = token_cache.credential_basis("OLD-SEED", "OLD-RT", group="token")
    token_cache.save_tokens(
        "s", "https://s.invalid", "OLD-CACHED", "OLD-CACHED-RT", token_basis=old_basis
    )

    site = accounts_store.runtime_site_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "access_token": "NEW-SECRET",
            "refresh_token": "NEW-SECRET-RT",
        }
    )

    assert site.access_token == "NEW-SECRET"
    assert site.refresh_token == "NEW-SECRET-RT"


def test_legacy_cache_only_fills_empty_config(tmp_path: Path, monkeypatch) -> None:
    """旧无 basis 缓存仅在配置为空时兼容兜底。"""
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens("s", "https://s.invalid", "LEGACY-AT", "LEGACY-RT")

    empty = accounts_store.runtime_site_from_mapping(
        {"name": "s", "base_url": "https://s.invalid"}
    )
    configured = accounts_store.runtime_site_from_mapping(
        {"name": "s", "base_url": "https://s.invalid", "access_token": "CONFIG-AT"}
    )

    assert empty.access_token == "LEGACY-AT"
    assert empty.refresh_token == "LEGACY-RT"
    assert configured.access_token == "CONFIG-AT"
    # refresh 配置为空，因此旧缓存仍可只补这个字段。
    assert configured.refresh_token == "LEGACY-RT"


def test_legacy_cache_marked_as_changed_is_not_used_but_kept(tmp_path: Path, monkeypatch) -> None:
    """无 basis 的旧条目被标记「配置已变」后不再兜底，但值必须留在文件里。

    背景：以前配置一变就直接删缓存条目。删除不可逆——用户改错又改回来、或只是
    改名，本来仍有效的运行期 token 与登录态就永久丢了。现在改成「只标记不删除」。
    """
    cache = tmp_path / "token_cache.json"
    token_cache.save_tokens("s", "https://s.invalid", "LEGACY-AT", path=cache)
    assert token_cache.resolve_cached_credentials(
        "s", "https://s.invalid", path=cache
    )["access_token"] == "LEGACY-AT"

    assert token_cache.mark_credentials_changed("s", "https://s.invalid", {"access_token"}, cache)
    assert token_cache.resolve_cached_credentials("s", "https://s.invalid", path=cache) == {}
    assert token_cache.load_tokens("s", "https://s.invalid", path=cache)["access_token"] == "LEGACY-AT", (
        "值必须保留：只是不再使用，而不是删除"
    )

    # 再次写入（例如浏览器重登后续存）即恢复可用
    token_cache.save_tokens("s", "https://s.invalid", "LEGACY-AT", path=cache)
    assert token_cache.resolve_cached_credentials(
        "s", "https://s.invalid", path=cache
    )["access_token"] == "LEGACY-AT"


def test_mark_changed_only_affects_requested_group(tmp_path: Path, monkeypatch) -> None:
    cache = tmp_path / "token_cache.json"
    token_cache.save_tokens("s", "https://s.invalid", "AT", browser_state="ST", path=cache)
    token_cache.mark_credentials_changed("s", "https://s.invalid", {"access_token"}, cache)

    resolved = token_cache.resolve_cached_credentials("s", "https://s.invalid", path=cache)
    assert "access_token" not in resolved
    assert resolved["browser_state"] == "ST", "只改 token 不该牵连登录态"


def test_basis_entries_ignore_the_changed_mark(tmp_path: Path, monkeypatch) -> None:
    """有 basis 的条目只看 basis：否则每次保存配置都会白丢一次运行期凭据。"""
    cache = tmp_path / "token_cache.json"
    token_cache.save_tokens(
        "s", "https://s.invalid", "CACHED",
        token_basis=token_cache.credential_basis("CFG", "", group="token"),
        path=cache,
    )
    token_cache.mark_credentials_changed("s", "https://s.invalid", {"access_token"}, cache)

    resolved = token_cache.resolve_cached_credentials(
        "s", "https://s.invalid", configured_access_token="CFG", path=cache
    )
    assert resolved["access_token"] == "CACHED"


def test_explicit_fields_win_over_compatible_cache_including_empty(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    token_basis = token_cache.credential_basis("CONFIG", "RT", group="token")
    state_basis = token_cache.credential_basis(browser_state="", group="state")
    token_cache.save_tokens(
        "s",
        "https://s.invalid",
        "CACHED",
        "CACHED-RT",
        browser_state="CACHED-STATE",
        token_basis=token_basis,
        state_basis=state_basis,
    )

    site = accounts_store.runtime_site_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "access_token": "CONFIG",
            "refresh_token": "RT",
            "browser_state": "",
        },
        explicit_fields={"access_token", "browser_state"},
    )

    assert site.access_token == "CONFIG"
    assert site.refresh_token == "CACHED-RT"
    assert site.browser_state == ""


def test_cache_policy_ignore_disables_all_cache_overlay(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens(
        "s", "https://s.invalid", "CACHED", "RT", browser_state="STATE"
    )

    site = accounts_store.runtime_site_from_mapping(
        {"name": "s", "base_url": "https://s.invalid"}, cache_policy="ignore"
    )

    assert site.access_token == ""
    assert site.refresh_token == ""
    assert site.browser_state == ""


def test_invalidate_token_fields_preserves_browser_state(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens(
        "s", "https://s.invalid", "AT", "RT", browser_state="STATE",
        token_basis="token-basis", state_basis="state-basis",
    )

    assert token_cache.invalidate_fields(
        "s", "https://s.invalid", {"access_token", "refresh_token"}
    )
    entry = token_cache.load_cache_entry("s", "https://s.invalid")
    assert "access_token" not in entry and "refresh_token" not in entry
    assert "token_basis" not in entry
    assert entry["browser_state"] == "STATE"
    assert entry["state_basis"] == "state-basis"


def test_invalidate_state_preserves_tokens(tmp_path: Path, monkeypatch) -> None:
    _cache_to(tmp_path, monkeypatch)
    token_cache.save_tokens(
        "s", "https://s.invalid", "AT", "RT", browser_state="STATE",
        token_basis="token-basis", state_basis="state-basis",
    )

    assert token_cache.invalidate_fields("s", "https://s.invalid", {"browser_state"})
    entry = token_cache.load_cache_entry("s", "https://s.invalid")
    assert entry["access_token"] == "AT" and entry["refresh_token"] == "RT"
    assert entry["token_basis"] == "token-basis"
    assert "browser_state" not in entry and "state_basis" not in entry


def test_save_site_tokens_records_basis_and_v2_document(tmp_path: Path, monkeypatch) -> None:
    cache = _cache_to(tmp_path, monkeypatch)
    site = accounts_store.configured_site_from_mapping(
        {
            "name": "s",
            "base_url": "https://s.invalid",
            "access_token": "SEED",
            "refresh_token": "SEED-RT",
            "browser_state": "SEED-STATE",
        }
    )

    assert token_cache.save_site_tokens(site, "NEW", "NEW-RT", browser_state="NEW-STATE")
    payload = json.loads(cache.read_text(encoding="utf-8"))
    entry = next(iter(payload["tokens"].values()))
    assert payload["version"] == 2
    assert entry["token_basis"] == site.runtime_credentials.token_basis
    assert entry["state_basis"] == site.runtime_credentials.state_basis
    assert entry["updated_at"].endswith("Z")
