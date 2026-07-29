# -*- coding: utf-8 -*-
from __future__ import annotations

from ci.detect_browser import account_needs_browser, needs_browser


def _account(**overrides):
    data = {
        "name": "s",
        "base_url": "https://s.invalid",
        "enabled": True,
        "auth_method": "access_token",
        "checkin_action": "api",
    }
    data.update(overrides)
    return data


def test_oauth_fallback_requires_browser_even_with_access_token() -> None:
    assert account_needs_browser(_account(oauth_fallback_provider="github")) is True


def test_plain_access_token_api_does_not_require_browser() -> None:
    assert account_needs_browser(_account()) is False


def test_disabled_browser_account_does_not_require_browser() -> None:
    assert account_needs_browser(
        _account(enabled=False, auth_method="browser", checkin_action="browser_script")
    ) is False


def test_browser_auth_and_browser_actions_require_browser() -> None:
    assert account_needs_browser(_account(auth_method="browser")) is True
    assert account_needs_browser(_account(auth_method="oauth")) is True
    assert account_needs_browser(_account(checkin_action="relogin")) is True
    assert account_needs_browser(_account(checkin_action="browser_script")) is True


def test_needs_browser_ignores_non_dict_entries() -> None:
    assert needs_browser([None, "bad", _account()]) is False
    assert needs_browser([_account(), _account(oauth_fallback_provider="linuxdo")]) is True
