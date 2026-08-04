"""认证维度的共享规范化与能力契约。"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .enums import AuthMethod, CheckinAction, SiteProfileName, parse_enum


def effective_auth(checkin_action: object, auth_method: object) -> str:
    """应用 action 对认证方式的硬约束，返回稳定字符串值。"""
    action = parse_enum(CheckinAction, checkin_action, CheckinAction.API)
    auth = parse_enum(AuthMethod, auth_method, AuthMethod.COOKIE)
    if action is CheckinAction.RELOGIN:
        return AuthMethod.OAUTH.value
    if action is CheckinAction.BROWSER_SCRIPT and auth not in {AuthMethod.BROWSER, AuthMethod.OAUTH}:
        return AuthMethod.OAUTH.value
    return auth.value


def infer_auth_method(
    checkin_action: object,
    *,
    has_token: bool = False,
    sub2api_browser: bool = False,
) -> str:
    action = parse_enum(CheckinAction, checkin_action, CheckinAction.API)
    if action in {CheckinAction.RELOGIN, CheckinAction.BROWSER_SCRIPT}:
        return AuthMethod.OAUTH.value
    if sub2api_browser:
        return AuthMethod.BROWSER.value
    return AuthMethod.ACCESS_TOKEN.value if has_token else AuthMethod.COOKIE.value


def can_optional_oauth(site_profile: object, checkin_action: object, auth_method: object) -> bool:
    profile = parse_enum(SiteProfileName, site_profile, SiteProfileName.NEWAPI)
    action = parse_enum(CheckinAction, checkin_action, CheckinAction.API)
    auth = parse_enum(AuthMethod, auth_method, AuthMethod.COOKIE)
    return (
        profile is SiteProfileName.SUB2API
        and action is CheckinAction.API
        and auth is AuthMethod.ACCESS_TOKEN
    ) or (action is CheckinAction.BROWSER_SCRIPT and auth is AuthMethod.BROWSER)


@runtime_checkable
class TokenRefreshCapable(Protocol):
    def refresh_token_via_http(self, site: Any, log: Any = None) -> dict[str, str] | None: ...


@runtime_checkable
class PasswordLoginCapable(Protocol):
    def http_password_login(
        self,
        site: Any,
        email: str,
        password: str,
        log: Any = None,
    ) -> dict[str, str] | None: ...
