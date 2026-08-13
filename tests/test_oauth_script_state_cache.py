# -*- coding: utf-8 -*-
"""OAuth + browser_script 站点的运行期登录态缓存往返。

回归背景：script_handles_oauth 站点（如 ABR 福利站）的 browser_state 完全是运行期
产物——父进程注入共享 provider 登录态，脚本结束后又把「本站会话 + provider 态」的
新快照写回缓存。此前 state basis 按「本次注入值」计算，于是每轮 basis 都不同，
下一轮 resolve_cached_credentials 判为过期缓存直接忽略：日志显示「已续存登录态」，
但每次运行仍要重跑整段 OAuth（含 Cloudflare 挑战）。
"""

from __future__ import annotations

from typing import Any

import accounts_store
import checkin
import run__all_checkin as runner
from providers import token_cache


def _oauth_script_site(**overrides: Any) -> dict[str, Any]:
    site = {
        "name": "OAuth 脚本站",
        "base_url": "https://oauth-script.invalid",
        "site_profile": "newapi",
        "auth_method": "oauth",
        "checkin_action": "browser_script",
        "oauth_provider": "linuxdo",
        "oauth_account": "default",
        "script": "scripts/checkin/abrdns_welfare.py",
        "script_args": {"script_handles_oauth": True},
        "enabled": True,
    }
    site.update(overrides)
    return site


def test_worker_state_basis_follows_configured_value_not_injected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CHECKIN_CONFIGURED_BROWSER_STATE", "")
    site = accounts_store.runtime_site_from_mapping(
        _oauth_script_site(browser_state="INJECTED-RUNTIME-STATE"),
        cache_policy="ignore",
    )
    assert site.runtime_credentials.state_basis == token_cache.credential_basis(
        browser_state="INJECTED-RUNTIME-STATE", group="state"
    )

    checkin._stabilize_oauth_state_basis(site)

    assert site.runtime_credentials.state_basis == token_cache.credential_basis(
        browser_state="", group="state"
    )
    # 注入值本身仍然可用，只是不再参与 basis 计算。
    assert site.browser_state == "INJECTED-RUNTIME-STATE"


def test_configured_state_keeps_its_own_basis(monkeypatch) -> None:
    monkeypatch.setenv("CHECKIN_CONFIGURED_BROWSER_STATE", "CONFIGURED-STATE")
    site = accounts_store.runtime_site_from_mapping(
        _oauth_script_site(browser_state="CACHED-RUNTIME-STATE"),
        cache_policy="ignore",
    )

    checkin._stabilize_oauth_state_basis(site)

    assert site.runtime_credentials.state_basis == token_cache.credential_basis(
        browser_state="CONFIGURED-STATE", group="state"
    )


def test_non_oauth_script_site_basis_is_untouched(monkeypatch) -> None:
    monkeypatch.setenv("CHECKIN_CONFIGURED_BROWSER_STATE", "")
    site = accounts_store.runtime_site_from_mapping(
        _oauth_script_site(auth_method="browser", browser_state="SITE-STATE"),
        cache_policy="ignore",
    )
    before = site.runtime_credentials.state_basis

    checkin._stabilize_oauth_state_basis(site)

    assert site.runtime_credentials.state_basis == before


def test_persisted_session_is_reused_on_next_run(monkeypatch, tmp_path) -> None:
    """核心断言：脚本续存的会话必须能被下一轮父进程解析出来。"""
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    monkeypatch.setenv("CHECKIN_CONFIGURED_BROWSER_STATE", "")

    # 第 1 轮：父进程注入共享 provider 登录态，脚本结束后写回本站会话快照。
    worker_site = accounts_store.runtime_site_from_mapping(
        _oauth_script_site(browser_state="SHARED-PROVIDER-STATE"),
        cache_policy="ignore",
    )
    checkin._stabilize_oauth_state_basis(worker_site)
    assert token_cache.save_site_tokens(
        worker_site, "", "", browser_state="SITE-SESSION-AFTER-OAUTH", path=cache
    )

    # 第 2 轮：父进程只有配置（browser_state 为空），必须命中上一轮缓存。
    resolved = token_cache.resolve_cached_credentials(
        worker_site.name,
        worker_site.base_url,
        configured_access_token="",
        configured_refresh_token="",
        configured_browser_state="",
        path=cache,
    )

    assert resolved.get("browser_state") == "SITE-SESSION-AFTER-OAUTH"


def test_parent_prefers_cached_site_session_for_script_handled_oauth(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "token_cache.json"
    monkeypatch.setattr(token_cache, "CACHE_PATH", cache)
    raw_site = _oauth_script_site()
    site_config = accounts_store.configured_site_from_mapping(raw_site)
    token_cache.save_tokens(
        site_config.name,
        site_config.base_url,
        browser_state="SITE-SESSION-AFTER-OAUTH",
        path=cache,
        state_basis=token_cache.credential_basis(browser_state="", group="state"),
    )

    resolved_site = accounts_store.runtime_site_from_mapping(raw_site)

    assert runner._script_args_handles_oauth(raw_site) is True
    assert resolved_site.browser_state == "SITE-SESSION-AFTER-OAUTH"
