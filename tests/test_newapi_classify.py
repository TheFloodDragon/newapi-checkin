from __future__ import annotations

import pytest

from providers.base import ApiError, AuthInfo, SiteConfig
from providers.profiles.newapi import NewApiClient


def _client() -> NewApiClient:
    site = SiteConfig(name="t", base_url="https://newapi.invalid")
    return NewApiClient(site, AuthInfo(access_token="tok", new_api_user="1"))


def test_turnstile_empty_message_is_need_verification() -> None:
    # 服务端要求人机验证时返回「Turnstile token 为空」；message 含宽泛的 "token"，
    # 不能被 LOGIN_PATTERNS 误判为 need_login，应归为 need_verification。
    from providers.base import ApiError

    error = ApiError(None, {"message": "Turnstile token 为空", "success": False}, "Turnstile token 为空")
    assert _client().classify(error) == "need_verification"


def test_http_401_is_need_login() -> None:
    from providers.base import ApiError

    error = ApiError(401, {"message": "unauthorized"}, "unauthorized")
    assert _client().classify(error) == "need_login"


def test_login_message_without_verification_is_need_login() -> None:
    from providers.base import ApiError

    error = ApiError(None, {"message": "未登录"}, "未登录")
    assert _client().classify(error) == "need_login"


def test_already_done_message() -> None:
    from providers.base import ApiError

    error = ApiError(None, {"message": "今日已签到"}, "今日已签到")
    assert _client().classify(error) == "already_done"


def test_auto_challenge_waf_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node challenge 被 CF 单独拦截时，auto 模式仍应尝试 legacy API。"""
    client = _client()
    legacy_result = {"quota_awarded": 250000}

    def blocked_challenge():
        raise ApiError(
            None,
            "<!DOCTYPE html><title>Just a moment...</title>",
            "接口返回非 JSON：<!DOCTYPE html><title>Just a moment...</title>",
        )

    legacy_calls: list[str] = []
    monkeypatch.setattr(client, "_challenge_checkin", blocked_challenge)
    monkeypatch.setattr(
        client,
        "_legacy_checkin",
        lambda turnstile="": legacy_calls.append(turnstile) or legacy_result,
    )

    assert client._challenge_with_fallback("token") == legacy_result
    assert legacy_calls == ["token"]


def test_legacy_checkin_sends_daily_code_in_body_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带口令时按 {"code": ...} 提交；不带时保持原来的空对象体。"""
    sent: list[dict] = []

    def fake_request(url: str, **kwargs: object) -> object:
        sent.append({"url": url, "body": kwargs.get("body")})
        return {"success": True, "data": {"quota_awarded": 1}}

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        SiteConfig(name="code-site", base_url="https://code.invalid"),
        AuthInfo(access_token="tok"),
    )

    client._legacy_checkin(code="TODAY42")
    client._legacy_checkin()

    assert sent[0]["body"] == b'{"code": "TODAY42"}'
    assert sent[1]["body"] == b"{}"
    assert all(item["url"].endswith("/api/user/checkin") for item in sent)


def test_cloudflare_response_refreshes_cookie_once_and_retries_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """access_token 仍是身份凭据，过期 cf_clearance 只触发一次浏览器辅助刷新。"""
    site = SiteConfig(
        name="waf-site",
        base_url="https://waf.invalid",
        cookie="session=old; cf_clearance=stale",
    )
    calls: list[dict[str, object]] = []
    refresh_calls = 0

    def fake_request(url: str, **kwargs: object) -> object:
        nonlocal refresh_calls
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            raise ApiError(
                403,
                "<!DOCTYPE html><title>Just a moment...</title>",
                "接口返回非 JSON：<!DOCTYPE html><title>Just a moment...</title>",
            )
        return {"success": True, "quota": 12}

    def refresh(_auth: AuthInfo) -> AuthInfo:
        nonlocal refresh_calls
        refresh_calls += 1
        return AuthInfo(
            cookie="session=old; cf_clearance=fresh",
            access_token="token-keep",
            new_api_user="7",
        )

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        site,
        AuthInfo(cookie="session=old; cf_clearance=stale", access_token="token-keep"),
        cookie_refresher=refresh,
    )

    assert client.request("GET", "/api/user/self") == {"success": True, "quota": 12}
    assert refresh_calls == 1
    assert len(calls) == 2
    assert "cf_clearance=fresh" in str(calls[1]["headers"])
    assert "Bearer token-keep" == calls[1]["headers"]["Authorization"]
    assert client.auth.access_token == "token-keep"
    assert client.auth.cookie.endswith("cf_clearance=fresh")


def test_cloudflare_refresh_is_not_repeated_after_failed_browser_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = SiteConfig(name="waf-site", base_url="https://waf.invalid")
    calls = 0
    refresh_calls = 0

    def fake_request(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise ApiError(403, "<title>Just a moment...</title>", "Just a moment")

    def refresh(_auth: AuthInfo) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        return None

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        site,
        AuthInfo(access_token="token", cookie="cf_clearance=stale"),
        cookie_refresher=refresh,
    )

    with pytest.raises(ApiError):
        client.request("GET", "/api/user/self")
    assert calls == 1
    assert refresh_calls == 1
