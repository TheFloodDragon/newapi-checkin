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


# ── 未登录时不得续存（登出态会覆盖上次可用的登录态）──────────────────────────
LOGGED_OUT_STATE = {
    # 登出态：preflight / 登录失败后 localStorage 里的 auth 键已被清空，
    # 只剩「使用说明已读」这类无关标记。cookies 也只有 WAF 通行证。
    "cookies": [{"name": "cf_clearance", "value": "v2", "domain": "t.invalid", "path": "/"}],
    "origins": [{
        "origin": "https://t.invalid",
        "localStorage": [{"name": "sub2api_site_usage_notice_v1", "value": "accepted"}],
    }],
}


def test_unauthenticated_outcome_keeps_previous_cached_state(tmp_path, monkeypatch) -> None:
    """Turnstile 没过 / 账密被拒时，不能把登出态的 browser_state 写进缓存。

    这是实测到的丢登录态路径：_persist_session 在 finally 里无条件执行，而
    save_site_tokens 只跳过空字段——token 确实不会被写空，但 browser_state
    每次都非空，于是一次失败的登录就把上次还能用的登录态覆盖成登出态，
    下次运行只能从零再登一次（表现为「登录失败之后就再也复用不上缓存」）。
    """
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()

    # 第一次：登录成功，存下可用登录态。
    asyncio.run(script_runner._persist_session(site, FakeContext(), lambda _m: None))
    good_state = _entry(cache)["browser_state"]
    assert good_state

    # 第二次：Turnstile 未通过，脚本返回 need_verification。
    logs: list[str] = []
    asyncio.run(
        script_runner._persist_session(
            site, FakeContext(LOGGED_OUT_STATE), logs.append, status="need_verification"
        )
    )
    assert _entry(cache)["browser_state"] == good_state, "登出态不得覆盖上次可用的登录态"
    assert any("跳过续存" in line for line in logs), "跳过续存必须留下日志，否则无法解释缓存为何没更新"


def test_all_unauthenticated_statuses_are_gated(tmp_path, monkeypatch) -> None:
    """need_login / need_verification / need_config 都代表「这次没登录成功」。"""
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()
    for status in ("need_login", "need_verification", "need_config"):
        asyncio.run(
            script_runner._persist_session(site, FakeContext(), lambda _m: None, status=status)
        )
        assert not cache.exists(), f"{status} 不该写缓存"


def test_script_error_still_persists_because_login_may_have_succeeded(tmp_path, monkeypatch) -> None:
    """error / 超时仍要续存：登录很可能已经成功，只是签到那步失败了。

    这份快照能让下次运行省掉一次 Turnstile 登录，直接从已登录状态继续。
    """
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()
    asyncio.run(script_runner._persist_session(site, FakeContext(), lambda _m: None, status="error"))
    assert _entry(cache)["access_token"] == "ACCESS"


def test_success_statuses_persist_as_before(tmp_path, monkeypatch) -> None:
    """成功路径行为不变（含不带 status 的旧调用）。"""
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    site = _site()
    for status in ("success", "already_done", ""):
        cache.unlink(missing_ok=True)
        asyncio.run(
            script_runner._persist_session(site, FakeContext(), lambda _m: None, status=status)
        )
        assert _entry(cache)["access_token"] == "ACCESS", f"status={status!r} 应正常续存"
