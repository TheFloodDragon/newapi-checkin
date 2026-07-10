#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点适配器（SiteProfile）注册表。

profile 只负责「接口长什么样」：路径 / 请求头 / 响应解析 / 额度换算。
- newapi ：New API 系（/api/user/checkin、/api/user/self，{success,data}，内部 quota）
- sub2api：Sub2API 系（/api/v1/check-in、/api/v1/check-in/status，{code,data}，美元）
"""

from __future__ import annotations

from ..base import SiteProfile
from .newapi import NewApiProfile
from .sub2api import Sub2ApiProfile

_PROFILES: dict[str, SiteProfile] = {
    "newapi": NewApiProfile(),
    "sub2api": Sub2ApiProfile(),
}

KNOWN_PROFILES = set(_PROFILES)
DEFAULT_PROFILE = "newapi"


def normalize_profile(value: str | None) -> str:
    key = (value or DEFAULT_PROFILE).strip().lower()
    return key if key in KNOWN_PROFILES else DEFAULT_PROFILE


def get_profile(value: str | None) -> SiteProfile:
    return _PROFILES[normalize_profile(value)]


__all__ = [
    "NewApiProfile",
    "Sub2ApiProfile",
    "KNOWN_PROFILES",
    "DEFAULT_PROFILE",
    "normalize_profile",
    "get_profile",
]
