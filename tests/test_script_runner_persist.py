# -*- coding: utf-8 -*-
"""browser_script 运行器的登录态续存回归。

修复的是一个静默失效的 bug：续存以前在脚本里做（_sub2api_common.persist_state），
而脚本拿到的是脱敏的 ScriptSiteView —— 它没有 access_token / browser_state 字段，
token_cache 只能按「空凭据」算 basis，写出的缓存与配置 basis 不一致，下次运行会被
resolve_cached_credentials 判为过期缓存直接忽略。实测极速蹬缓存里的 state_basis
一直是空串的摘要，等于登录态从未续存，每天都要重新走一次 Turnstile 登录。

现在由 runner 用真实 SiteConfig 写，并把 localStorage 里的 auth_token /
refresh_token 一起存下 —— 这是纯 HTTP 路径（不启动浏览器）唯一的凭据来源，
因为该站启用了服务端 Turnstile 校验，纯 HTTP 账密登录必然被拒。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from accounts_store import site_config_from_mapping
from browser import script_runner
from providers import token_cache

STORAGE_STATE = {
    "cookies": [{"name": "cf_clearance", "value": "v", "domain": "t.invalid", "path": "/"}],
    "origins": [{
        "origin": "https://t.invalid",
        "localStorage": [
            {"name": "auth_token", "value": "ACCESS"},
            {"name": "refresh_token", "value": "REFRESH"},
            {"name": "token_expires_at", "value": "1"},
        ],
    }],
}


class FakeContext:
    def __init__(self, state: Any = STORAGE_STATE) -> None:
        self.state = state

    async def storage_state(self) -> Any:
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def _site():
    return site_config_from_mapping({
        "name": "t", "base_url": "https://t.invalid", "site_profile": "sub2api",
        "auth_method": "browser", "checkin_action": "browser_script",
        "access_token": "cfg-token", "refresh_token": "cfg-refresh",
        "browser_state": "cfg-state", "enabled": True,
    })


def _entry(cache_path) -> dict[str, Any]:
    doc = json.loads(cache_path.read_text(encoding="utf-8"))
    return doc["tokens"]["https://t.invalid|t"]


def test_persist_session_writes_tokens_from_local_storage(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    logs: list[str] = []
    asyncio.run(script_runner._persist_session(_site(), FakeContext(), logs.append))
    entry = _entry(cache)
    assert (entry["access_token"], entry["refresh_token"]) == ("ACCESS", "REFRESH")
    assert entry["browser_state"], "storage_state 也要一起存，供下次恢复浏览器登录态"
    assert any("续存登录态" in line for line in logs)


def test_persisted_basis_matches_config_so_cache_is_reusable(tmp_path, monkeypatch) -> None:
    """核心断言：写入的 basis 必须让下次运行真的能用上缓存。"""
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()
    asyncio.run(script_runner._persist_session(site, FakeContext(), lambda _m: None))
    resolved = token_cache.resolve_cached_credentials(
        site.name,
        site.base_url,
        configured_access_token=site.access_token,
        configured_refresh_token=site.refresh_token,
        configured_browser_state=site.browser_state,
        path=cache,
    )
    assert resolved.get("access_token") == "ACCESS"
    assert resolved.get("refresh_token") == "REFRESH"


def test_persist_session_is_silent_when_context_unavailable(tmp_path, monkeypatch) -> None:
    """取不到 storage_state 只意味着下次多开一次浏览器，不该抛异常。"""
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    asyncio.run(script_runner._persist_session(_site(), None, lambda _m: None))
    asyncio.run(script_runner._persist_session(_site(), FakeContext(RuntimeError("closed")), lambda _m: None))
    assert not cache.exists()


def test_missing_tokens_do_not_wipe_previously_cached_ones(tmp_path, monkeypatch) -> None:
    """localStorage 里没有 token 时只续存 state，不能把上次存好的 token 写空。

    save_tokens 只更新非空字段，这条断言是为了防止以后改成「整条覆盖」——
    那会让「脚本这次没登录（token 已被 preflight 清掉）」直接毁掉可用的缓存凭据。
    """
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()
    asyncio.run(script_runner._persist_session(site, FakeContext(), lambda _m: None))
    without_tokens = {"cookies": [], "origins": [{"origin": "https://t.invalid", "localStorage": [
        {"name": "sub2api_site_usage_notice_v1", "value": "accepted"},
    ]}]}
    asyncio.run(script_runner._persist_session(site, FakeContext(without_tokens), lambda _m: None))
    entry = _entry(cache)
    assert (entry["access_token"], entry["refresh_token"]) == ("ACCESS", "REFRESH")
