#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行期凭据缓存（token_cache.json）。

短期 access_token、轮换 refresh_token 与浏览器运行态不写回用户维护的
ACCOUNTS.json，而存入 ``.cache-checkin/token_cache.json``。

缓存不是无条件权威来源。每个条目记录生成时配置凭据的不可逆 basis：只有 basis
仍与当前 ACCOUNTS/GitHub Secret 一致时，缓存才可覆盖配置。这样用户更新 Secret
后，旧 Actions cache 会被自动忽略；GUI/CLI 显式输入（包括显式清空）则始终优先。

结构保持向后兼容：旧代码仍能读取 ``tokens`` 下的 access_token/refresh_token/
browser_state；新版仅增加 version、token_basis、state_basis 与带时区 updated_at。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import accounts_store
import time_utils

CACHE_PATH = accounts_store.RESULTS_DIR / "token_cache.json"
CACHE_VERSION = 2
TOKEN_FIELDS = frozenset({"access_token", "refresh_token"})
STATE_FIELDS = frozenset({"browser_state"})
ALL_CREDENTIAL_FIELDS = TOKEN_FIELDS | STATE_FIELDS


def _cache_key(name: str, base_url: str) -> str:
    """按 base_url + name 定位渠道，与 GUI StatusStore 的身份形态一致。"""
    base = accounts_store.normalize_base_url(str(base_url or ""))
    return f"{base}|{str(name or '').strip()}"


def _utc_iso() -> str:
    return time_utils.utc_iso()


def credential_basis(
    access_token: str = "",
    refresh_token: str = "",
    browser_state: str = "",
    *,
    group: str = "token",
) -> str:
    """返回配置凭据种子的不可逆 SHA-256 摘要。

    token 与 browser_state 分组计算，避免用户只更新其中一组时误伤另一组缓存。
    摘要只用于兼容性判断，不写入日志，也无法还原原凭据。
    """
    if group == "state":
        payload = {"browser_state": str(browser_state or "").strip()}
    elif group == "token":
        payload = {
            "access_token": str(access_token or "").strip(),
            "refresh_token": str(refresh_token or "").strip(),
        }
    else:
        raise ValueError(f"未知凭据 basis 分组：{group}")
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_document(path: Path | None = None) -> dict[str, Any]:
    """读取缓存文档；缺失/损坏返回空文档（缓存可重建，不阻断签到）。"""
    target = path or CACHE_PATH
    if not target.exists():
        return {"version": CACHE_VERSION, "tokens": {}}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"version": CACHE_VERSION, "tokens": {}}
    if not isinstance(data, dict):
        return {"version": CACHE_VERSION, "tokens": {}}
    tokens = data.get("tokens")
    return {
        "version": data.get("version") if isinstance(data.get("version"), int) else 1,
        "tokens": tokens if isinstance(tokens, dict) else {},
    }


def _read_all(path: Path | None = None) -> dict[str, Any]:
    """兼容旧内部调用：返回 tokens 映射。"""
    return dict(_read_document(path).get("tokens") or {})


def load_cache_entry(name: str, base_url: str, path: Path | None = None) -> dict[str, Any]:
    """读取某渠道的完整原始缓存条目（含 basis/updated_at）。"""
    entry = _read_all(path).get(_cache_key(name, base_url))
    return dict(entry) if isinstance(entry, dict) else {}


def load_tokens(name: str, base_url: str, path: Path | None = None) -> dict[str, str]:
    """兼容读取某渠道缓存的非空凭据，不执行 basis 判断。

    运行主链路应使用 ``resolve_cached_credentials``；本函数保留给诊断、兼容调用和
    测试读取缓存内容。它没有当前配置上下文，因此无法判断条目是否仍可覆盖配置。
    """
    entry = load_cache_entry(name, base_url, path)
    out: dict[str, str] = {}
    for field in ("access_token", "refresh_token", "browser_state"):
        value = str(entry.get(field) or "").strip()
        if value:
            out[field] = value
    return out


