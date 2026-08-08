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


def test_api_variant_defaults_to_legacy() -> None:
    """未配置时默认 legacy：实测在用站点的 challenge 端点与 WASM 均 404，
    challenge 优先只会白启一个 Node 子进程再回落。"""
    site = accounts_store.site_config_from_mapping(
        {
            "name": "d",
            "base_url": "https://d.invalid",
            "site_profile": "newapi",
            "checkin_action": "api",
        }
    )
    assert site.api_variant == "legacy"
    assert accounts_store.normalize_api_variant("") == "legacy"
    assert accounts_store.normalize_api_variant("nonsense") == "legacy"
    # auto 仍是合法选项，显式选择必须被尊重。
    assert accounts_store.normalize_api_variant("auto") == "auto"


def test_legacy_challenge_mode_migrates_to_auto() -> None:
    """旧配置显式写过 checkin_mode=challenge 时保留 challenge 优先，不被新默认覆盖。"""
    challenge = accounts_store.migrate_fields(
        {"type": "newapi", "checkin_mode": "challenge", "access_token": "t"}
    )
    assert challenge["api_variant"] == "auto"
    # 未指定 checkin_mode 的旧账号跟随新默认。
    blank = accounts_store.migrate_fields({"type": "newapi", "access_token": "t"})
    assert blank["api_variant"] == "legacy"


def test_explicit_auto_variant_survives_save(tmp_path: Path) -> None:
    """默认值不落盘，但显式选的 auto 必须落盘，否则保存一次就丢掉 challenge 偏好。"""
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [
            {
                "name": "default",
                "base_url": "https://a.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "api_variant": "legacy",
            },
            {
                "name": "challenge-first",
                "base_url": "https://b.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "api_variant": "auto",
            },
        ],
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert "api_variant" not in saved[0], "默认 legacy 不写盘"
    assert saved[1]["api_variant"] == "auto"


def test_save_accounts_preserves_api_script_without_browser_only_fields(tmp_path: Path) -> None:
    """api 脚本保存时只保留路径；参数与超时属于 browser_script，不应混入。"""
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [
            {
                "name": "captcha",
                "base_url": "https://captcha.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "script": "scripts/custom_captcha.py",
                "script_args": {"ignored": True},
                "script_timeout": 99,
                "enabled": True,
            }
        ],
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"][0]
    assert saved["script"] == "scripts/custom_captcha.py"
    assert "script_args" not in saved
    assert "script_timeout" not in saved


# ── GitHub Secret 导出：接口凭据 vs 登录方式凭据 ──────────────────────────────
def _sub2api_browser_account(**extra) -> dict:
    """sub2api + auth_method=browser 的典型账号（本仓库 6 个 sub2api 站点都是这样配的）。"""
    account = {
        "name": "s",
        "base_url": "https://s.invalid",
        "site_profile": "sub2api",
        "auth_method": "browser",
        "checkin_action": "browser_script",
        "enabled": True,
    }
    account.update(extra)
    return account


def test_export_keeps_api_script_without_browser_only_fields() -> None:
    payload = accounts_store.build_github_secret_payload(
        [
            {
                "name": "captcha",
                "base_url": "https://captcha.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "script": "scripts/custom_captcha.py",
                "script_args": {"ignored": True},
                "script_timeout": 99,
                "access_token": "at",
                "user_id": "42",
                "enabled": True,
            }
        ]
    )
    exported = payload["accounts"][0]
    assert exported["script"] == "scripts/custom_captcha.py"
    assert "script_args" not in exported
    assert "script_timeout" not in exported


def test_export_keeps_access_token_regardless_of_auth_method() -> None:
    """access_token 是**接口凭据**，与 auth_method 无关，必须导出。

    回归：旧实现只在 auth_method == "access_token" 时导出，于是 sub2api +
    auth_method=browser 的站点导出的 Secret 里没有 access_token，CI 里第 1 级
    纯 API 直接被跳过（日志显示「未配置 access_token」），只能靠 refresh_token
    续期；refresh_token 也失效时就得在 CI 里拉浏览器，而 Turnstile 大概率过不去。
    """
    payload = accounts_store.build_github_secret_payload(
        [_sub2api_browser_account(access_token="at-value", refresh_token="rt-value")]
    )
    exported = payload["accounts"][0]
    assert exported["access_token"] == "at-value"
    # refresh_token 本来就不看 auth_method，两者必须一致
    assert exported["refresh_token"] == "rt-value"


