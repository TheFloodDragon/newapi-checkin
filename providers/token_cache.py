#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行期 token 缓存（token_cache.json）。

为什么不写回 ACCOUNTS.json：
- ACCOUNTS.json 是**用户维护的配置**，记录站点怎么签到、用哪个账号。把程序自动
  续期出来的短期 JWT 混进去，会让配置文件被后台任务反复改写：用户在 GUI 里编辑
  的同时，CLI 可能正在写入新 token，diff 噪声大、也容易和手工修改抢锁。
- access_token 是**运行期产物**（Sub2API 的 JWT 往往只有几小时有效期），语义上
  属于缓存而不是配置。配置应当只保留长期凭据（账密、browser_state、oauth 账号）。
- 同一份 ACCOUNTS.json 会导出为 GitHub Secret；混入短期 token 只会让 Secret 更快
  过期，没有任何收益。

因此续期结果统一落到独立的 token_cache.json：
- 结构：{"tokens": {"<base_url>|<name>": {access_token, refresh_token, updated_at}}}
- 与 login_grant_state.json 一样，走 accounts_store 的原子写 + 跨进程文件锁；
- 已在 .gitignore 覆盖范围内（*.lock + 显式条目），不会入库；
- 读取时优先用缓存里的新 token，回落到 ACCOUNTS.json 里用户填的初始 token。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import accounts_store

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CACHE_PATH = accounts_store.RESULTS_DIR / "token_cache.json"


def _cache_key(name: str, base_url: str) -> str:
    """按 base_url + name 定位账号，与 StatusStore.status_key 保持一致的形态。"""
    base = accounts_store.normalize_base_url(str(base_url or ""))
    return f"{base}|{str(name or '').strip()}"


def _read_all(path: Path | None = None) -> dict[str, Any]:
    """读取整份缓存；文件缺失或损坏都返回空 dict（缓存可重建，不必失败）。"""
    target = path or CACHE_PATH
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    tokens = data.get("tokens")
    return tokens if isinstance(tokens, dict) else {}


def load_tokens(name: str, base_url: str, path: Path | None = None) -> dict[str, str]:
    """取某账号缓存的运行期凭据（access_token / refresh_token / browser_state）。

    无缓存返回 {}。browser_state 同属运行期产物：浏览器每次打开站点都会刷新
    cookie/localStorage，写回 ACCOUNTS.json 会让用户配置被后台任务反复改写。
    """
    entry = _read_all(path).get(_cache_key(name, base_url))
    if not isinstance(entry, dict):
        return {}
    out: dict[str, str] = {}
    for field in ("access_token", "refresh_token", "browser_state"):
        value = str(entry.get(field) or "").strip()
        if value:
            out[field] = value
    return out


def save_tokens(
    name: str,
    base_url: str,
    access_token: str = "",
    refresh_token: str = "",
    path: Path | None = None,
    browser_state: str = "",
) -> bool:
    """把续期出的凭据写入缓存（原子写 + 文件锁）。

    只更新传入的非空字段，避免一次只拿到 access_token 时把已有 refresh_token 抹掉。
    写入失败返回 False，调用方应当继续使用内存中的 token，不因缓存失败中断签到。
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
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
            tokens[key] = entry
            accounts_store.atomic_write_text(
                target,
                json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2),
            )
    except Exception:
        return False
    return True


def save_browser_state(
    name: str,
    base_url: str,
    browser_state: str,
    path: Path | None = None,
) -> bool:
    """只写 browser_state（签到过程中刷新出的登录态）。

    与 token 同理：登录态是运行期产物，每次签到都可能被服务端换掉 cookie。写回
    ACCOUNTS.json 会让用户配置被后台任务反复改写，也会把几十 KB 的 base64 塞进
    导出的 GitHub Secret。
    """
    return save_tokens(name, base_url, browser_state=browser_state, path=path)


def load_browser_state(name: str, base_url: str, path: Path | None = None) -> str:
    """取某渠道缓存的 browser_state；无缓存返回空串。"""
    entry = _read_all(path).get(_cache_key(name, base_url))
    if not isinstance(entry, dict):
        return ""
    return str(entry.get("browser_state") or "").strip()


def reconcile_with_config(
    name: str,
    base_url: str,
    access_token: str = "",
    refresh_token: str = "",
    path: Path | None = None,
) -> bool:
    """用户手工填写的凭据与缓存冲突时，丢弃缓存条目；返回是否清理了缓存。

    读取时缓存优先（缓存里通常是刚续期出的新 token，比配置里的初始值新）。但这条
    规则对「用户刚手工粘贴了新凭据」是错的：旧缓存会把用户填的值直接盖掉，表现为
    「填了有效 token 却仍提示没有 / 用不了」。保存配置时调用本函数，凡是用户填的值
    与缓存不一致就清掉缓存，让新填的值生效；之后续期会重新写入缓存。
    """
    access = str(access_token or "").strip()
    refresh = str(refresh_token or "").strip()
    if not access and not refresh:
        return False
    cached = load_tokens(name, base_url, path)
    if not cached:
        return False
    conflict = (access and cached.get("access_token", "") != access) or (
        refresh and cached.get("refresh_token", "") != refresh
    )
    if not conflict:
        return False
    return clear_tokens(name, base_url, path)


def clear_tokens(name: str, base_url: str, path: Path | None = None) -> bool:
    """删除某账号的缓存条目（凭据确认失效时调用）。"""
    target = path or CACHE_PATH
    key = _cache_key(name, base_url)
    try:
        with accounts_store.file_lock(target):
            tokens = _read_all(target)
            if key not in tokens:
                return False
            tokens.pop(key, None)
            accounts_store.atomic_write_text(
                target,
                json.dumps({"tokens": tokens}, ensure_ascii=False, indent=2),
            )
    except Exception:
        return False
    return True


def apply_cached_tokens(site: Any) -> bool:
    """把缓存里的 token 合并进 SiteConfig，返回是否命中缓存。

    缓存优先于 ACCOUNTS.json 里的初始值：配置里的 token 往往是用户第一次采集时
    粘贴的，早已过期；缓存里的才是最近一次续期结果。
    """
    cached = load_tokens(getattr(site, "name", ""), getattr(site, "base_url", ""))
    if not cached:
        return False
    access = cached.get("access_token", "")
    refresh = cached.get("refresh_token", "")
    if access:
        site.access_token = access
    if refresh:
        site.refresh_token = refresh
    return bool(access or refresh)