def resolve_cached_credentials(
    name: str,
    base_url: str,
    *,
    configured_access_token: str = "",
    configured_refresh_token: str = "",
    configured_browser_state: str = "",
    explicit_fields: Iterable[str] = (),
    cache_policy: str = "compatible",
    path: Path | None = None,
) -> dict[str, str]:
    """返回当前配置允许使用的缓存字段。

    规则：
    - cache_policy=ignore：完全不读缓存（父进程已解析后的 worker）。
    - 显式字段永不被缓存覆盖，包括显式空字符串。
    - 新版有 basis 的分组：basis 与当前配置种子一致才应用。
    - 旧版无 basis 的条目：仅在对应配置字段为空时兜底，避免覆盖新 Secret。
    """
    if str(cache_policy or "compatible").strip().lower() == "ignore":
        return {}
    explicit = {str(field) for field in explicit_fields if str(field) in ALL_CREDENTIAL_FIELDS}
    entry = load_cache_entry(name, base_url, path)
    if not entry:
        return {}

    configured = {
        "access_token": str(configured_access_token or "").strip(),
        "refresh_token": str(configured_refresh_token or "").strip(),
        "browser_state": str(configured_browser_state or "").strip(),
    }
    resolved: dict[str, str] = {}

    current_token_basis = credential_basis(
        configured["access_token"], configured["refresh_token"], group="token"
    )
    cached_token_basis = str(entry.get("token_basis") or "").strip()
    token_basis_matches = bool(cached_token_basis and cached_token_basis == current_token_basis)
    for field in TOKEN_FIELDS:
        value = str(entry.get(field) or "").strip()
        if not value or field in explicit:
            continue
        if cached_token_basis:
            if token_basis_matches:
                resolved[field] = value
        elif not configured[field]:
            # 旧缓存无 basis：配置为空时兼容使用；配置已有值时配置优先。
            resolved[field] = value

    current_state_basis = credential_basis(browser_state=configured["browser_state"], group="state")
    cached_state_basis = str(entry.get("state_basis") or "").strip()
    state_value = str(entry.get("browser_state") or "").strip()
    if state_value and "browser_state" not in explicit:
        if cached_state_basis:
            if cached_state_basis == current_state_basis:
                resolved["browser_state"] = state_value
        elif not configured["browser_state"]:
            resolved["browser_state"] = state_value

    return resolved


def _write_tokens(target: Path, tokens: dict[str, Any]) -> None:
    accounts_store.atomic_write_text(
        target,
        json.dumps({"version": CACHE_VERSION, "tokens": tokens}, ensure_ascii=False, indent=2),
    )


def save_tokens(
    name: str,
    base_url: str,
    access_token: str = "",
    refresh_token: str = "",
    path: Path | None = None,
    browser_state: str = "",
    *,
    token_basis: str = "",
    state_basis: str = "",
) -> bool:
    """写运行期凭据（原子写 + 文件锁），只更新传入的非空字段。

    basis 为空时保留条目原 basis；用于兼容仓库外调用。内置运行链路应优先调用
    ``save_site_tokens`` / ``save_site_browser_state``，确保新条目总有配置基线。
    """
    access = str(access_token or "").strip()
    refresh = str(refresh_token or "").strip()
    state_text = str(browser_state or "").strip()
    if not access and not refresh and not state_text:
        return False
    target = path or CACHE_PATH
    key = _cache_key(name, base_url)
    try:
        with accounts_store.file_lock(target):
            tokens = _read_all(target)
            entry = tokens.get(key)
            entry = dict(entry) if isinstance(entry, dict) else {}
            if access:
                entry["access_token"] = access
            if refresh:
                entry["refresh_token"] = refresh
            if state_text:
                entry["browser_state"] = state_text
            if (access or refresh) and token_basis:
                entry["token_basis"] = str(token_basis)
            if state_text and state_basis:
                entry["state_basis"] = str(state_basis)
            entry["updated_at"] = _utc_iso()
            tokens[key] = entry
            _write_tokens(target, tokens)
    except Exception:
        return False
    return True


def _site_context(site: Any) -> tuple[str, str]:
    context = getattr(site, "runtime_credentials", None)
    token = str(getattr(context, "token_basis", "") or "")
    state = str(getattr(context, "state_basis", "") or "")
    if not token:
        token = credential_basis(
            getattr(site, "access_token", ""), getattr(site, "refresh_token", ""), group="token"
        )
    if not state:
        state = credential_basis(browser_state=getattr(site, "browser_state", ""), group="state")
    return token, state


def save_site_tokens(
    site: Any,
    access_token: str = "",
    refresh_token: str = "",
    *,
    browser_state: str = "",
    path: Path | None = None,
) -> bool:
    """按 SiteConfig 的配置 basis 写运行期 token/state。"""
    token_basis, state_basis = _site_context(site)
    return save_tokens(
        getattr(site, "name", ""),
        getattr(site, "base_url", ""),
        access_token,
        refresh_token,
        path,
        browser_state,
        token_basis=token_basis,
        state_basis=state_basis,
    )


