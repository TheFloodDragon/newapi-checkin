# -*- coding: utf-8 -*-
"""storage_state 取 token 必须限定 origin（回归）。

storage_state 是整个浏览器上下文的快照，可能同时含共享 OAuth 站点、上一个站点的
残留 origin。而 auth_token / refresh_token 在各 Sub2API fork 里是同一套键名，
「取第一个同名键」会把 A 站的长期凭据当成 B 站的存进缓存，甚至发给 B 站。
"""

from __future__ import annotations

import accounts_store
from browser import session


def _multi_origin_state() -> dict:
    """A 站在前、B 站在后：不限定来源时旧实现总是返回 A 的值。"""
    return {
        "cookies": [],
        "origins": [
            {
                "origin": "https://site-a.invalid",
                "localStorage": [
                    {"name": "auth_token", "value": "TOKEN_FROM_A"},
                    {"name": "refresh_token", "value": "RT_FROM_A"},
                ],
            },
            {
                "origin": "https://site-b.invalid",
                "localStorage": [
                    {"name": "auth_token", "value": "TOKEN_FROM_B"},
                    {"name": "refresh_token", "value": "RT_FROM_B"},
                ],
            },
        ],
    }


def test_token_is_read_from_requested_origin_only() -> None:
    state = _multi_origin_state()
    assert session.storage_access_token(state, base_url="https://site-b.invalid") == "TOKEN_FROM_B"
    assert session.storage_refresh_token(state, base_url="https://site-b.invalid") == "RT_FROM_B"
    # 反向同样成立：不能因为 A 排在前面就总拿 A
    assert session.storage_access_token(state, base_url="https://site-a.invalid") == "TOKEN_FROM_A"


def test_missing_origin_returns_empty_instead_of_other_site_token() -> None:
    """宁可退化成重新登录，也不能拿错身份。"""
    state = _multi_origin_state()
    assert session.storage_access_token(state, base_url="https://site-c.invalid") == ""
    assert session.storage_refresh_token(state, base_url="https://site-c.invalid") == ""


def test_origin_match_is_strict_on_scheme_host_and_port() -> None:
    state = {
        "origins": [
            {"origin": "https://site.invalid", "localStorage": [{"name": "auth_token", "value": "HTTPS_443"}]},
        ]
    }
    assert session.storage_access_token(state, base_url="https://site.invalid") == "HTTPS_443"
    assert session.storage_access_token(state, base_url="https://site.invalid/dashboard") == "HTTPS_443"
    # 显式默认端口等价于省略
    assert session.storage_access_token(state, base_url="https://site.invalid:443") == "HTTPS_443"
    # scheme / 端口 / 子域都不得放过
    assert session.storage_access_token(state, base_url="http://site.invalid") == ""
    assert session.storage_access_token(state, base_url="https://site.invalid:8443") == ""
    assert session.storage_access_token(state, base_url="https://evil-site.invalid") == ""
    assert session.storage_access_token(state, base_url="https://sub.site.invalid") == ""


def test_no_base_url_keeps_legacy_first_match_behavior() -> None:
    """存在性诊断等场景不关心来源，旧行为保持不变。"""
    state = _multi_origin_state()
    assert session.storage_access_token(state) == "TOKEN_FROM_A"
    assert session.storage_item(state, "refresh_token") == "RT_FROM_A"


def test_welfare_token_is_not_a_generic_cache_key() -> None:
    state = {
        "origins": [
            {
                "origin": "https://api-welfalre.fengwind.com",
                "localStorage": [{"name": "welfare_token", "value": "SITE_ONLY_TOKEN"}],
            },
            {
                "origin": "https://api.fengwind.com",
                "localStorage": [{"name": "auth_token", "value": "GENERIC_TOKEN"}],
            },
        ]
    }

    assert session.storage_access_token(state, base_url="https://api-welfalre.fengwind.com") == ""
    assert session.storage_access_token(state, base_url="https://api.fengwind.com") == "GENERIC_TOKEN"


def test_refresh_token_backfill_ignores_other_origins() -> None:
    """存量登录态回填不得把别站 refresh_token 写进本站配置。"""
    from browser.state import encode_state

    encoded = encode_state(_multi_origin_state())
    entry = accounts_store._normalize_account_entry(
        {
            "name": "B 站",
            "base_url": "https://site-b.invalid",
            "site_profile": "sub2api",
            "auth_method": "browser",
            "browser_state": encoded,
        }
    )
    assert entry["refresh_token"] == "RT_FROM_B"

    unrelated = accounts_store._normalize_account_entry(
        {
            "name": "C 站",
            "base_url": "https://site-c.invalid",
            "site_profile": "sub2api",
            "auth_method": "browser",
            "browser_state": encoded,
        }
    )
    assert unrelated.get("refresh_token", "") == ""
