from __future__ import annotations

import pytest

from providers.actions import api
from providers.base import ApiError, AuthInfo, SiteConfig
from providers.profiles import sub2api


def _client() -> sub2api.Sub2ApiClient:
    site = SiteConfig(
        name="sub2api-test",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="access_token",
        access_token="token",
    )
    return sub2api.Sub2ApiClient(site, AuthInfo(access_token="token"))


def test_expired_cached_token_refreshes_once_and_retries(monkeypatch) -> None:
    site = SiteConfig(
        name="sub2api-oauth",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="oauth",
        access_token="old-token",
        browser_state="saved-state",
    )
    refresh_calls = 0
    auth_headers: list[str] = []

    def refresher() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "fresh-token"

    client = sub2api.Sub2ApiClient(
        site,
        AuthInfo(access_token="old-token"),
        token_refresher=refresher,
    )

    def fake_http_request(url, *, method, headers, body, proxy, retry_non_idempotent, verify_ssl):
        auth_headers.append(headers.get("Authorization", ""))
        if len(auth_headers) == 1:
            raise ApiError(401, {"code": "TOKEN_EXPIRED"}, "Token has expired")
        return {"code": 0, "data": {"balance": 9}}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    payload = client.request("GET", "/user/profile")

    assert payload == {"code": 0, "data": {"balance": 9}}
    assert refresh_calls == 1
    assert auth_headers == ["Bearer old-token", "Bearer fresh-token"]


def test_fetch_status_reads_checkin_extension(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "request",
        lambda method, path, body=None, *, retry_non_idempotent=False: {
            "checked_in_today": True,
            "balance": 22.5,
            "current_streak": 4,
        },
    )

    status = client.fetch_status()

    assert status.checked_in_today is True
    assert status.quota_usd == 22.5
    assert status.raw["source"] == "/check-in/status"


def test_usage_zero_balance_does_not_fall_back(monkeypatch) -> None:
    client = _client()
    usage_payload = {
        "items": [
            {
                "user": {"balance": 0},
                "api_key": {"quota": 99},
            }
        ]
    }

    def fake_standard_balance(data):
        return 99 if data is usage_payload else None

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        assert method == "GET"
        if path in {"/user/profile", "/auth/me"}:
            return {}
        if path.startswith("/usage?"):
            return usage_payload
        raise AssertionError(f"不应访问 {path}")

    monkeypatch.setattr(sub2api, "_extract_standard_balance", fake_standard_balance)
    monkeypatch.setattr(client, "request", fake_request)

    assert client.fetch_user().quota_raw == 0


def test_status_then_unsupported_checkin_reuses_user_probe(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        # 标准 Sub2API（无签到扩展）：/check-in/status 返回 404，fetch_status 降级到用户资料。
        if method == "GET" and path == "/check-in/status":
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "GET" and path == "/user/profile":
            return {"username": "alice", "balance": 7}
        if method == "POST" and path == "/check-in":
            raise ApiError(404, {"message": "not found"}, "not found")
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    status = client.fetch_status()
    reward = client.do_checkin()

    assert status.quota_usd == 7
    assert reward.current_quota == 7
    assert reward.extra["unsupported_checkin"] is True
    assert calls == [
        ("GET", "/check-in/status"),
        ("GET", "/user/profile"),
        ("POST", "/check-in"),
    ]


def test_query_action_fetch_user_and_status_share_one_probe(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        # 标准 Sub2API：无签到扩展接口，/check-in/status 返回 404 触发降级。
        if method == "GET" and path == "/check-in/status":
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "GET" and path == "/user/profile":
            return {"username": "alice", "balance": 12.5}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _site, _auth: client)

    result = api.query_action(client.site, profile)

    assert result.ok is True
    assert result.quota_usd == 12.5
    # fetch_user 探测 /user/profile 并缓存；fetch_status 先试 /check-in/status（404）再复用缓存。
    assert calls == [("GET", "/user/profile"), ("GET", "/check-in/status")]


def test_successful_real_checkin_invalidates_user_cache(monkeypatch) -> None:
    client = _client()
    profile_requests = 0

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        nonlocal profile_requests
        if method == "GET" and path == "/user/profile":
            profile_requests += 1
            return {"balance": 10 if profile_requests == 1 else 20}
        if method == "POST" and path == "/check-in":
            return {"reward_amount": 1}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    assert client.fetch_user().quota_raw == 10
    client.do_checkin()
    assert client.fetch_user().quota_raw == 20
    assert profile_requests == 2


def test_failed_first_user_query_does_not_poison_cache(monkeypatch) -> None:
    client = _client()
    attempts = 0

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        nonlocal attempts
        assert (method, path) == ("GET", "/user/profile")
        attempts += 1
        if attempts == 1:
            raise ApiError(503, None, "temporary", transient=True)
        return {"balance": 3}

    monkeypatch.setattr(client, "request", fake_request)

    with pytest.raises(ApiError, match="temporary"):
        client.fetch_user()
    assert client.fetch_user().quota_raw == 3
    assert client.fetch_user().quota_raw == 3
    assert attempts == 2


def test_authenticated_unknown_balance_is_cached(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        return {}

    def fake_usage():
        calls.append(("GET", "/v1/usage"))
        return {}

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(client, "request_usage", fake_usage)

    first = client.fetch_user()
    first_call_count = len(calls)
    second = client.fetch_user()

    assert first.quota_raw is None
    assert second is first
    assert len(calls) == first_call_count
