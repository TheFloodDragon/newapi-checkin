# -*- coding: utf-8 -*-
"""gui.core 纯逻辑层测试（不依赖 PySide6）。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import time_utils
from gui import config_store, core


def _mk_states(provider: str = "linuxdo", account: str = "default", state: str = "abc123") -> dict:
    return {provider: {"accounts": {account: {"state": state, "username": "u"}}}}


# ── effective_auth / can_optional_oauth ──────────────────────────────────────
def test_effective_auth_relogin_forces_oauth() -> None:
    assert core.effective_auth("relogin", "cookie") == "oauth"
    assert core.effective_auth("relogin", "browser") == "oauth"


def test_effective_auth_browser_script_allows_browser_and_oauth() -> None:
    assert core.effective_auth("browser_script", "browser") == "browser"
    assert core.effective_auth("browser_script", "oauth") == "oauth"
    assert core.effective_auth("browser_script", "cookie") == "oauth"
    assert core.effective_auth("api", "cookie") == "cookie"


def test_can_optional_oauth_matrix() -> None:
    assert core.can_optional_oauth("sub2api", "api", "access_token")
    assert core.can_optional_oauth("newapi", "browser_script", "browser")
    assert not core.can_optional_oauth("newapi", "api", "access_token")
    assert not core.can_optional_oauth("sub2api", "api", "cookie")


# ── row_from_store ───────────────────────────────────────────────────────────
def test_row_from_store_coerces_and_blanks_state() -> None:
    row = core.row_from_store(
        {
            "name": "s",
            "base_url": "https://Example.com/",
            "checkin_action": "relogin",
            "auth_method": "cookie",
            "browser_state": "SHOULD-DROP",
        }
    )
    assert row.auth_method == "oauth"
    assert row.browser_state == ""
    assert row.base_url == "https://Example.com"  # normalize 去尾斜杠，不改大小写


def test_row_from_store_infers_auth_from_token() -> None:
    row = core.row_from_store({"name": "s", "base_url": "https://a.com", "access_token": "tok", "auth_method": "??"})
    assert row.auth_method == "access_token"
    row2 = core.row_from_store({"name": "s", "base_url": "https://a.com", "auth_method": "??"})
    assert row2.auth_method == "cookie"


def test_row_from_store_verification_mode() -> None:
    row = core.row_from_store(
        {
            "name": "s",
            "base_url": "https://a.com",
            "verification_mode": "click-shape",
        }
    )
    assert row.verification_mode == "click_shape"


def test_row_from_store_script_args_text() -> None:
    row = core.row_from_store(
        {"name": "s", "base_url": "https://a.com", "script_args": {"k": "v"}, "checkin_action": "browser_script",
         "auth_method": "browser"}
    )
    assert row.script_args == {"k": "v"}
    assert json.loads(row.script_args_text) == {"k": "v"}
    empty = core.row_from_store({"name": "s", "base_url": "https://a.com"})
    assert empty.script_args_text == "{}"


# ── task_params ──────────────────────────────────────────────────────────────
def test_task_params_oauth_pulls_shared_state() -> None:
    row = core.SiteRow(name="s", base_url="https://a.com", checkin_action="relogin", auth_method="cookie")
    params = core.task_params(row, _mk_states(state="STATE-X"))
    assert params["auth_method"] == "oauth"
    assert params["browser_state"] == "STATE-X"
    assert params["fallback_uid"] == params["user_id"] == ""
    assert params["verify_ssl"] is True
    assert params["verification_mode"] == "auto"


def test_task_params_browser_keeps_row_state_and_fallback_gating() -> None:
    row = core.SiteRow(
        name="s",
        base_url="https://a.com",
        type="sub2api",
        auth_method="access_token",
        checkin_action="api",
        browser_state="row-state",
        oauth_fallback_provider="linuxdo",
        oauth_fallback_account="default",
        verify_ssl=False,
    )
    params = core.task_params(row, {})
    assert params["browser_state"] == "row-state"
    assert params["oauth_fallback_provider"] == "linuxdo"
    assert params["verify_ssl"] is False
    # 不满足可选 OAuth 条件时兜底键归空
    row2 = core.SiteRow(
        name="s", base_url="https://a.com", auth_method="cookie", checkin_action="api",
        oauth_fallback_provider="linuxdo",
    )
    assert core.task_params(row2, {})["oauth_fallback_provider"] == ""


# ── build_form_plan ──────────────────────────────────────────────────────────
def test_form_plan_relogin() -> None:
    row = core.SiteRow(name="s", base_url="https://a.com", checkin_action="relogin", auth_method="oauth")
    plan = core.build_form_plan(row, _mk_states())
    assert plan.show_oauth and plan.show_browser_ops and plan.show_delete_oauth
    assert not plan.creds_enabled and not plan.show_fallback
    assert plan.capture_text == "捕获 OAuth 登录态"
    assert "已保存" in plan.oauth_status


def test_form_plan_sub2api_token_fallback() -> None:
    row = core.SiteRow(
        name="s", base_url="https://a.com", type="sub2api", auth_method="access_token", checkin_action="api"
    )
    plan = core.build_form_plan(row, {})
    assert plan.show_fallback and plan.creds_enabled and plan.show_state_box
    assert not plan.show_browser_ops and not plan.state_editable
    assert "暂无共享 OAuth 登录态" in plan.oauth_status


def test_form_plan_browser_script_browser_auth() -> None:
    row = core.SiteRow(
        name="s", base_url="https://a.com", auth_method="browser", checkin_action="browser_script",
        oauth_fallback_provider="linuxdo", oauth_fallback_account="default",
    )
    plan = core.build_form_plan(row, _mk_states())
    assert plan.show_script and plan.show_fallback and plan.state_editable
    assert plan.oauth_status.startswith("可选 OAuth：")


def test_form_plan_newapi_api_controls_visible() -> None:
    row = core.SiteRow(name="s", base_url="https://a.com")
    plan = core.build_form_plan(row, {})
    assert plan.show_variant
    assert plan.show_verification
    assert not plan.show_state_box


# ── 校验 / 持久化 ────────────────────────────────────────────────────────────
def test_validate_rows_errors() -> None:
    assert core.validate_rows([core.SiteRow(name="", base_url="https://a.com")]) is not None
    assert core.validate_rows([core.SiteRow(name="a", base_url="")]) is not None
    dup = [core.SiteRow(name="a", base_url="https://a.com"), core.SiteRow(name="a", base_url="https://b.com")]
    assert "重复" in (core.validate_rows(dup) or "")
    # 脚本参数不是 JSON：脚本路径必须用真实存在的文件，否则先撞上路径校验
    bad_args = core.SiteRow(
        name="a", base_url="https://a.com", checkin_action="browser_script", auth_method="browser",
        script="scripts/checkin/jisudeng.py", script_args_text="not json",
    )
    assert "JSON" in (core.validate_rows([bad_args]) or "")
    ok = core.SiteRow(name="a", base_url="https://a.com")
    assert core.validate_rows([ok]) is None


def test_validate_rows_checks_script_path_exists() -> None:
    """路径写错要在保存时就拦住：签到跑在后台，隔很久才会被看到。"""
    missing = core.SiteRow(
        name="a", base_url="https://a.com", checkin_action="browser_script", auth_method="browser",
        script="scripts/checkin/nope.py",
    )
    assert "不存在" in (core.validate_rows([missing]) or "")

    escaping = core.SiteRow(
        name="a", base_url="https://a.com", checkin_action="browser_script", auth_method="browser",
        script="/etc/passwd",
    )
    assert core.validate_rows([escaping]) is not None


def test_api_action_script_is_optional_but_validated() -> None:
    """api 的脚本是可选增强（图形验证码等私改流程），留空合法、写错要拦。"""
    blank = core.SiteRow(name="a", base_url="https://a.com", checkin_action="api")
    assert core.validate_rows([blank]) is None

    good = core.SiteRow(name="a", base_url="https://a.com", checkin_action="api",
                        script="scripts/newapi_captcha.py")
    assert core.validate_rows([good]) is None

    typo = core.SiteRow(name="a", base_url="https://a.com", checkin_action="api",
                        script="scripts/checkin/newapi_captchaa.py")
    assert core.validate_rows([typo]) is not None


def test_form_plan_shows_script_for_api_with_its_own_hint() -> None:
    """同一个「脚本路径」字段在两种方式下含义不同，提示必须跟着变。"""
    api_plan = core.build_form_plan(core.SiteRow(name="s", base_url="https://a.com"), {})
    assert api_plan.show_script
    assert not api_plan.show_script_args, "api 钩子不接收 script_args"
    assert not api_plan.show_script_timeout, "纯 HTTP 脚本没有浏览器超时的概念"
    assert api_plan.script_hint == core.SCRIPT_HINT_API

    bs_plan = core.build_form_plan(
        core.SiteRow(name="s", base_url="https://a.com", auth_method="browser",
                     checkin_action="browser_script"),
        {},
    )
    assert bs_plan.show_script and bs_plan.show_script_args and bs_plan.show_script_timeout
    assert bs_plan.script_hint == core.SCRIPT_HINT_BROWSER


def test_persist_accounts_shapes() -> None:
    rows = [
        core.SiteRow(
            name="script-site", base_url="https://a.com", auth_method="browser",
            checkin_action="browser_script", script="scripts/x.py", script_args_text='{"a": 1}',
            browser_state="ST", verify_ssl=False,
        ),
        core.SiteRow(
            name="api-script",
            base_url="https://b.com",
            checkin_action="api",
            script="scripts/custom_captcha.py",
            script_args_text='{"ignored": true}',
            verification_mode="string_captcha",
        ),
        core.SiteRow(name="relogin-site", base_url="https://c.com", checkin_action="relogin", auth_method="cookie"),
    ]
    accts = core.persist_accounts(rows)
    first, api_script, relogin = accts
    assert first["script"] == "scripts/x.py" and first["script_args"] == {"a": 1}
    assert first["browser_state"] == "ST" and first["verify_ssl"] is False
    assert api_script["script"] == "scripts/custom_captcha.py"
    assert api_script["verification_mode"] == "string_captcha"
    assert "script_args" not in api_script and "script_timeout" not in api_script
    # relogin：auth 矫正为 oauth、不落 browser_state、带 oauth 字段、无 api_variant
    assert relogin["auth_method"] == "oauth"
    assert "browser_state" not in relogin and "api_variant" not in relogin
    assert relogin["oauth_provider"] == "linuxdo"


def test_config_snapshot_stable_and_sensitive_to_changes() -> None:
    rows = [core.SiteRow(name="a", base_url="https://a.com")]
    snap1 = core.config_snapshot(rows, {})
    snap2 = core.config_snapshot([r.copy() for r in rows], {})
    assert snap1 == snap2
    rows[0].cookie = "changed"
    assert core.config_snapshot(rows, {}) != snap1


# ── 剪贴板 / 格式化 ──────────────────────────────────────────────────────────
def test_parse_clipboard_site_variants() -> None:
    data, err = core.parse_clipboard_site('{"name": "x", "base_url": "https://a.com"}')
    assert err == "" and data["name"] == "x"
    data, err = core.parse_clipboard_site('[{"name": "x"}]')
    assert err == "" and data["name"] == "x"
    data, err = core.parse_clipboard_site('{"wrapped": {"cookie": "c"}}')
    assert err == "" and data["name"] == "wrapped" and data["cookie"] == "c"
    _, err = core.parse_clipboard_site("not json")
    assert err
    _, err = core.parse_clipboard_site("")
    assert err


def test_merge_clipboard_site_consumes_collector_three_dimensions() -> None:
    original = core.SiteRow(
        name="old",
        base_url="https://old.invalid",
        type="newapi",
        auth_method="cookie",
        checkin_action="api",
        runtime_id="stable-row",
        referer_path="/custom",
    )
    collector_data = {
        "name": "Sub2 channel",
        "base_url": "https://sub.invalid/",
        "site_profile": "sub2api",
        "auth_method": "oauth",
        "checkin_action": "browser_script",
        "oauth_provider": "github",
        "oauth_account": "work",
        "script": "scripts/checkin/100xlabs.py",
        "access_token": "a.b.c",
        "refresh_token": "refresh",
        "enabled": False,
    }

    merged = core.merge_clipboard_site(original, collector_data)

    assert merged.runtime_id == "stable-row"
    assert merged.type == "sub2api"
    assert merged.auth_method == "oauth"
    assert merged.checkin_action == "browser_script"
    assert merged.oauth_provider == "github"
    assert merged.oauth_account == "work"
    assert merged.access_token == "a.b.c"
    assert merged.refresh_token == "refresh"
    assert merged.enabled is False
    assert merged.referer_path == "/custom"  # 未导入字段保持原值


def test_merge_clipboard_site_accepts_legacy_type_field() -> None:
    original = core.SiteRow(name="old", base_url="https://old.invalid")

    merged = core.merge_clipboard_site(original, {"type": "sub2api", "access_token": "a.b.c"})

    assert merged.type == "sub2api"
    assert merged.access_token == "a.b.c"


def test_format_usd_and_detail_quota() -> None:
    assert core.format_usd(246.1) == "$246.10"
    assert core.format_usd(0.004) == "$0.0040"
    assert core.detail_quota_usd({"current_quota": 500000}) == 1.0
    assert core.detail_quota_usd({"current_quota": 2.5, "quota_is_usd": True}) == 2.5
    assert core.detail_quota_usd({"current_quota": "n/a"}) is None
    assert core.detail_quota_usd(None) is None


# ── StatusStore ──────────────────────────────────────────────────────────────
def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_status_store_merges_by_saved_at(tmp_path: Path) -> None:
    today = time_utils.business_date()
    _write(
        tmp_path / "checkin_result.json",
        {
            "generated_at": time_utils.utc_iso(),
            "business_date": today,
            "results": [
                {"site": "a", "base_url": "https://a.com", "status": "success", "current_quota": "$5.0"},
                {"site": "b", "base_url": "https://b.com", "status": "need_login", "current_quota": "$9.9"},
            ],
        },
    )
    now = time_utils.utc_now()
    _write(
        tmp_path / "gui_status_cache.json",
        {
            "entries": {
                # 更新的手动查询应覆盖批量结果
                "https://a.com|a": {
                    "quota_usd": 7.5, "checked_in": False, "ok": True, "status": "success",
                    "message": "",
                    "saved_at": time_utils.utc_iso(now + timedelta(minutes=5)),
                    "business_date": today,
                },
                # 更旧的条目不应覆盖
                "https://b.com|b": {
                    "quota_usd": 1.0, "checked_in": True, "ok": True, "status": "success",
                    "message": "",
                    "saved_at": time_utils.utc_iso(now - timedelta(minutes=5)),
                    "business_date": today,
                },
            }
        },
    )
    store = core.StatusStore(results_dir=tmp_path)
    store.load()
    a = store.get("https://a.com|a")
    assert a["quota_usd"] == 7.5 and a["checked_in"] is False
    b = store.get("https://b.com|b")
    assert b["status"] == "need_login" and b["quota_usd"] is None and b["last_quota_usd"] == 9.9


def test_status_store_merge_compares_across_timezones(tmp_path: Path) -> None:
    """CI 写 +08:00、GUI 写 UTC 时，字符串比较会得出错误的先后顺序。"""
    today = time_utils.business_date()
    _write(
        tmp_path / "checkin_result.json",
        {
            # UTC 02:00 == 北京 10:00，比下面的 09:00+08:00 更新
            "generated_at": f"{today}T02:00:00Z",
            "business_date": today,
            "results": [
                {"site": "a", "base_url": "https://a.com", "status": "success", "current_quota": "$5.0"},
            ],
        },
    )
    _write(
        tmp_path / "gui_status_cache.json",
        {
            "entries": {
                "https://a.com|a": {
                    "quota_usd": 7.5, "checked_in": False, "ok": True, "status": "success",
                    "message": "", "saved_at": f"{today}T09:00:00+08:00", "business_date": today,
                }
            }
        },
    )
    store = core.StatusStore(results_dir=tmp_path)
    store.load()
    # GUI 条目其实更旧，字符串比较会误判成更新
    assert store.get("https://a.com|a")["quota_usd"] == 5.0


def test_status_store_expires_yesterday_entries(tmp_path: Path) -> None:
    """昨日的「今日已签到」不得继续作为今日状态显示。"""
    yesterday = (time_utils.utc_now() - timedelta(days=1)).isoformat(timespec="seconds")
    _write(
        tmp_path / "checkin_result.json",
        {
            "generated_at": yesterday,
            "results": [
                {"site": "a", "base_url": "https://a.com", "status": "success", "current_quota": "$5.0"},
            ],
        },
    )
    _write(
        tmp_path / "gui_status_cache.json",
        {
            "entries": {
                "https://b.com|b": {
                    "quota_usd": 3.0, "checked_in": True, "ok": True, "status": "already_done",
                    "message": "", "saved_at": yesterday,
                }
            }
        },
    )
    store = core.StatusStore(results_dir=tmp_path)
    store.load()
    assert store.get("https://a.com|a") is None
    assert store.get("https://b.com|b") is None


def test_status_store_drops_unparsable_timestamps(tmp_path: Path) -> None:
    _write(
        tmp_path / "gui_status_cache.json",
        {
            "entries": {
                "https://a.com|a": {
                    "quota_usd": 3.0, "checked_in": True, "ok": True, "status": "success",
                    "message": "", "saved_at": "not-a-timestamp",
                }
            }
        },
    )
    store = core.StatusStore(results_dir=tmp_path)
    store.load()
    assert store.get("https://a.com|a") is None


def test_status_store_prunes_expired_entries_on_save(tmp_path: Path) -> None:
    store = core.StatusStore(results_dir=tmp_path)
    store.apply_query("fresh", {"ok": True, "quota_usd": 1.0, "status": "success", "message": "m"})
    store.entries["stale"] = {
        "quota_usd": 9.0,
        "last_quota_usd": 9.0,
        "checked_in": True,
        "ok": True,
        "status": "already_done",
        "message": "",
        "saved_at": (time_utils.utc_now() - timedelta(days=2)).isoformat(timespec="seconds"),
    }
    store.save()

    payload = json.loads((tmp_path / "gui_status_cache.json").read_text(encoding="utf-8"))
    assert "fresh" in payload["entries"]
    assert "stale" not in payload["entries"]


def test_status_store_apply_query_keeps_last_quota_on_failure(tmp_path: Path) -> None:
    store = core.StatusStore(results_dir=tmp_path)
    store.apply_query("k", {"ok": True, "quota_usd": 3.0, "checked_in": True, "status": "success", "message": "m"})
    entry = store.apply_query("k", {"ok": False, "status": "need_login", "message": "expired"})
    assert entry["quota_usd"] is None
    assert entry["last_quota_usd"] == 3.0
    # 落盘 + 重载后仍可读回
    store2 = core.StatusStore(results_dir=tmp_path)
    store2.load()
    assert store2.get("k")["last_quota_usd"] == 3.0


def test_status_store_apply_checkin_extracts_detail_quota(tmp_path: Path) -> None:
    store = core.StatusStore(results_dir=tmp_path)
    entry = store.apply_checkin("k", {"status": "success", "detail": {"current_quota": 1000000}})
    assert entry["quota_usd"] == 2.0 and entry["checked_in"] is True
    entry = store.apply_checkin("k", {"status": "need_verification", "message": "ts"})
    assert entry["checked_in"] is None and entry["last_quota_usd"] == 2.0


def test_status_store_can_defer_writes_for_gui_queue(tmp_path: Path) -> None:
    store = core.StatusStore(results_dir=tmp_path, autosave=False)

    store.apply_query("k", {"ok": True, "quota_usd": 3.0, "status": "success"})

    path = tmp_path / "gui_status_cache.json"
    assert not path.exists()
    core.StatusStore.write_payload(tmp_path, store.snapshot_payload())
    assert json.loads(path.read_text(encoding="utf-8"))["entries"]["k"]["quota_usd"] == 3.0


def test_status_store_merges_concurrent_gui_snapshots(tmp_path: Path) -> None:
    today = time_utils.business_date()
    now = time_utils.utc_now()

    def entry(quota: float, minutes: int) -> dict:
        return {
            "quota_usd": quota,
            "last_quota_usd": quota,
            "checked_in": True,
            "ok": True,
            "status": "success",
            "message": "",
            "saved_at": time_utils.utc_iso(now + timedelta(minutes=minutes)),
            "business_date": today,
        }

    core.StatusStore.write_payload(
        tmp_path,
        {"business_date": today, "entries": {"shared": entry(9.0, 5), "first": entry(1.0, 0)}},
    )
    # 第二个 GUI 带着陈旧 shared 快照写盘时，不得覆盖第一个 GUI 的更新。
    core.StatusStore.write_payload(
        tmp_path,
        {"business_date": today, "entries": {"shared": entry(2.0, -5), "second": entry(2.0, 1)}},
    )

    entries = json.loads((tmp_path / "gui_status_cache.json").read_text(encoding="utf-8"))["entries"]
    assert entries["shared"]["quota_usd"] == 9.0
    assert {"first", "second"} <= entries.keys()


def test_status_store_rolls_over_while_gui_stays_open(tmp_path: Path, monkeypatch) -> None:
    day = {"value": "2099-01-01"}
    monkeypatch.setattr(time_utils, "business_date", lambda: day["value"])
    store = core.StatusStore(results_dir=tmp_path, autosave=False)
    store.apply_query("old", {"ok": True, "quota_usd": 1.0, "status": "success"})

    day["value"] = "2099-01-02"

    assert store.get("old") is None
    assert store.today == "2099-01-02"


def test_config_save_request_freezes_mutable_gui_state(monkeypatch) -> None:
    row = core.SiteRow(name="before", base_url="https://site.invalid", access_token="old-token")
    oauth_states = _mk_states(state="old-state")
    previous = core.credential_snapshots([row])
    request = config_store.build_save_request([row], oauth_states, previous)

    row.name = "after"
    row.access_token = "new-token"
    oauth_states["linuxdo"]["accounts"]["default"]["state"] = "new-state"

    assert request.accounts[0]["name"] == "before"
    assert request.accounts[0]["access_token"] == "old-token"
    assert request.oauth_states["linuxdo"]["accounts"]["default"]["state"] == "old-state"
    assert request.rows[0].runtime_id == row.runtime_id

    calls: dict[str, object] = {}
    monkeypatch.setattr(
        config_store.accounts_store,
        "save_accounts",
        lambda accounts, oauth_states: calls.update(accounts=accounts, oauth_states=oauth_states),
    )
    monkeypatch.setattr(
        config_store.core,
        "apply_credential_cache_changes",
        lambda rows, saved: calls.update(rows=rows, saved=saved) or 2,
    )

    assert request.persist() == 2
    assert calls["accounts"] == request.accounts
    assert calls["saved"] == previous


def test_summarize(tmp_path: Path) -> None:
    store = core.StatusStore(results_dir=tmp_path)
    rows = [
        core.SiteRow(name="a", base_url="https://a.com"),
        core.SiteRow(name="b", base_url="https://b.com", enabled=False),
    ]
    store.apply_query(core.StatusStore.status_key(rows[0]), {"ok": True, "quota_usd": 4.0, "checked_in": True})
    store.apply_query(core.StatusStore.status_key(rows[1]), {"ok": False, "status": "need_login"})
    stats = core.summarize(rows, store)
    assert stats.total == 2 and stats.enabled == 1
    assert stats.done == 1 and stats.failed == 1
    assert stats.quota_sum == 4.0 and stats.quota_known == 1


# ── 脱敏日志 ─────────────────────────────────────────────────────────────────
def test_safe_log_value_redacts_sensitive_keys() -> None:
    assert "redacted" in core._safe_log_value("secret-token-value", "access_token")
    nested = core._safe_log_value({"cookie": "abc", "plain": "ok"}, "result")
    assert "abc" not in nested and "ok" in nested


def test_log_sink_receives_lines() -> None:
    lines: list[str] = []
    core.add_log_sink(lines.append)
    try:
        core.bg_log("INFO", "hello", site="x")
    finally:
        core.remove_log_sink(lines.append)
    assert lines and "hello" in lines[0] and "site=x" in lines[0]


# ── 接口凭据可编辑性 / token 健康度 ──────────────────────────────────────────
def test_sub2api_token_inputs_editable_under_browser_auth() -> None:
    """sub2api 即使用 browser 登录态，签到仍先走纯 API，故 token 框必须可编辑。

    回归：此前 token 框跟随 creds_enabled（只在 access_token/cookie 时启用），
    导致 auth_method=browser 的 sub2api 站点无法手填 token，表现为「填了仍显示没有」。
    """
    row = core.SiteRow(
        name="s", base_url="https://s.invalid", type="sub2api",
        auth_method="browser", checkin_action="browser_script", script="x.js",
    )
    plan = core.build_form_plan(row, {})
    assert plan.creds_enabled is False          # cookie/uid 仍按登录方式灰掉
    assert plan.token_enabled is True           # 但接口凭据必须可填
    assert plan.show_refresh_input is True


def test_newapi_cookie_auth_keeps_token_enabled() -> None:
    row = core.SiteRow(name="n", base_url="https://n.invalid", type="newapi", auth_method="cookie")
    plan = core.build_form_plan(row, {})
    assert plan.token_enabled is True
    assert plan.show_refresh_input is False     # refresh_token 只对 sub2api 有意义


def test_newapi_browser_auth_disables_token() -> None:
    row = core.SiteRow(name="n", base_url="https://n.invalid", type="newapi", auth_method="browser")
    plan = core.build_form_plan(row, {})
    assert plan.token_enabled is False


def test_token_defect_flags_non_ascii_ellipsis() -> None:
    """从截断显示里复制的 token 带 U+2026，运行时被静默判为空，必须提示。"""
    msg = core.token_defect("eyJhbGciOi\u2026Q1NH0.xCpdND.sig")
    assert "U+2026" in msg


def test_token_defect_flags_placeholder_and_shape() -> None:
    assert "占位" in core.token_defect("<在站点后台采集的 access_token>")
    assert "JWT" in core.token_defect("abcdef")


def test_token_defect_accepts_valid_jwt_with_prefix() -> None:
    assert core.token_defect("Bearer aaa.bbb.ccc") == ""
    assert core.token_defect("aaa.bbb.ccc") == ""
    assert core.token_defect("") == ""
    assert core.token_defect("   ") == ""


def test_refresh_status_surfaces_defect_before_refresh_hint() -> None:
    row = core.SiteRow(
        name="s", base_url="https://s.invalid", type="sub2api",
        access_token="aa\u2026bb.cc.dd", refresh_token="rt_x",
    )
    plan = core.build_form_plan(row, {})
    assert plan.refresh_status.startswith("\u26a0")
    assert "已保存 refresh_token" in plan.refresh_status


def test_cred_json_roundtrip_includes_refresh_token() -> None:
    """复制凭据 → 剪贴板导入必须往返一致，漏掉 refresh_token 会丢长期凭据。"""
    row = core.SiteRow(
        name="s", base_url="https://s.invalid", type="sub2api",
        access_token="aa.bb.cc", refresh_token="rt_keep",
    )
    data, err = core.parse_clipboard_site(core.cred_json(row))
    assert err == ""
    assert data["access_token"] == "aa.bb.cc"
    assert data["refresh_token"] == "rt_keep"


# ── 配置字段往返（GUI 保存不得丢字段）────────────────────────────────────────
def test_cli_consumed_fields_survive_roundtrip() -> None:
    """checkin.py / run__all_checkin.py 会消费的字段必须能从 GUI 往返落盘。

    这些字段此前只存在于 ACCOUNTS.json 与 CLI，GUI 既不展示也不写回，用户手写的
    值被「保存全部」静默抹掉（实测 5 个字段全部丢失）。
    """
    raw = {
        "name": "s",
        "base_url": "https://s.invalid",
        "site_profile": "sub2api",
        "auth_method": "browser",
        "checkin_action": "browser_script",
        "script": "scripts/checkin/x.py",
        "cookie_file": "secrets/t.txt",
        "referer_path": "/console",
        "auto_refresh_cookie": False,
        "browser_profile": ".p_t",
        "login_selector": "a.login",
    }
    row = core.row_from_store(raw)
    assert row.cookie_file == "secrets/t.txt"
    assert row.referer_path == "/console"
    assert row.auto_refresh_cookie is False
    assert row.browser_profile == ".p_t"
    assert row.login_selector == "a.login"

    saved = core.persist_accounts([row])[0]
    assert saved["cookie_file"] == "secrets/t.txt"
    assert saved["referer_path"] == "/console"
    assert saved["auto_refresh_cookie"] is False
    assert saved["browser_profile"] == ".p_t"
    assert saved["login_selector"] == "a.login"


def test_default_values_do_not_add_noise_keys() -> None:
    """等于默认值时不落盘，避免给每个账号塞进一堆冗余键。"""
    row = core.row_from_store({
        "name": "s",
        "base_url": "https://s.invalid",
        "site_profile": "newapi",
        "auth_method": "cookie",
    })
    saved = core.persist_accounts([row])[0]
    for key in ("cookie_file", "referer_path", "auto_refresh_cookie",
                "browser_profile", "login_selector"):
        assert key not in saved, f"默认值不应写入 {key}"


def test_task_params_passes_cli_fields() -> None:
    """GUI 内单站点执行必须与批量/CI 传同样的参数。"""
    row = core.row_from_store({
        "name": "s",
        "base_url": "https://s.invalid",
        "site_profile": "newapi",
        "auth_method": "cookie",
        "referer_path": "/console",
        "cookie_file": "secrets/t.txt",
        "auto_refresh_cookie": False,
    })
    params = core.task_params(row, {})
    assert params["referer_path"] == "/console"
    assert params["cookie_file"] == "secrets/t.txt"
    assert params["auto_refresh_cookie"] is False
    # 未配置时给出与 SiteConfig 一致的默认值，而不是空串
    plain = core.task_params(core.row_from_store(
        {"name": "p", "base_url": "https://p.invalid"}), {})
    assert plain["referer_path"] == core.REFERER_PATH_DEFAULT
    assert plain["browser_profile"] == core.BROWSER_PROFILE_DEFAULT


# ── GUI 显式凭据与字段级缓存失效 ──────────────────────────────────────────────
def test_task_params_marks_only_changed_credentials_explicit() -> None:
    row = core.SiteRow(
        name="s",
        base_url="https://s.invalid",
        access_token="NEW",
        refresh_token="RT",
        browser_state="STATE",
    )
    saved = {
        "name": "s",
        "base_url": "https://s.invalid",
        "access_token": "OLD",
        "refresh_token": "RT",
        "browser_state": "STATE",
    }

    changed = core.changed_credential_fields(row, saved)
    params = core.task_params(row, {}, explicit_credential_fields=changed)

    assert changed == {"access_token"}
    assert params["_explicit_credential_fields"] == ["access_token"]


def test_changed_credentials_detects_explicit_clear() -> None:
    row = core.SiteRow(name="s", base_url="https://s.invalid", browser_state="")
    saved = {
        "name": "s",
        "base_url": "https://s.invalid",
        "access_token": "",
        "refresh_token": "",
        "browser_state": "OLD-STATE",
    }
    assert core.changed_credential_fields(row, saved) == {"browser_state"}


def test_unrelated_save_does_not_clear_token_cache(tmp_path: Path, monkeypatch) -> None:
    from providers import token_cache

    monkeypatch.setattr(token_cache, "CACHE_PATH", tmp_path / "token_cache.json")
    row = core.SiteRow(
        name="s", base_url="https://s.invalid", access_token="SEED", proxy="http://old"
    )
    saved = core.credential_snapshots([row])
    basis = token_cache.credential_basis("SEED", "", group="token")
    token_cache.save_tokens("s", row.base_url, "FRESH", token_basis=basis)

    row.proxy = "http://new"
    assert core.apply_credential_cache_changes([row], saved) == 0
    assert token_cache.load_tokens("s", row.base_url)["access_token"] == "FRESH"


def test_credential_edit_keeps_cache_entry(tmp_path: Path, monkeypatch) -> None:
    """改凭据不再删缓存：删除不可逆，改错又改回来就白丢一份可用登录态。"""
    from providers import token_cache

    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    row = core.SiteRow(
        name="s",
        base_url="https://s.invalid",
        access_token="OLD",
        refresh_token="OLD-RT",
        browser_state="SEED-STATE",
    )
    saved = core.credential_snapshots([row])
    token_cache.save_tokens(
        "s", row.base_url, "CACHED", "CACHED-RT", browser_state="CACHED-STATE",
        token_basis=token_cache.credential_basis("OLD", "OLD-RT", group="token"),
        state_basis=token_cache.credential_basis(browser_state="SEED-STATE", group="state"),
    )

    row.access_token = "NEW"
    assert core.apply_credential_cache_changes([row], saved) == 1, "应记一次标记，而不是删除"
    cached = token_cache.load_tokens("s", row.base_url)
    assert cached["access_token"] == "CACHED", "值必须留着：改回原凭据即可重新命中"
    assert cached["browser_state"] == "CACHED-STATE"


def test_changed_token_is_not_applied_although_cache_kept(tmp_path: Path, monkeypatch) -> None:
    """留着条目不等于会被用上：basis 与新配置不符时读取侧必须拒绝应用。"""
    from providers import token_cache

    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    token_cache.save_tokens(
        "s", "https://s.invalid", "CACHED", "CACHED-RT", browser_state="CACHED-STATE",
        token_basis=token_cache.credential_basis("OLD", "OLD-RT", group="token"),
        state_basis=token_cache.credential_basis(browser_state="SEED-STATE", group="state"),
    )

    changed = token_cache.resolve_cached_credentials(
        "s", "https://s.invalid",
        configured_access_token="NEW", configured_refresh_token="OLD-RT",
        configured_browser_state="SEED-STATE", path=cache,
    )
    assert "access_token" not in changed and "refresh_token" not in changed
    assert changed["browser_state"] == "CACHED-STATE", "只改 token 不该牵连登录态"

    reverted = token_cache.resolve_cached_credentials(
        "s", "https://s.invalid",
        configured_access_token="OLD", configured_refresh_token="OLD-RT",
        configured_browser_state="SEED-STATE", path=cache,
    )
    assert reverted["access_token"] == "CACHED", "改回原凭据后缓存应重新可用"
