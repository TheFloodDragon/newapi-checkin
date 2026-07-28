# -*- coding: utf-8 -*-
"""browser_script 的「API 优先」三级降级链路。

顺序（用户要求）：
1. 已保存的 access_token（过期时 profile 用 refresh_token 纯 HTTP 续期）；
2. 纯 HTTP 账密登录换新 token（站点未启用 Turnstile 时可行）；
3. 都不行才降级到浏览器脚本（返回 None）。

实站确证（2026-07-28）：
- 极速蹬 turnstile_enabled=false，纯 HTTP 账密登录 200 并可签到；
- 百倍 turnstile_enabled=true，纯 HTTP 登录必被拒，需回落浏览器。
"""

from __future__ import annotations

from typing import Any

from providers.actions import browser_script
from providers.base import ApiError, CheckinReward, SiteConfig, StatusInfo


class FakeClient:
    def __init__(self, *, status: Any = None, status_error: ApiError | None = None,
                 reward: CheckinReward | None = None) -> None:
        self._status = status
        self._status_error = status_error
        self._reward = reward or CheckinReward(quota_awarded=0.5)
        self.checkin_calls = 0

    def fetch_status(self) -> Any:
        if self._status_error is not None:
            raise self._status_error
        return self._status

    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        self.checkin_calls += 1
        return self._reward


class FakeProfile:
    """按 token 值分发客户端，模拟「旧 token 失效、新 token 可用」。"""

    def __init__(self, clients: dict[str, FakeClient], login_result: dict[str, str] | None = None) -> None:
        self.clients = clients
        self.login_result = login_result
        self.login_calls = 0
        self.lazy_calls = 0

    def build_client(self, site: SiteConfig, auth: Any) -> FakeClient:
        return self.clients[auth.access_token]

    def build_lazy_refresh_client(self, site: SiteConfig) -> FakeClient | None:
        # 第 1 级必须是纯 HTTP：若被调用说明会拉起浏览器，属实现回退。
        self.lazy_calls += 1
        return None

    def classify(self, error: ApiError) -> str:
        if error.status == 401:
            return "need_login"
        return "error"

    def http_password_login(self, site, email, password, log=None):
        self.login_calls += 1
        if log:
            log("fake login")
        return dict(self.login_result or {})


def _site(**kw: Any) -> SiteConfig:
    base = {
        "name": "s",
        "base_url": "https://s.invalid",
        "site_profile": "sub2api",
        "auth_method": "browser",
        "checkin_action": "browser_script",
        "script": "scripts/checkin/jisudeng.py",
        "access_token": "old",
    }
    base.update(kw)
    return SiteConfig(**base)


def _no_persist(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_script.accounts_store, "update_account_access_token",
        lambda *a, **k: False,
    )


def test_stage1_token_already_checked_in_returns_without_login(monkeypatch) -> None:
    client = FakeClient(status=StatusInfo(checked_in_today=True, quota_usd=26.55))
    profile = FakeProfile({"old": client})

    result = browser_script._try_api_checkin(_site(), profile)

    assert result is not None and result.status == "already_done"
    assert result.detail["api_stage"] == "token"
    assert profile.login_calls == 0
    assert client.checkin_calls == 0


def test_stage1_does_not_use_browser_lazy_client(monkeypatch) -> None:
    """第 1 级必须纯 HTTP：lazy client 的 refresher 会拉起 Camoufox。"""
    client = FakeClient(status=StatusInfo(checked_in_today=True))
    profile = FakeProfile({"old": client})

    browser_script._try_api_checkin(_site(), profile)

    assert profile.lazy_calls == 0


def test_stage2_password_login_recovers_expired_token(monkeypatch) -> None:
    _no_persist(monkeypatch)
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))
    fresh = FakeClient(status=StatusInfo(checked_in_today=False))
    profile = FakeProfile(
        {"old": expired, "new": fresh},
        login_result={"access_token": "new", "refresh_token": "rt"},
    )
    site = _site(script_args={"email": "a@b.c", "password": "pw"})

    result = browser_script._try_api_checkin(site, profile)

    assert result is not None and result.status == "success"
    assert result.detail["api_stage"] == "password"
    assert profile.login_calls == 1
    assert site.access_token == "new"
    assert site.refresh_token == "rt"


def test_stage2_skipped_without_credentials(monkeypatch) -> None:
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))
    profile = FakeProfile({"old": expired})

    result = browser_script._try_api_checkin(_site(), profile)

    assert result is None
    assert profile.login_calls == 0


def test_falls_back_to_browser_when_login_yields_no_token(monkeypatch) -> None:
    _no_persist(monkeypatch)
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))
    profile = FakeProfile({"old": expired}, login_result={})
    site = _site(script_args={"email": "a@b.c", "password": "pw"})

    assert browser_script._try_api_checkin(site, profile) is None
    assert profile.login_calls == 1