def test_export_omits_cookie_when_auth_method_ignores_it() -> None:
    """cookie 只在 cookie 登录方式下会被读取，browser/oauth 场景不该导出。

    与 access_token 的差异是有意的：_common.build_http_client 对 browser/oauth
    传的是空 AuthInfo()，cookie 根本不会被使用；而 sub2api 的 api_first 链路会
    直接读 site.access_token。少传一份用不到的敏感数据。
    """
    payload = accounts_store.build_github_secret_payload(
        [_sub2api_browser_account(cookie="session=secret", access_token="at")]
    )
    exported = payload["accounts"][0]
    assert "cookie" not in exported
    assert exported["access_token"] == "at"


def test_export_keeps_cookie_for_cookie_auth() -> None:
    payload = accounts_store.build_github_secret_payload(
        [
            {
                "name": "c",
                "base_url": "https://c.invalid",
                "site_profile": "newapi",
                "auth_method": "cookie",
                "cookie": "session=secret",
                "enabled": True,
            }
        ]
    )
    assert payload["accounts"][0]["cookie"] == "session=secret"


def test_export_skips_disabled_accounts() -> None:
    payload = accounts_store.build_github_secret_payload(
        [_sub2api_browser_account(access_token="at", enabled=False)]
    )
    assert payload["accounts"] == []


def test_export_keeps_non_default_runtime_options() -> None:
    payload = accounts_store.build_github_secret_payload(
        [
            _sub2api_browser_account(
                access_token="at",
                verify_ssl=False,
                referer_path="/console",
                auto_refresh_cookie=False,
            )
        ]
    )
    exported = payload["accounts"][0]
    assert exported["verify_ssl"] is False
    assert exported["referer_path"] == "/console"
    assert exported["auto_refresh_cookie"] is False

    restored = accounts_store.configured_site_from_mapping(exported)
    assert restored.verify_ssl is False
    assert restored.referer_path == "/console"
    assert restored.auto_refresh_cookie is False


def test_export_omits_default_runtime_options() -> None:
    payload = accounts_store.build_github_secret_payload(
        [
            _sub2api_browser_account(
                access_token="at",
                verify_ssl=True,
                referer_path="/profile",
                auto_refresh_cookie=True,
            )
        ]
    )
    exported = payload["accounts"][0]
    assert "verify_ssl" not in exported
    assert "referer_path" not in exported
    assert "auto_refresh_cookie" not in exported


def test_verification_mode_persists_only_when_non_default(tmp_path: Path) -> None:
    path = tmp_path / "ACCOUNTS.json"
    accounts_store.save_accounts(
        [
            {
                "name": "auto",
                "base_url": "https://auto.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "verification_mode": "auto",
            },
            {
                "name": "shape",
                "base_url": "https://shape.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "verification_mode": "click_shape",
            },
        ],
        path=path,
    )
    saved = json.loads(path.read_text(encoding="utf-8"))["accounts"]
    assert "verification_mode" not in saved[0]
    assert saved[1]["verification_mode"] == "click_shape"


def test_builtin_verification_scripts_migrate_to_mode() -> None:
    turnstile = accounts_store.migrate_fields(
        {
            "site_profile": "newapi",
            "auth_method": "access_token",
            "checkin_action": "api",
            "script": "scripts/newapi_turnstile.py",
        }
    )
    assert turnstile["verification_mode"] == "turnstile"
    assert "script" not in turnstile

    captcha = accounts_store.migrate_fields(
        {
            "site_profile": "newapi",
            "auth_method": "access_token",
            "checkin_action": "api",
            "script": "scripts/newapi_captcha.py",
        }
    )
    assert captcha["verification_mode"] == "auto"
    assert "script" not in captcha


def test_custom_api_script_is_not_migrated() -> None:
    row = accounts_store.migrate_fields(
        {
            "site_profile": "newapi",
            "auth_method": "access_token",
            "checkin_action": "api",
            "script": "scripts/custom_captcha.py",
            "verification_mode": "string_captcha",
        }
    )
    assert row["script"] == "scripts/custom_captcha.py"
    assert row["verification_mode"] == "string_captcha"
