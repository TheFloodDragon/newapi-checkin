from __future__ import annotations

import pytest

from providers.actions import api
from providers.base import (
    ApiError,
    AuthInfo,
    BrowserAuthError,
    CheckinReward,
    SiteConfig,
    StatusInfo,
    UserInfo,
)


class FakeClient:
    base_url = "https://action.invalid"
    quota_is_usd = True

    def __init__(self, *, login_error_on: str = "") -> None:
        self.login_error_on = login_error_on
        self.status_calls = 0
        self.user_calls = 0
        self.checkin_calls = 0

    def fetch_status(self) -> StatusInfo:
        self.status_calls += 1
        if self.login_error_on == "status":
            raise ApiError(401, {"where": "status"}, "unauthorized")
        return StatusInfo(checked_in_today=False, quota_usd=5)

    def fetch_user(self) -> UserInfo:
        self.user_calls += 1
        if self.login_error_on == "user":
            raise ApiError(401, {"where": "user"}, "unauthorized")
        return UserInfo(quota_raw=5)

    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        self.checkin_calls += 1
        return CheckinReward(current_quota=5)

    def classify(self, error: ApiError) -> str:
        return "need_login" if error.status == 401 else "error"

    def quota_to_usd(self, value):
        return float(value) if value is not None else None


class FakeProfile:
    def __init__(
        self,
        client: FakeClient,
        *,
        refresh_result: AuthInfo | None = None,
        refresh_error: BrowserAuthError | None = None,
        lazy_result: FakeClient | None = None,
    ) -> None:
        self.client = client
        self.refresh_result = refresh_result
        self.refresh_error = refresh_error
        self.lazy_result = lazy_result
        self.refresh_calls = 0
        self.build_calls = 0
        self.lazy_calls = 0

    def build_lazy_refresh_client(self, site: SiteConfig) -> FakeClient | None:
        self.lazy_calls += 1
        return self.lazy_result

    def supports_browser_refresh(self) -> bool:
        return True

    def refresh_auth_via_browser(self, site: SiteConfig) -> AuthInfo | None:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error
        return self.refresh_result

    def build_client(self, site: SiteConfig, auth: AuthInfo) -> FakeClient:
        self.build_calls += 1
        return self.client


def _site(auth_method: str) -> SiteConfig:
    kwargs = {
        "name": f"action-{auth_method}",
        "base_url": "https://action.invalid",
        "auth_method": auth_method,
    }
    if auth_method == "access_token":
        kwargs["access_token"] = "token"
    elif auth_method == "cookie":
        kwargs["cookie"] = "session=secret"
    else:
        kwargs["browser_state"] = "saved-state"
    return SiteConfig(**kwargs)


@pytest.mark.parametrize("auth_method", ["browser", "oauth"])
def test_run_action_refreshes_browser_auth_only_once(auth_method: str) -> None:
    client = FakeClient(login_error_on="status")
    profile = FakeProfile(client, refresh_result=AuthInfo(cookie="fresh=1"))

    result = api.run_action(_site(auth_method), profile)

    assert result.status == "need_login"
    assert profile.refresh_calls == 1
    assert profile.build_calls == 1
    assert client.status_calls == 1


@pytest.mark.parametrize("auth_method", ["browser", "oauth"])
def test_query_action_refreshes_browser_auth_only_once(auth_method: str) -> None:
    client = FakeClient(login_error_on="user")
    profile = FakeProfile(client, refresh_result=AuthInfo(cookie="fresh=1"))

    result = api.query_action(_site(auth_method), profile)

    assert result.status == "need_login"
    assert profile.refresh_calls == 1
    assert profile.build_calls == 1
    assert client.user_calls == 1
    assert client.status_calls == 0


@pytest.mark.parametrize("query", [False, True])
def test_refresh_none_returns_need_login_without_empty_http_client(query: bool) -> None:
    client = FakeClient()
    profile = FakeProfile(client, refresh_result=None)
    site = _site("browser")

    result = api.query_action(site, profile) if query else api.run_action(site, profile)

    assert result.status == "need_login"
    assert profile.refresh_calls == 1
    assert profile.build_calls == 0
    assert client.user_calls == 0
    assert client.status_calls == 0
    assert client.checkin_calls == 0


@pytest.mark.parametrize("auth_method", ["access_token", "cookie"])
def test_http_credentials_never_trigger_browser_refresh(auth_method: str) -> None:
    client = FakeClient()
    profile = FakeProfile(client, refresh_result=AuthInfo(cookie="unused=1"))

    result = api.query_action(_site(auth_method), profile)

    assert result.ok is True
    assert profile.refresh_calls == 0
    assert profile.build_calls == 1
    assert client.user_calls == 1
    assert client.status_calls == 1


def test_access_token_expiry_reports_error_without_oauth_refresh() -> None:
    client = FakeClient(login_error_on="user")
    profile = FakeProfile(client, refresh_result=AuthInfo(access_token="should-not-be-used"))

    result = api.query_action(_site("access_token"), profile)

    assert result.status == "need_login"
    assert "重新导出凭据" in result.message
    assert profile.lazy_calls == 0
    assert profile.refresh_calls == 0
    assert profile.build_calls == 1
    assert client.user_calls == 1


def test_optional_oauth_selection_enables_lazy_fallback_client() -> None:
    client = FakeClient()
    profile = FakeProfile(client, lazy_result=client)
    site = _site("access_token")
    site.oauth_fallback_provider = "linuxdo"
    site.oauth_fallback_account = "default"

    result = api.query_action(site, profile)

    assert result.ok is True
    assert profile.lazy_calls == 1
    assert profile.refresh_calls == 0
    assert profile.build_calls == 0
    assert client.user_calls == 1


@pytest.mark.parametrize("query", [False, True])
def test_browser_auth_error_status_and_detail_are_preserved(query: bool) -> None:
    detail = {"waf_blocked": True, "reason": "challenge"}
    error = BrowserAuthError("need_verification", "需要验证", detail=detail)
    client = FakeClient()
    profile = FakeProfile(client, refresh_error=error)
    site = _site("browser")

    result = api.query_action(site, profile) if query else api.run_action(site, profile)

    assert result.status == "need_verification"
    assert result.detail is detail
    assert profile.refresh_calls == 1
    assert profile.build_calls == 0
