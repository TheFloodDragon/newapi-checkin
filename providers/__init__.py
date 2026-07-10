#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到 provider 统一入口：三个正交维度的组装器。

正交三维：
- site_profile  ：站点适配器（接口路径/请求头/响应/额度换算），newapi / sub2api；
- auth_method   ：登录方式（如何获得已认证会话），access_token / cookie / browser / oauth；
- checkin_action：签到方式（如何触发发额度），api / relogin / visit。

run_checkin 流程：
1. 按 site_profile 选 Profile（接口适配器）；
2. 按 checkin_action 执行动作（api/relogin/visit）；动作内部按 auth_method 准备认证，
   并用 Profile 解析接口结果。

有意义的组合：
- access_token/cookie + api  ：普通 HTTP 签到（newapi challenge/legacy、sub2api token）
- access_token/cookie + visit：保活监控（login_grant）
- oauth + relogin            ：OAuth 重登发额度（AgentRouter）
- browser + api              ：站点级浏览器登录态刷新 token 后调接口（sub2api browser）
- oauth + api                ：共享 OAuth 账号登录态刷新 token 后调接口（如 Sub2API）
"""

from __future__ import annotations

from . import actions
from .base import CheckinResult, QueryStatus, SiteConfig
from .profiles import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    get_profile,
    normalize_profile,
)

# 登录方式 / 签到方式的合法值（供 GUI / CLI / 配置层共享）
AUTH_METHODS = ("access_token", "cookie", "browser", "oauth")
DEFAULT_AUTH_METHOD = "cookie"
CHECKIN_ACTIONS = actions.KNOWN_ACTIONS
DEFAULT_CHECKIN_ACTION = actions.DEFAULT_ACTION


def normalize_auth_method(value: str | None) -> str:
    key = (value or DEFAULT_AUTH_METHOD).strip().lower()
    return key if key in AUTH_METHODS else DEFAULT_AUTH_METHOD


def normalize_action(value: str | None) -> str:
    return actions.normalize_action(value)


def run_checkin(site: SiteConfig, turnstile: str = "") -> CheckinResult:
    """统一入口：profile × auth × action 组装执行。"""
    profile = get_profile(site.site_profile)
    return actions.run_action(site, profile, turnstile)


def query_status(site: SiteConfig) -> QueryStatus:
    """只读查询站点额度 + 签到状态（不执行签到）。"""
    profile = get_profile(site.site_profile)
    return actions.query_action(site, profile)


__all__ = [
    "CheckinResult",
    "QueryStatus",
    "SiteConfig",
    "KNOWN_PROFILES",
    "DEFAULT_PROFILE",
    "AUTH_METHODS",
    "DEFAULT_AUTH_METHOD",
    "CHECKIN_ACTIONS",
    "DEFAULT_CHECKIN_ACTION",
    "normalize_profile",
    "normalize_auth_method",
    "normalize_action",
    "run_checkin",
    "query_status",
]