def test_unconfirmed_reward_does_not_report_success(monkeypatch) -> None:
    """接口回 200 但无签到证据时不谎报成功，交浏览器脚本确认。"""
    client = FakeClient(
        status=StatusInfo(checked_in_today=False),
        reward=CheckinReward(checkin_unconfirmed=True),
    )
    profile = FakeProfile({"old": client})

    assert browser_script._try_api_checkin(_site(), profile) is None


def test_logs_every_stage(monkeypatch, capsys) -> None:
    _no_persist(monkeypatch)
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))
    fresh = FakeClient(status=StatusInfo(checked_in_today=True))
    profile = FakeProfile(
        {"old": expired, "new": fresh},
        login_result={"access_token": "new"},
    )
    site = _site(script_args={"email": "a@b.c", "password": "pw"})

    browser_script._try_api_checkin(site, profile)
    err = capsys.readouterr().err

    assert "尝试纯 API 签到" in err
    assert "账密登录" in err
    assert "api_first:s" in err


def test_credentials_never_appear_in_logs(monkeypatch, capsys) -> None:
    _no_persist(monkeypatch)
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))
    profile = FakeProfile({"old": expired}, login_result={})
    site = _site(script_args={"email": "user@x.test", "password": "SuperSecret123"})

    browser_script._try_api_checkin(site, profile)
    err = capsys.readouterr().err

    assert "SuperSecret123" not in err


def test_turnstile_site_skips_http_login(monkeypatch) -> None:
    """站点启用 Turnstile 时纯 HTTP 登录必被拒，应直接降级（实测百倍）。"""
    expired = FakeClient(status_error=ApiError(401, None, "Token has expired"))

    class TurnstileProfile(FakeProfile):
        def http_password_login(self, site, email, password, log=None):
            self.login_calls += 1
            if log:
                log("站点声明启用 Turnstile，纯 HTTP 账密登录不可用，需回落浏览器")
            return {}

    profile = TurnstileProfile({"old": expired})
    site = _site(script_args={"email": "a@b.c", "password": "pw"})

    assert browser_script._try_api_checkin(site, profile) is None


# ── 失败日志必须带可诊断上下文（站点 / 状态码 / 服务端 reason）──────────────
def test_describe_failure_surfaces_status_and_server_reason() -> None:
    """排查凭据问题时，状态码和 reason 是区分「过期」与「被作废」的唯一依据。

    以前只打 exc.message，401/403 与 REFRESH_TOKEN_INVALID 全看不到，
    只能另写脚本手打接口才能区分，本测试锁住这些字段出现在日志里。
    """
    from providers.actions.browser_script import _describe_failure
    from providers.base import ApiError

    text = _describe_failure(
        ApiError(
            401,
            {"code": 401, "message": "invalid refresh token", "reason": "REFRESH_TOKEN_INVALID"},
            "invalid refresh token",
        )
    )
    assert "HTTP 401" in text
    assert "REFRESH_TOKEN_INVALID" in text


def test_describe_failure_marks_transient_errors() -> None:
    """瞬时错误要标出来：这类失败重试即可，不该让用户去换凭据。"""
    from providers.actions.browser_script import _describe_failure
    from providers.base import ApiError

    assert "可重试" in _describe_failure(ApiError(None, None, "timeout", transient=True))
    assert "可重试" not in _describe_failure(ApiError(403, None, "Forbidden"))


def test_missing_token_distinguishes_empty_from_corrupt() -> None:
    """「填了但值损坏」与「压根没填」必须能从日志区分开。

    normalize_access_token 把含非 ASCII 的值静默判空（HTTP 头只能承载 latin-1），
    最常见来源是从截断显示里复制、值中间带了 U+2026 省略号。
    """
    from providers.actions.browser_script import _describe_missing_token

    empty = _site(access_token="")
    assert "为空" in _describe_missing_token(empty)

    corrupt = _site(access_token="eyJhbGci.eyJ1c2Vy…Q1NH0.xCpdND")
    text = _describe_missing_token(corrupt)
    assert "U+2026" in text or "非 ASCII" in text


def test_refresh_failure_detail_is_exposed_by_profile() -> None:
    """profile 侧要把服务端判据交给上层，否则 actions 层无从记录。"""
    from providers.profiles.sub2api import _describe_api_error
    from providers.base import ApiError

    text = _describe_api_error(
        ApiError(401, {"reason": "REFRESH_TOKEN_INVALID"}, "invalid refresh token"),
        "https://s.invalid/api/v1/auth/refresh",
    )
    assert "auth/refresh" in text
    assert "HTTP 401" in text
    assert "REFRESH_TOKEN_INVALID" in text