def save_browser_state(
    name: str,
    base_url: str,
    browser_state: str,
    path: Path | None = None,
    *,
    state_basis: str = "",
) -> bool:
    """兼容的 state-only 写入入口。"""
    return save_tokens(
        name,
        base_url,
        browser_state=browser_state,
        path=path,
        state_basis=state_basis,
    )


def save_site_browser_state(site: Any, browser_state: str, path: Path | None = None) -> bool:
    """按 SiteConfig 的配置 state basis 写运行期 browser_state。"""
    _token_basis, state_basis = _site_context(site)
    return save_browser_state(
        getattr(site, "name", ""),
        getattr(site, "base_url", ""),
        browser_state,
        path,
        state_basis=state_basis,
    )


def load_browser_state(name: str, base_url: str, path: Path | None = None) -> str:
    """兼容读取某渠道缓存的 browser_state；不执行 basis 判断。"""
    return str(load_cache_entry(name, base_url, path).get("browser_state") or "").strip()


def invalidate_fields(
    name: str,
    base_url: str,
    fields: Iterable[str],
    path: Path | None = None,
) -> bool:
    """只删除指定凭据字段，token/state 分组互不误伤。"""
    requested = {str(field) for field in fields if str(field) in ALL_CREDENTIAL_FIELDS}
    if not requested:
        return False
    target = path or CACHE_PATH
    key = _cache_key(name, base_url)
    try:
        with accounts_store.file_lock(target):
            tokens = _read_all(target)
            raw = tokens.get(key)
            if not isinstance(raw, dict):
                return False
            entry = dict(raw)
            changed = False
            for field in requested:
                if field in entry:
                    entry.pop(field, None)
                    changed = True
            if requested & TOKEN_FIELDS:
                changed = entry.pop("token_basis", None) is not None or changed
            if requested & STATE_FIELDS:
                changed = entry.pop("state_basis", None) is not None or changed
            if not changed:
                return False
            credential_keys = ALL_CREDENTIAL_FIELDS & set(entry)
            if credential_keys:
                entry["updated_at"] = _utc_iso()
                tokens[key] = entry
            else:
                tokens.pop(key, None)
            _write_tokens(target, tokens)
    except Exception:
        return False
    return True


def reconcile_with_config(
    name: str,
    base_url: str,
    access_token: str = "",
    refresh_token: str = "",
    path: Path | None = None,
    browser_state: str | None = None,
) -> bool:
    """兼容旧 GUI 对账：只失效真正冲突的字段组，不再删除整个账号缓存。"""
    cached = load_tokens(name, base_url, path)
    if not cached:
        return False
    fields: set[str] = set()
    access = str(access_token or "").strip()
    refresh = str(refresh_token or "").strip()
    if (access and cached.get("access_token", "") != access) or (
        refresh and cached.get("refresh_token", "") != refresh
    ):
        fields.update(TOKEN_FIELDS)
    if browser_state is not None:
        state_text = str(browser_state or "").strip()
        if cached.get("browser_state", "") != state_text:
            fields.add("browser_state")
    return invalidate_fields(name, base_url, fields, path)


def clear_tokens(name: str, base_url: str, path: Path | None = None) -> bool:
    """删除某渠道的整个缓存条目。"""
    target = path or CACHE_PATH
    key = _cache_key(name, base_url)
    try:
        with accounts_store.file_lock(target):
            tokens = _read_all(target)
            if key not in tokens:
                return False
            tokens.pop(key, None)
            _write_tokens(target, tokens)
    except Exception:
        return False
    return True


def apply_cached_tokens(site: Any) -> bool:
    """兼容入口：按 SiteConfig 当前配置上下文合并可用缓存。"""
    context = getattr(site, "runtime_credentials", None)
    resolved = resolve_cached_credentials(
        getattr(site, "name", ""),
        getattr(site, "base_url", ""),
        configured_access_token=getattr(site, "access_token", ""),
        configured_refresh_token=getattr(site, "refresh_token", ""),
        configured_browser_state=getattr(site, "browser_state", ""),
        explicit_fields=getattr(context, "explicit_fields", ()),
        cache_policy=getattr(context, "cache_policy", "compatible"),
    )
    for field, value in resolved.items():
        setattr(site, field, value)
    return bool(resolved)
