#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第三方 OAuth 提供商适配器（linux.do / github）。

每个 provider 定义 relogin 签到所需的一切「站点无关」知识：
- 授权端点与授权 URL 构造；
- 站点 /api/status 里 client_id / oauth 开关的字段名；
- 授权页的「同意授权」按钮选择器；
- 第三方登录页特征（用于判定登录态是否失效）。

站点侧固定约定（New API 系）：
- GET {origin}/api/status      → {linuxdo_client_id, github_client_id, linuxdo_oauth, github_oauth, ...}
- GET {origin}/api/oauth/state → {"data": "<state>"}（一次性，每次重新获取）
- 回调 {origin}/api/oauth/{provider}?code=..&state=.. → 站点发额度
- 授权后跳回 {origin}/console/token 或 URL 含 code=
"""

from __future__ import annotations

from abc import ABC
from urllib.parse import urlencode


class OAuthProvider(ABC):
    key: str = ""
    authorize_endpoint: str = ""
    # 授权页「同意授权」按钮候选选择器（按顺序尝试）
    approve_selectors: list[str] = []
    # 第三方登录页特征选择器（出现即说明未登录/登录态失效）
    login_markers: list[str] = []
    # 人工捕获共享登录态时打开的入口页
    capture_url: str = ""
    # 用于判断 storage_state 是否包含该 provider 登录态的域名特征
    state_domain_hints: tuple[str, ...] = ()
    # 授权 URL 附加 scope（github 需要）
    scope: str = ""

    def build_authorize_url(self, client_id: str, state: str) -> str:
        params = {"response_type": "code", "client_id": client_id, "state": state}
        if self.scope:
            params["scope"] = self.scope
        return f"{self.authorize_endpoint}?{urlencode(params)}"

    def status_client_id_field(self) -> str:
        return f"{self.key}_client_id"

    def status_oauth_field(self) -> str:
        return f"{self.key}_oauth"

    def callback_path(self) -> str:
        return f"/api/oauth/{self.key}"

    def matches_url(self, url: str) -> bool:
        """当前 URL 是否处于该 provider 的授权域名下。"""
        raise NotImplementedError


class LinuxDoProvider(OAuthProvider):
    key = "linuxdo"
    authorize_endpoint = "https://connect.linux.do/oauth2/authorize"
    capture_url = "https://linux.do"
    state_domain_hints = ("linux.do", "connect.linux.do")
    approve_selectors = ['a[href^="/oauth2/approve"]', 'button:has-text("允许")', 'button:has-text("Authorize")']
    login_markers = ["#login-account-name", "#login-account-password", "#login-button"]

    def matches_url(self, url: str) -> bool:
        u = (url or "").lower()
        return "connect.linux.do" in u or "linux.do" in u


class GitHubProvider(OAuthProvider):
    key = "github"
    authorize_endpoint = "https://github.com/login/oauth/authorize"
    capture_url = "https://github.com/login"
    state_domain_hints = ("github.com",)
    scope = "user:email"
    approve_selectors = ['button[name="authorize"][value="1"]', 'button[type="submit"]']
    login_markers = ["#login_field", "#password"]

    def matches_url(self, url: str) -> bool:
        return "github.com" in (url or "").lower()


_PROVIDERS: dict[str, OAuthProvider] = {
    "linuxdo": LinuxDoProvider(),
    "github": GitHubProvider(),
}

KNOWN_OAUTH_PROVIDERS = tuple(_PROVIDERS)
DEFAULT_OAUTH_PROVIDER = "linuxdo"


def normalize_oauth_provider(value: str | None) -> str:
    key = (value or "").strip().lower()
    return key if key in _PROVIDERS else DEFAULT_OAUTH_PROVIDER


def get_oauth_provider(value: str | None) -> OAuthProvider:
    return _PROVIDERS[normalize_oauth_provider(value)]


__all__ = [
    "OAuthProvider",
    "LinuxDoProvider",
    "GitHubProvider",
    "KNOWN_OAUTH_PROVIDERS",
    "DEFAULT_OAUTH_PROVIDER",
    "normalize_oauth_provider",
    "get_oauth_provider",
]
