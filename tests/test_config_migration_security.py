# -*- coding: utf-8 -*-
"""配置迁移的敏感数据清理与布尔字段持久化。

回归背景：
1. 旧格式允许把账号直接挂在顶层（{"站点名": {...}}）。_document_metadata 过去把
   所有非 accounts/oauth_states 的顶层键都当元数据透传，于是自动迁移后文件里
   同时存在新的 accounts 数组与旧账号键——旧 token/cookie 变成运行时不再读取、
   用户也删不掉的隐藏副本。
2. auto_refresh_cookie=False 走字符串型可选字段分支被折成空串丢弃，下次加载
   恢复默认 True，用户明确关闭的行为被一次保存悄悄打开。

所有用例只使用虚构凭据，且全部在 tmp_path 内操作。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import accounts_store

FAKE_LEGACY_TOKEN = "FAKE_LEGACY_TOKEN"
FAKE_LEGACY_COOKIE = "session=FAKE_LEGACY_COOKIE"


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _legacy_top_level_doc() -> dict:
    """旧格式：账号直接挂顶层，且带旧 type/checkin_mode 触发迁移。"""
    return {
        "旧站点": {
            "base_url": "https://legacy.invalid",
            "type": "newapi",
            "checkin_mode": "legacy",
            "access_token": FAKE_LEGACY_TOKEN,
            "cookie": FAKE_LEGACY_COOKIE,
            "user_id": "1001",
        }
    }


def _load(path: Path) -> list[dict]:
    return accounts_store.load_unified_accounts(path=path, sites_path=path.parent / "missing.json")


# ── R2：迁移不得残留旧账号键 ────────────────────────────────────────────────
def test_legacy_top_level_accounts_are_not_kept_as_metadata(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(path, _legacy_top_level_doc())

    _load(path)
    doc = _read(path)

    assert "accounts" in doc
    assert "旧站点" not in doc, "旧顶层账号键必须随迁移丢弃，否则凭据留下隐藏副本"
    assert doc["schema_version"] == accounts_store.SCHEMA_VERSION


def test_migration_removes_stale_credential_copies(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(path, _legacy_top_level_doc())

    _load(path)
    raw_text = path.read_text(encoding="utf-8")

    # 凭据只能出现在 accounts 数组里，不能同时存在第二份副本。
    assert raw_text.count(FAKE_LEGACY_TOKEN) == 1
    assert raw_text.count("FAKE_LEGACY_COOKIE") == 1


def test_migration_creates_restricted_backup(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(path, _legacy_top_level_doc())

    _load(path)

    backups = sorted(tmp_path.glob("ACCOUNTS.json.bak.*"))
    assert len(backups) == 1, "迁移会重写整份配置，必须留一份备份"
    assert FAKE_LEGACY_TOKEN in backups[0].read_text(encoding="utf-8")
    if os.name != "nt":
        assert backups[0].stat().st_mode & 0o777 == 0o600


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(path, _legacy_top_level_doc())

    _load(path)
    first = _read(path)
    _load(path)
    second = _read(path)

    assert first == second
    # 第二次不再判定为旧格式，因此不会产生第二个备份。
    assert len(sorted(tmp_path.glob("ACCOUNTS.json.bak.*"))) == 1


def test_real_metadata_is_preserved_for_new_format(tmp_path: Path) -> None:
    """含 accounts 键的文档里，未知顶层字段仍属于元数据，必须透传。"""
    path = tmp_path / "ACCOUNTS.json"
    _write(
        path,
        {
            "_note": "keep-me",
            "accounts": [
                {
                    "name": "站点",
                    "base_url": "https://site.invalid",
                    "type": "newapi",
                    "checkin_mode": "legacy",
                    "access_token": FAKE_LEGACY_TOKEN,
                }
            ],
        },
    )

    _load(path)
    doc = _read(path)

    assert doc["_note"] == "keep-me"
    assert [entry["name"] for entry in doc["accounts"]] == ["站点"]


def test_migration_keeps_account_order_and_oauth_states(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(
        path,
        {
            "accounts": [
                {"name": "第一个", "base_url": "https://a.invalid", "type": "newapi", "checkin_mode": "legacy"},
                {"name": "第二个", "base_url": "https://b.invalid", "type": "sub2api", "checkin_mode": "browser"},
                {"name": "同址其二", "base_url": "https://a.invalid", "type": "newapi", "checkin_mode": "legacy"},
            ],
            "oauth_states": {"linuxdo": {"state": "FAKE_SHARED_STATE"}},
        },
    )

    _load(path)
    doc = _read(path)

    assert [entry["name"] for entry in doc["accounts"]] == ["第一个", "第二个", "同址其二"]
    accounts = doc["oauth_states"]["linuxdo"]["accounts"]
    assert accounts["default"]["state"] == "FAKE_SHARED_STATE"


def test_save_accounts_writes_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [{"name": "站点", "base_url": "https://site.invalid"}], path=path, oauth_states={}
    )
    assert _read(path)["schema_version"] == accounts_store.SCHEMA_VERSION


# ── R3：布尔可选字段必须落盘 ────────────────────────────────────────────────
def test_disabled_auto_refresh_cookie_survives_save(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [
            {
                "name": "站点",
                "base_url": "https://site.invalid",
                "site_profile": "newapi",
                "auth_method": "cookie",
                "checkin_action": "api",
                "auto_refresh_cookie": False,
            }
        ],
        path=path,
        oauth_states={},
    )

    assert _read(path)["accounts"][0]["auto_refresh_cookie"] is False
    site = accounts_store.configured_site_from_mapping(_load(path)[0])
    assert site.auto_refresh_cookie is False


def test_default_booleans_are_not_written(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [{"name": "站点", "base_url": "https://site.invalid", "auto_refresh_cookie": True}],
        path=path,
        oauth_states={},
    )

    entry = _read(path)["accounts"][0]
    assert "auto_refresh_cookie" not in entry
    assert "verify_ssl" not in entry


def test_disabled_verify_ssl_still_survives_save(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [{"name": "站点", "base_url": "https://site.invalid", "verify_ssl": False}],
        path=path,
        oauth_states={},
    )
    assert _read(path)["accounts"][0]["verify_ssl"] is False
