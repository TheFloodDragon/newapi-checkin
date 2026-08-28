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


# ── 双层 SSO 链路 ────────────────────────────────────────────────────────────
# 实测链路：福利站 → 主站 /sso/continue → 主站 /login → 主站 linuxdo start
# → connect.linux.do/oauth2/authorize（「允许」是 a[href^="/oauth2/approve"]）
# → linux.do/session/sso_provider（302，可能被 Cloudflare Turnstile 挡住）
# → 主站 callback → 福利站 /auth/callback?code=...
WELFARE_ORIGIN = "https://api-welfalre.fengwind.com"
MAIN_ORIGIN = "https://api.fengwind.com"
LOGIN_URL = (
    f"{MAIN_ORIGIN}/sso/continue?client_id=welfare"
    f"&redirect_uri=https%3A%2F%2Fapi-welfalre.fengwind.com%2Fauth%2Fcallback&state=s1"
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{WELFARE_ORIGIN}/auth/callback?code=abc&state=s1", "welfare_callback"),
        (f"{WELFARE_ORIGIN}/", "welfare_home"),
        (f"{MAIN_ORIGIN}/login?redirect=%2Fsso%2Fcontinue", "main_login"),
        (LOGIN_URL, "main_other"),
        (f"{MAIN_ORIGIN}/auth/linuxdo/callback", "main_other"),
        ("https://connect.linux.do/oauth2/authorize?client_id=SP", "connect"),
        ("https://linux.do/session/sso_provider?sig=1&sso=2", "linuxdo"),
        ("https://linux.do/login", "linuxdo"),
        ("https://elsewhere.invalid/x", "other"),
        ("", "other"),
    ],
)
def test_sso_stage_classifies_every_hop(url: str, expected: str) -> None:
    assert welfare._sso_stage(url, WELFARE_ORIGIN, MAIN_ORIGIN) == expected


def test_linuxdo_start_url_navigates_instead_of_clicking_spa_button() -> None:
    """主站按钮在 humanize 轨迹下三种点击都会超时；起跳端点是等价且确定的入口。"""
    start = welfare._linuxdo_start_url(LOGIN_URL)

    assert start.startswith(f"{MAIN_ORIGIN}/api/v1/auth/oauth/linuxdo/start?redirect=")
    assert "%2Fsso%2Fcontinue" in start
    assert "state%3Ds1" in start
    assert welfare._linuxdo_start_url("not-a-url") == ""


def test_sso_failure_message_names_the_stalled_hop() -> None:
    """三种停住方式该做的事完全不同，结论必须能区分（旧实现只有一句通用文案）。"""
    provider_login = welfare._sso_failure_message({"reason": "provider_login", "stage": "linuxdo"})
    timeout = welfare._sso_failure_message({"reason": "timeout", "stage": "connect"})

    assert "linux.do 登录页" in provider_login and "重新捕获" in provider_login
    assert "LINUX DO Connect 授权页" in timeout
    assert timeout != provider_login


class FakeLocator:
    def __init__(self, page: FakePage, selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self) -> FakeLocator:
        return self

    async def count(self) -> int:
        return 1 if self._selector in self._page.present else 0

    async def is_visible(self) -> bool:
        return self._selector in self._page.present

    async def click(self, **_kwargs: Any) -> None:
        self._page.clicks.append(self._selector)
        self._page.present.pop(self._selector, None)
        self._page.url = self._page.present_target(self._selector)

    async def dispatch_event(self, _event: str) -> None:
        await self.click()


class FakePage:
    """按「点了什么 / 跳到哪」驱动的假页面，用于断言状态机的推进路径。"""

    def __init__(self, url: str, present: dict[str, str] | None = None, token: str = "wt-1") -> None:
        self.url = url
        self.present = dict(present or {})
        self.clicks: list[str] = []
        self.gotos: list[str] = []
        self.reloads = 0
        self.token = token
        self.stored_token = ""

    def present_target(self, selector: str) -> str:
        return self.present_targets[selector]

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self.gotos.append(url)
        self.url = self.goto_targets.get(url, url)

    async def reload(self, **_kwargs: Any) -> None:
        self.reloads += 1

    async def wait_for_timeout(self, _ms: int) -> None:
        return None

    async def evaluate(self, script: str, _arg: Any = None) -> Any:
        if "sso/exchange" in script:
            self.stored_token = self.token
            return {"ok": True, "stage": "exchanged"}
        if "/api/me" in script:
            return bool(self.stored_token)
        if "localStorage.getItem" in script:
            return self.stored_token
        return ""


