from __future__ import annotations

from types import ModuleType
import subprocess
import sys

import accounts_store
import providers
import run__all_checkin as runner
from browser.script_contract import LoadedSiteScript
import pytest

from checkin_core.auth import PasswordLoginCapable, TokenRefreshCapable, effective_auth
from checkin_core.batch import run_serial_groups, serial_groups
from checkin_core.enums import (
    ACTION_VALUES,
    AUTH_METHOD_VALUES,
    OK_STATUSES,
    PROFILE_VALUES,
    VALID_RESULT_STATUSES,
    ResultStatus,
    status_meta,
)
from checkin_core.events import WorkerEvent


def test_finite_values_are_shared_across_boundaries() -> None:
    assert providers.AUTH_METHODS == AUTH_METHOD_VALUES
    assert providers.CHECKIN_ACTIONS == ACTION_VALUES
    assert set(providers.KNOWN_PROFILES) == set(PROFILE_VALUES)
    assert set(runner.VALID_RESULT_STATUSES) == set(VALID_RESULT_STATUSES)
    assert set(runner.OK_STATUSES) == set(OK_STATUSES)


def test_action_auth_constraints_are_applied_during_storage_normalization() -> None:
    migrated = accounts_store.migrate_fields(
        {
            "site_profile": "sub2api",
            "auth_method": "cookie",
            "checkin_action": "browser_script",
        }
    )

    assert effective_auth("browser_script", "cookie") == "oauth"
    assert migrated["auth_method"] == "oauth"


def test_status_metadata_drives_cli_labels_and_icons() -> None:
    for status in ResultStatus:
        meta = status_meta(status)
        assert runner.compact_status(status.value, 0) == meta.label
        assert runner.status_icon(status.value, 0) == meta.icon


def test_not_open_is_neutral_but_not_labeled_success() -> None:
    assert ResultStatus.NOT_OPEN.value in OK_STATUSES
    meta = status_meta(ResultStatus.NOT_OPEN)
    assert meta.ok is True
    assert meta.label == "未开放"
    assert meta.icon == "🚧"


def test_worker_event_roundtrip_is_versioned_and_redacted() -> None:
    event = WorkerEvent(
        stage="api_first",
        site="s",
        message="refresh",
        fields={"access_token": "top-secret-token", "attempt": 2},
    )

    line = event.to_line()
    restored = WorkerEvent.from_line(line)

    assert "top-secret-token" not in line
    assert restored is not None
    assert restored.stage == "api_first"
    assert restored.site == "s"
    assert restored.fields == {"access_token": "<redacted>", "attempt": 2}
    assert WorkerEvent.from_line("ordinary diagnostic") is None


def test_browser_package_keeps_hcaptcha_modules_lazy() -> None:
    code = (
        "import json, sys; import browser; "
        "print(json.dumps({"
        "'hcaptcha': 'browser.hcaptcha' in sys.modules, "
        "'vision': 'browser.openai_vision' in sys.modules, "
        "'playwright': 'playwright' in sys.modules}))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == '{"hcaptcha": false, "vision": false, "playwright": false}'


def test_loaded_script_contract_resolves_supported_hooks() -> None:
    module = ModuleType("site_script")
    module.run = lambda *_args: None
    module.do_checkin = lambda *_args, **_kwargs: None
    module.unrelated = "ignored"

    hooks = LoadedSiteScript.from_module(module)

    assert hooks.require_browser_run() is module.run
    assert hooks.do_checkin is module.do_checkin
    assert hooks.run_http_extras is None


def test_auth_capability_protocols_replace_getattr_probing() -> None:
    class Capable:
        def refresh_token_via_http(self, site, log=None):
            return {"access_token": "token"}

        def http_password_login(self, site, email, password, log=None):
            return {"access_token": "token"}

    capable = Capable()
    assert isinstance(capable, TokenRefreshCapable)
    assert isinstance(capable, PasswordLoginCapable)


def test_serial_groups_keeps_same_site_together_and_isolates_blank_keys() -> None:
    items = [("a", "s1"), ("b", "s2"), ("c", "s1"), ("d", ""), ("e", "")]

    groups = serial_groups(items, lambda pair: pair[1])

    assert [[name for name, _ in group] for group in groups] == [["a", "c"], ["b"], ["d"], ["e"]]


def test_run_serial_groups_serializes_per_site_and_preserves_input_order() -> None:
    import threading

    active: dict[str, int] = {}
    overlaps: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)

    def execute(item: tuple[str, str]) -> str:
        name, site = item
        with lock:
            active[site] = active.get(site, 0) + 1
            if active[site] > 1:
                overlaps.append(site)
        try:
            # 不同站点必须能真正并行；同站并行会让 barrier 超时暴露串行被破坏。
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        with lock:
            active[site] -= 1
        return name

    items = [("a", "s1"), ("b", "s2"), ("c", "s1")]
    results = run_serial_groups(
        items,
        key=lambda pair: pair[1],
        execute=execute,
        on_error=lambda item, exc: f"{item[0]}:error",
    )

    assert results == ["a", "b", "c"]
    assert not overlaps


def test_run_serial_groups_reports_failures_without_aborting_batch() -> None:
    def execute(name: str) -> str:
        if name == "bad":
            raise RuntimeError("boom")
        return name

    results = run_serial_groups(
        ["ok1", "bad", "ok2"],
        key=lambda name: name,
        execute=execute,
        on_error=lambda name, exc: f"{name}:{exc}",
    )

    assert results == ["ok1", "bad:boom", "ok2"]


def test_run_serial_groups_propagates_keyboard_interrupt() -> None:
    """Ctrl-C 必须中断批量，不能被收敛成一条普通失败结果。"""

    def execute(name: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_serial_groups(
            ["only"],
            key=lambda name: name,
            execute=execute,
            on_error=lambda name, exc: "swallowed",
        )
