from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers.base import ApiError, CheckinReward
from scripts import newapi_verification as router


def _client(mode: str = "auto"):
    return SimpleNamespace(site=SimpleNamespace(verification_mode=mode))


def test_auto_detection_uses_mechanism_names() -> None:
    assert router._auto_modes(
        {
            "captcha_checkin_enabled": True,
            "captcha_type": "click-shape",
            "turnstile_check": True,
            "turnstile_site_key": "key",
        }
    ) == ["click_shape", "turnstile"]
    assert router._auto_modes(
        {"checkin_captcha_enabled": True, "captcha_type": "string"}
    ) == ["bitmap_code", "string_captcha"]


def test_selected_mode_runs_first_then_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    sentinel = CheckinReward(raw={"ok": True})

    def run(_client, mode, _options, log=None):
        calls.append(mode)
        return sentinel if mode == "click_shape" else None

    monkeypatch.setattr(router, "_run_mode", run)
    result = router.do_checkin(
        _client("bitmap_code"),
        status_data={"captcha_checkin_enabled": True, "captcha_type": "click-shape"},
    )
    assert result is sentinel
    assert calls == ["bitmap_code", "click_shape"]


def test_selected_not_applicable_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    sentinel = CheckinReward(raw={"ok": True})

    def run(_client, mode, _options, log=None):
        calls.append(mode)
        if mode == "string_captcha":
            raise ApiError(404, None, "Invalid URL")
        return sentinel

    monkeypatch.setattr(router, "_run_mode", run)
    result = router.do_checkin(
        _client("string_captcha"),
        status_data={"turnstile_check": True, "turnstile_site_key": "key"},
    )
    assert result is sentinel
    assert calls == ["string_captcha", "turnstile"]


def test_applicable_failure_does_not_switch_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def run(_client, mode, _options, log=None):
        calls.append(mode)
        raise ApiError(None, None, "验证码错误")

    monkeypatch.setattr(router, "_run_mode", run)
    with pytest.raises(ApiError, match="验证码错误"):
        router.do_checkin(
            _client("bitmap_code"),
            status_data={"turnstile_check": True, "turnstile_site_key": "key"},
        )
    assert calls == ["bitmap_code"]


def test_auto_without_detected_verification_returns_none() -> None:
    assert router.do_checkin(_client(), status_data={}) is None
