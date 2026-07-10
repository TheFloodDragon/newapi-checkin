#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到方式注册表（checkin_action）：如何触发发额度。

- api    ：调站点签到接口（newapi 的 /api/user/checkin、sub2api 的 /api/v1/check-in）；
- relogin：浏览器重放 OAuth 登录触发发放（AgentRouter 类站点）；
- visit  ：访问 /api/user/self 保活监控额度（login_grant 类站点）。

每个 action 暴露：
- run_action(site, profile, turnstile) -> CheckinResult
- query_action(site, profile) -> QueryStatus
"""

from __future__ import annotations

from typing import Callable

from ..base import CheckinResult, QueryStatus, SiteConfig, SiteProfile
from . import api, browser_script, relogin, visit

_RUN: dict[str, Callable[[SiteConfig, SiteProfile, str], CheckinResult]] = {
    "api": api.run_action,
    "browser_script": browser_script.run_action,
    "relogin": relogin.run_action,
    "visit": visit.run_action,
}
_QUERY: dict[str, Callable[[SiteConfig, SiteProfile], QueryStatus]] = {
    "api": api.query_action,
    "browser_script": browser_script.query_action,
    "relogin": relogin.query_action,
    "visit": visit.query_action,
}

KNOWN_ACTIONS = set(_RUN)
DEFAULT_ACTION = "api"


def normalize_action(value: str | None) -> str:
    key = (value or DEFAULT_ACTION).strip().lower()
    return key if key in KNOWN_ACTIONS else DEFAULT_ACTION


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    return _RUN[normalize_action(site.checkin_action)](site, profile, turnstile)


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    return _QUERY[normalize_action(site.checkin_action)](site, profile)


__all__ = [
    "KNOWN_ACTIONS",
    "DEFAULT_ACTION",
    "normalize_action",
    "run_action",
    "query_action",
]
