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
    assert calls == [("GET", "/user/profile"), ("POST", "/check-in")]


def test_query_action_fetch_user_and_status_share_one_probe(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        if method == "GET" and path == "/user/profile":
            return {"username": "alice", "balance": 12.5}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _site, _auth: client)

    result = api.query_action(client.site, profile)

    assert result.ok is True
    assert result.quota_usd == 12.5
    assert calls == [("GET", "/user/profile")]


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
