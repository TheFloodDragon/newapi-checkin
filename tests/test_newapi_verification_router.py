from __future__ import annotations

from types import SimpleNamespace

import pytest

from providers.base import ApiError, CheckinReward
from scripts import newapi_verification as router


def _client(mode: str = "auto", checkin_status: dict | None = None):
    return SimpleNamespace(
        site=SimpleNamespace(verification_mode=mode),
        _checkin_status_data=checkin_status or {},
    )


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


def test_checkin_status_code_required_reports_actionable_result_instead_of_blind_submit() -> None:
    """星芽的 code_required 是「每日口令」，不是图形验证码。

    实测证据：前端只渲染一个「今日签到口令」输入框（Code is valid for today only），
    按 {"code": ...} 提交 POST /api/user/checkin；站点没有任何取图/取码端点，
    /api/status 里 turnstile_check=false 且无验证码开关。旧实现裸提交，被服务端回
    「签到验证码不正确」，每天固定失败且看不出该做什么。
    """
    client = _client(checkin_status={"code_required": True})

    with pytest.raises(ApiError, match="今日签到口令") as raised:
        router.do_checkin(client, status_data={})

    assert raised.value.payload == {"code_required": True}
    # 缺配置不是「失败」也不是人机验证：必须归为 need_config，否则报表只显示 ❌ 失败，
    # 用户看不出该做什么。
    from providers.profiles.newapi import NewApiClient

    assert NewApiClient.classify(None, raised.value) == "need_config"  # type: ignore[arg-type]
    # 不得把它误当成图形验证码去探测取图端点。
    assert router._auto_modes({}) == []


def test_configured_daily_code_is_submitted_as_request_body() -> None:
    """配置了今日口令时应直接带 code 提交，而不是报错。"""
    submitted: list[dict] = []
    sentinel = CheckinReward(raw={"quota_awarded": 123})

    client = _client(checkin_status={"code_required": True})
    client.site.script_args = {"checkin_code": " TODAY42 "}
    client._legacy_checkin = lambda turnstile="", code="": submitted.append(
        {"turnstile": turnstile, "code": code}
    ) or {"quota_awarded": 123}
    client._reward_from = lambda data: sentinel

    assert router.do_checkin(client, status_data={}) is sentinel
    assert submitted == [{"turnstile": "", "code": "TODAY42"}], "口令要去掉首尾空白后提交"


def test_code_not_required_keeps_default_flow() -> None:
    """未声明 code_required 的站点行为不变（不读口令、不额外报错）。"""
    client = _client(checkin_status={"code_required": False})
    assert router.do_checkin(client, status_data={}) is None
