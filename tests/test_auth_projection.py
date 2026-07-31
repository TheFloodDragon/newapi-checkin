# -*- coding: utf-8 -*-
"""auth_method 必须真正隔离凭据。

回归背景：load_auth() 同时返回 cookie 与 access_token，而 profile 客户端各有
优先级——newapi 见到 token 就发 Bearer 并剥掉 session cookie，sub2api 两个一起
发。于是用户在 GUI 明确选择 Cookie 登录，实际请求头却带 Authorization（已实测）。
同一份配置里的两套凭据可能属于不同账号，混用等于签到到别的身份上。

所有 token/cookie 均为虚构值。
"""

from __future__ import annotations

from typing import Any

import pytest

from providers import auth as auth_module
from providers.base import AuthInfo, SiteConfig
from providers.profiles import newapi as newapi_module
from providers.profiles import sub2api as sub2api_module

FAKE_TOKEN = "FAKE_TOKEN_VALUE"
FAKE_SESSION = "session=FAKE_SESSION_VALUE"
FAKE_WAF_COOKIE = "cf_clearance=FAKE_WAF_VALUE"


def _site(auth_method: str, *, profile: str = "newapi") -> SiteConfig:
    return SiteConfig(
        name="probe",
        base_url="https://probe.invalid",
        site_profile=profile,
        auth_method=auth_method,
        checkin_action="api",
        cookie=f"{FAKE_SESSION}; {FAKE_WAF_COOKIE}",
        access_token=FAKE_TOKEN,
        user_id="1001",
    )


def _captured_headers(monkeypatch: pytest.MonkeyPatch, module: Any, site: SiteConfig) -> dict[str, str]:
    """构造 profile 客户端并抓取它实际发出的请求头。"""
    captured: dict[str, Any] = {}

    def fake_request(url: str, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"success": True, "code": 0, "data": {}}

    monkeypatch.setattr(module, "http_request", fake_request)
    auth = auth_module.auth_for_method(site)
    client = module_client(module, site, auth)
    client.request("GET", "/probe")
    return dict(captured.get("headers") or {})


def module_client(module: Any, site: SiteConfig, auth: AuthInfo) -> Any:
    if module is newapi_module:
        return module.NewApiClient(site, auth)
    return module.Sub2ApiClient(site, auth)


# ── 投影语义 ────────────────────────────────────────────────────────────────
def test_cookie_method_drops_access_token() -> None:
    auth = auth_module.auth_for_method(_site("cookie"))
    assert auth.access_token == ""
    assert "FAKE_SESSION_VALUE" in auth.cookie
    assert auth.new_api_user == "1001"


def test_access_token_method_drops_session_cookie_but_keeps_waf_cookie() -> None:
    auth = auth_module.auth_for_method(_site("access_token"))
    assert auth.access_token == FAKE_TOKEN
    # session 是第二身份，必须剥离；cf_clearance 只是过 WAF 的辅助 cookie，要保留。
    assert "FAKE_SESSION_VALUE" not in auth.cookie
    assert "FAKE_WAF_VALUE" in auth.cookie


@pytest.mark.parametrize("auth_method", ["browser", "oauth"])
def test_browser_and_oauth_methods_are_untouched(auth_method: str) -> None:
    """这两种方式的凭据由 action 层运行期注入，投影不得干预。"""
    auth = auth_module.auth_for_method(_site(auth_method))
    raw = auth_module.load_auth(_site(auth_method))
    assert auth.access_token == raw.access_token
    assert auth.cookie == raw.cookie


# ── 实际请求头（两个 profile 都要验证）──────────────────────────────────────
def test_newapi_cookie_method_sends_no_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _captured_headers(monkeypatch, newapi_module, _site("cookie"))
    assert "Authorization" not in headers
    assert "FAKE_SESSION_VALUE" in headers["Cookie"]
    assert FAKE_TOKEN not in str(headers)


def test_newapi_access_token_method_sends_no_session_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _captured_headers(monkeypatch, newapi_module, _site("access_token"))
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "FAKE_SESSION_VALUE" not in headers.get("Cookie", "")


def test_sub2api_keeps_token_as_interface_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub2API 的 token 按设计是「接口凭据」，不是 cookie 的替身。

    README 明确：Sub2API 的 Access/Refresh Token 即使认证方式选 browser/oauth 也
    仍然生效（签到链路始终先试纯 API）。因此这里不要求剥离 token；要保证的是
    cookie 不被顶掉——session cookie 一旦被剥离，绑定网络指纹的 fork 会直接掉线。
    """
    headers = _captured_headers(monkeypatch, sub2api_module, _site("cookie", profile="sub2api"))
    assert "FAKE_SESSION_VALUE" in headers.get("Cookie", "")


def test_sub2api_access_token_method_keeps_waf_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = _captured_headers(monkeypatch, sub2api_module, _site("access_token", profile="sub2api"))
    assert headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    assert "FAKE_WAF_VALUE" in headers.get("Cookie", "")


# ── credentials_ready 必须与投影一致 ────────────────────────────────────────
def test_credentials_ready_follows_projection() -> None:
    from providers.actions import _common
    from providers.profiles import get_profile

    profile = get_profile("newapi")

    cookie_only = SiteConfig(
        name="p", base_url="https://p.invalid", auth_method="access_token", cookie=FAKE_SESSION
    )
    # 只有 cookie 却声明 access_token：投影后没有 token，必须判为未就绪。
    assert _common.credentials_ready(cookie_only, profile) is False

    token_only = SiteConfig(
        name="p", base_url="https://p.invalid", auth_method="cookie", access_token=FAKE_TOKEN
    )
    assert _common.credentials_ready(token_only, profile) is False
