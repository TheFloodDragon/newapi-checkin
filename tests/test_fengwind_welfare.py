# -*- coding: utf-8 -*-
"""Fengwind API 福利站签到脚本回归。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest

from browser import script_loader
from providers.base import ApiError

welfare = script_loader.load_site_script("scripts/checkin/fengwind_welfare.py")


def test_script_declares_own_http_flow() -> None:
    hooks = script_loader.load_script_hooks("scripts/checkin/fengwind_welfare.py")

    assert hooks.owns_http_flow is True
    assert hooks.do_checkin is not None


class FakeClient:
    def __init__(self, token: str = "welfare-jwt") -> None:
        self.base_url = "https://api-welfalre.fengwind.com"
        self.access_token = token
        self.site = SimpleNamespace(
            name="Fengwind API 福利站",
            proxy="",
            verify_ssl=True,
        )


def _status(
    *,
    checked: bool,
    amount: float | None = None,
    credit_status: str = "credited",
) -> dict[str, Any]:
    today = None
    if checked:
        today = {
            "amount": amount,
            "floor_amount": 0.5,
            "bonus_actual": max(0.0, float(amount or 0) - 0.5),
            "tier_name": "基础福利",
            "status": credit_status,
        }
    return {
        "code": 0,
        "data": {
            "enabled": True,
            "checked_in_today": checked,
            "today": today,
            "amount_floor": 0.5,
            "amount_cap": 1.0,
            "current_streak": 3 if checked else 2,
            "longest_streak": 8,
            "biz_date": "2026-08-10",
            "next_reset_at": "2026-08-11T00:00:00+08:00",
        },
    }


def _level(*, eligible: bool = True) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "profile": {"level": 1},
            "checkin_eligible": eligible,
            "checkin_qualification": "welfare_level" if eligible else "locked",
            "linuxdo_trust_level": 2,
        },
    }


def _history(amount: float = 0.8) -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "items": [
                {
                    "id": 12,
                    "biz_date": "2026-08-10",
                    "amount": amount,
                    "status": "credited",
                    "streak_after": 3,
                    "tier_name": "基础福利",
                    "private_field": "must-not-leak",
                }
            ]
        },
    }


def _install_http(monkeypatch: pytest.MonkeyPatch, replies: dict[tuple[str, str], Any]):
    calls: list[tuple[str, str, dict[str, str]]] = []

    def fake_http_request(url: str, *, method: str, headers: dict[str, str], **_kwargs: Any) -> Any:
        parsed = urlsplit(url)
        path = parsed.path.removeprefix("/api")
        if parsed.query:
            path += "?" + parsed.query
        calls.append((method, path, dict(headers)))
        reply = replies[(method, path)]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(welfare, "http_request", fake_http_request)
    return calls


def test_already_checked_in_returns_amount_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_http(
        monkeypatch,
        {
            ("GET", welfare.STATUS_PATH): _status(checked=True, amount=0.8),
            ("GET", "/level"): _level(),
            ("GET", welfare.HISTORY_PATH): _history(),
        },
    )

    reward = welfare.do_checkin(FakeClient())

    assert reward.already_done is True
    assert reward.extra["result_message"] == "今日已签到，获得 $0.80（已入账）"
    status = reward.extra["welfare_status"]
    assert status["biz_date"] == "2026-08-10"
    assert status["current_streak"] == 3
    assert status["history"][0]["amount"] == 0.8
    assert "private_field" not in status["history"][0]
    assert not any(method == "POST" for method, _path, _headers in calls)


def test_first_checkin_records_post_status_and_history(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_http(
        monkeypatch,
        {
            ("GET", welfare.STATUS_PATH): [
                _status(checked=False),
                _status(checked=True, amount=0.65, credit_status="pending_credit"),
            ],
            ("GET", "/level"): _level(),
            ("GET", welfare.HISTORY_PATH): [_history(0.5), _history(0.65)],
            ("POST", welfare.CHECKIN_PATH): {
                "code": 0,
                "data": {"amount": 0.65, "status": "pending_credit"},
            },
        },
    )

    reward = welfare.do_checkin(FakeClient())

    assert reward.already_done is False
    assert reward.quota_awarded == pytest.approx(0.65)
    assert reward.extra["result_message"] == "签到成功，获得 $0.65（入账中）"
    assert reward.extra["completion_signal"] == "welfare_checkin_response"
    assert reward.extra["today"]["bonus_actual"] == pytest.approx(0.15)
    assert sum(method == "POST" for method, _path, _headers in calls) == 1
    assert all(headers["Authorization"] == "Bearer welfare-jwt" for _method, _path, headers in calls)


def test_ineligible_account_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_http(
        monkeypatch,
        {
            ("GET", welfare.STATUS_PATH): _status(checked=False),
            ("GET", "/level"): _level(eligible=False),
            ("GET", welfare.HISTORY_PATH): _history(),
        },
    )

    with pytest.raises(ApiError, match="不具备签到资格") as caught:
        welfare.do_checkin(FakeClient())

    assert caught.value.status == 403
    assert not any(method == "POST" for method, _path, _headers in calls)


def test_ambiguous_post_recovers_from_checked_status(monkeypatch: pytest.MonkeyPatch) -> None:
    transient = ApiError(None, None, "network timeout", transient=True)
    _install_http(
        monkeypatch,
        {
            ("GET", welfare.STATUS_PATH): [
                _status(checked=False),
                _status(checked=True, amount=0.55),
            ],
            ("GET", "/level"): _level(),
            ("GET", welfare.HISTORY_PATH): _history(0.55),
            ("POST", welfare.CHECKIN_PATH): transient,
        },
    )

    reward = welfare.do_checkin(FakeClient())

    assert reward.already_done is True
    assert reward.extra["result_message"] == "今日已签到，获得 $0.55（已入账）"


def test_missing_welfare_token_fails_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        welfare,
        "http_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("不应发请求")),
    )

    with pytest.raises(ApiError, match="缺少 welfare_token") as caught:
        welfare.do_checkin(FakeClient(token=""))

    assert caught.value.status == 401


def test_logs_never_include_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_http(
        monkeypatch,
        {
            ("GET", welfare.STATUS_PATH): _status(checked=True, amount=0.8),
            ("GET", "/level"): _level(),
            ("GET", welfare.HISTORY_PATH): _history(),
        },
    )
    logs: list[str] = []

    welfare.do_checkin(FakeClient(token="SuperSecretWelfareToken"), log=logs.append)

    assert "SuperSecretWelfareToken" not in "\n".join(logs)
