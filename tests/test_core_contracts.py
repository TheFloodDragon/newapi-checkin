from __future__ import annotations

from types import ModuleType

import accounts_store
import providers
import run__all_checkin as runner
from browser.script_contract import LoadedSiteScript
from checkin_core.auth import PasswordLoginCapable, TokenRefreshCapable, effective_auth
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