class FakeHelpers:
    def __init__(self) -> None:
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(str(message))

    def remaining_seconds(self) -> float:
        return 200.0


def _run_chain(page: FakePage, helpers: FakeHelpers) -> dict[str, Any]:
    import asyncio

    return asyncio.run(
        welfare._drive_sso_chain(page, helpers, WELFARE_ORIGIN, LOGIN_URL, "s1", 20.0)
    )


def test_chain_walks_login_page_consent_and_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """主站登录页 → 起跳端点 → 授权同意页 → 福利站 callback 应一路自动走完。"""
    start_url = welfare._linuxdo_start_url(LOGIN_URL)
    callback = f"{WELFARE_ORIGIN}/auth/callback?code=abc&state=s1"
    page = FakePage(f"{MAIN_ORIGIN}/login?redirect=%2Fsso%2Fcontinue")
    page.goto_targets = {start_url: "https://connect.linux.do/oauth2/authorize?client_id=SP"}
    page.present = {"a[href^=\"/oauth2/approve\"]": callback}
    page.present_targets = {"a[href^=\"/oauth2/approve\"]": callback}
    helpers = FakeHelpers()

    outcome = _run_chain(page, helpers)

    assert outcome == {"token": "wt-1", "reason": "", "stage": "welfare_callback"}
    # 主站按钮一次都不点：它在 humanize 下点不动，起跳端点才是确定入口。
    assert page.gotos == [start_url]
    assert page.clicks == ['a[href^="/oauth2/approve"]']
    assert any("直连 LinuxDO OAuth 起跳端点" in line for line in helpers.logs)


def test_chain_stops_immediately_on_provider_login_page() -> None:
    """停在 linux.do 登录页说明共享登录态失效，必须立刻收敛而不是耗完预算。"""
    page = FakePage("https://linux.do/login")
    page.goto_targets = {}
    page.present = {"#login-account-name": ""}
    page.present_targets = {"#login-account-name": ""}
    helpers = FakeHelpers()

    outcome = _run_chain(page, helpers)

    assert outcome["reason"] == "provider_login"
    assert outcome["stage"] == "linuxdo"
    assert page.clicks == [] and page.reloads == 0


def test_logged_in_main_site_reopens_sso_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    """主站已登录时靠重新发起 /sso/continue 把 code 带回福利站（旧实现缺这一步）。"""
    monkeypatch.setattr(welfare, "STAGE_SETTLE_SECONDS", 0.0)
    callback = f"{WELFARE_ORIGIN}/auth/callback?code=abc&state=s1"
    page = FakePage(f"{MAIN_ORIGIN}/dashboard")
    page.goto_targets = {LOGIN_URL: callback}
    page.present_targets = {}
    helpers = FakeHelpers()

    outcome = _run_chain(page, helpers)

    assert outcome["token"] == "wt-1"
    assert page.gotos == [LOGIN_URL]


def test_stalled_hop_is_nudged_instead_of_waited_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """302 中转跳被 Cloudflare 挡住时要主动解验证并重试该跳，而不是干等到超时。"""
    monkeypatch.setattr(welfare, "STAGE_STALL_SECONDS", 0.0)
    solved: list[bool] = []

    async def fake_solve(_page: Any, log: Any = None) -> bool:
        solved.append(True)
        return True

    monkeypatch.setattr(welfare.bypass, "solve_cloudflare", fake_solve)
    page = FakePage("https://linux.do/session/sso_provider?sig=1&sso=2")
    page.goto_targets = {LOGIN_URL: f"{WELFARE_ORIGIN}/auth/callback?code=abc&state=s1"}
    page.present_targets = {}
    helpers = FakeHelpers()

    outcome = _run_chain(page, helpers)

    assert solved, "停滞的一跳必须先尝试解 Cloudflare"
    assert page.reloads >= 1
    assert outcome["token"] == "wt-1"
