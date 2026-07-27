from __future__ import annotations

from providers.base import AuthInfo, SiteConfig
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
