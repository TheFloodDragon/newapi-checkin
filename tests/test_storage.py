from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import accounts_store


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_save_accounts_preserves_unknown_metadata_and_fields(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(
        path,
        {
            "schema_note": {"owner": "user"},
            "accounts": [
                {
                    "name": "one",
                    "base_url": "https://one.invalid",
                    "site_profile": "newapi",
                    "auth_method": "cookie",
                    "checkin_action": "api",
                    "cookie": "session=secret",
                    "custom_field": {"keep": True},
                }
            ],
        },
    )
    accounts_store.save_accounts(accounts_store._account_entries(path), path=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["schema_note"] == {"owner": "user"}
    assert saved["accounts"][0]["custom_field"] == {"keep": True}


def test_corrupt_accounts_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(accounts_store.ConfigError):
        accounts_store.load_unified_accounts(path=path, sites_path=tmp_path / "missing.json")

    path.write_text("123", encoding="utf-8")
    with pytest.raises(accounts_store.ConfigError, match="顶层"):
        accounts_store.load_unified_accounts(path=path, sites_path=tmp_path / "missing.json")


def test_ambiguous_identity_update_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    duplicate = {
        "name": "same",
        "base_url": "https://same.invalid",
        "site_profile": "newapi",
        "auth_method": "access_token",
        "checkin_action": "api",
    }
    _write(path, {"accounts": [duplicate, duplicate]})
    with pytest.raises(accounts_store.ConfigError, match="不唯一"):
        accounts_store.update_account_access_token("same", "https://same.invalid", "new", path=path)


def test_concurrent_updates_do_not_lose_data(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    _write(
        path,
        {
            "accounts": [
                {
                    "name": "one",
                    "base_url": "https://one.invalid",
                    "site_profile": "sub2api",
                    "auth_method": "access_token",
                    "checkin_action": "api",
                },
                {
                    "name": "two",
                    "base_url": "https://two.invalid",
                    "site_profile": "sub2api",
                    "auth_method": "access_token",
                    "checkin_action": "api",
                },
            ]
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda args: accounts_store.update_account_access_token(*args, path=path),
                [
                    ("one", "https://one.invalid", "token-one"),
                    ("two", "https://two.invalid", "token-two"),
                ],
            )
        )
    assert results == [True, True]
    entries = {entry["name"]: entry for entry in accounts_store._account_entries(path)}
    assert entries["one"]["access_token"] == "token-one"
    assert entries["two"]["access_token"] == "token-two"


def test_site_config_factory_normalizes_legacy_fields() -> None:
    site = accounts_store.site_config_from_mapping(
        {
            "name": "legacy",
            "base_url": "legacy.invalid/",
            "type": "newapi",
            "checkin_mode": "legacy",
            "enabled": "yes",
            "auto_refresh_cookie": "false",
        }
    )
    assert site.base_url == "https://legacy.invalid"
    assert site.site_profile == "newapi"
    assert site.checkin_action == "api"
    assert site.api_variant == "legacy"
    assert site.enabled is True
    assert site.auto_refresh_cookie is False
