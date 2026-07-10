#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置与凭据的共享读写层。

主配置文件：ACCOUNTS.json，统一保存站点配置、本地启用状态与凭据：
- 站点配置：name、base_url、type、checkin_mode 等；
- 本地状态与凭据：enabled、user_id、access_token、cookie 等；
- GitHub 上整份内容存为单个 Secret。已被 .gitignore 忽略。

为兼容旧配置，读取时仍可从 sites.json 补全缺失的站点字段。

ACCOUNTS.json 支持以下任一形态：

    {"accounts": [{"name": "站点名", "base_url": "", "type": "newapi", "user_id": "", ...}]}
    {"accounts": {"站点名": {"base_url": "", "user_id": "", ...}}}
    {"站点名": {"user_id": "", ...}}            # 省略 accounts 包裹
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

SCRIPT_DIR = Path(__file__).resolve().parent
SITES_CONFIG_PATH = SCRIPT_DIR / "sites.json"
ACCOUNTS_PATH = SCRIPT_DIR / "ACCOUNTS.json"


class ConfigError(Exception):
    """Raised when a config file exists but cannot be parsed as valid JSON.

    We surface this instead of a bare json.JSONDecodeError so callers can tell
    "file is corrupt" apart from "file is missing" and give a clear message
    rather than crashing every task with an opaque traceback.
    """


def _read_json_file(path: Path, *, what: str = "config") -> Any:
    """Read and JSON-parse a file, raising ConfigError with a clear message.

    The caller is expected to have checked existence; a parse failure here
    means the file is present but corrupt (e.g. a previous non-atomic write was
    interrupted). We refuse to silently treat corrupt data as empty, because
    that could wipe a user's real config on the next save.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ConfigError(f"{what} file {path.name} could not be read: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{what} file {path.name} is not valid JSON (it may be corrupt from an "
            f"interrupted write): {exc}"
        ) from exc


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically (temp file in same dir + os.replace).

    A crash mid-write can otherwise leave a truncated, unparseable file that
    breaks every subsequent read. Writing to a sibling temp file and then
    os.replace() guarantees readers always see either the old or the new full
    content, never a partial one. flush + fsync makes the bytes durable before
    the rename so a power loss right after replace still yields valid content.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


class _LockState:
    """Holds the process-wide reentrant lock and per-thread lock bookkeeping."""

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.local = threading.local()


_LOCK_STATE = _LockState()


@contextlib.contextmanager
def _file_lock(path: Path, *, timeout: float = 30.0):
    """Cross-platform advisory lock serializing read-modify-write on `path`.

    Concurrent check-in tasks each do "read whole file -> change one entry ->
    write whole file". Without a lock, two writers that both read version v0
    race, and the later writer clobbers the earlier one's change (lost update).
    We hold an exclusive lock on a sibling ``<name>.lock`` file for the entire
    RMW so those operations serialize.

    若无法在超时内获得锁则显式失败，绝不在无锁状态继续读-改-写，避免并发覆盖。
    """
    # Reentrancy: the same thread may nest lock scopes (e.g. update_* -> save_accounts).
    # A plain OS file lock would self-deadlock on Windows (msvcrt), so we serialize
    # within the process using a reentrant lock and only touch the OS-level lock at
    # the outermost scope, tracked by a per-thread depth counter.
    _LOCK_STATE.rlock.acquire()
    try:
        depth = getattr(_LOCK_STATE.local, "depth", 0)
        _LOCK_STATE.local.depth = depth + 1
        handle = None
        locked = False
        try:
            if depth == 0:
                lock_path = path.parent / (path.name + ".lock")
                path.parent.mkdir(parents=True, exist_ok=True)
                handle = open(lock_path, "a+")  # noqa: SIM115 - lifetime tied to context
                locked = _acquire_lock(handle, timeout=timeout)
                if not locked:
                    raise ConfigError(f"等待配置文件锁超时：{lock_path.name}")
                _LOCK_STATE.local.handle = handle
                _LOCK_STATE.local.locked = locked
            yield
        finally:
            _LOCK_STATE.local.depth = depth
            if depth == 0:
                if handle is not None:
                    if locked:
                        _release_lock(handle)
                    with contextlib.suppress(OSError):
                        handle.close()
                _LOCK_STATE.local.handle = None
                _LOCK_STATE.local.locked = False
    finally:
        _LOCK_STATE.rlock.release()


def _acquire_lock(handle, *, timeout: float) -> bool:
    """Try to take an exclusive lock on an open file handle. Returns success."""
    deadline = time.monotonic() + timeout
    try:
        import msvcrt  # Windows

        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
    except ImportError:
        pass

    try:
        import fcntl  # POSIX

        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
    except ImportError:
        return False


def _release_lock(handle) -> None:
    """Release a lock previously taken by _acquire_lock (best-effort)."""
    try:
        import msvcrt

        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    except ImportError:
        pass
    try:
        import fcntl

        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except ImportError:
        pass


def atomic_write_text(path: Path, text: str) -> None:
    """公开的共享原子写入口。"""
    _atomic_write_text(path, text)


@contextlib.contextmanager
def file_lock(path: Path, *, timeout: float = 30.0):
    """公开的共享文件锁入口，供额度状态和结果文件复用。"""
    with _file_lock(path, timeout=timeout):
        yield


# 第三方 OAuth 提供商（OAuth 登录 / relogin 使用），登录态跨站点共享，存 ACCOUNTS.json 顶层 oauth_states
KNOWN_OAUTH_PROVIDERS = ("linuxdo", "github")
DEFAULT_OAUTH_ACCOUNT = "default"
# provider → 判定该 provider 登录态的域名特征
OAUTH_PROVIDER_DOMAINS = {
    "linuxdo": ("linux.do", "connect.linux.do"),
    "github": ("github.com",),
}


def _state_domains(state_text: str) -> set[str]:
    """从经过严格校验的 storage_state 提取 cookie 域名集合。"""
    try:
        from browser.state import decode_state

        data = decode_state(state_text)
    except Exception:
        return set()
    domains: set[str] = set()
    for cookie in data.get("cookies", []) if isinstance(data, dict) else []:
        dom = str(cookie.get("domain", "")).lstrip(".")
        if dom:
            domains.add(dom)
    return domains


def guess_oauth_provider(state_text: str) -> str:
    """按登录态里的域名猜 OAuth 提供商（linux.do → linuxdo，github.com → github）。"""
    domains = _state_domains(state_text)
    if any("linux.do" in d for d in domains):
        return "linuxdo"
    if any("github.com" in d for d in domains):
        return "github"
    return ""


def state_contains_site_domain(state_text: str, base_url: str) -> bool:
    """判断 storage_state 是否含目标站点 Cookie；OAuth 共享态不应保存站点凭证。"""
    host = urlparse(normalize_base_url(str(base_url or ""))).hostname or ""
    host = host.lstrip(".").lower()
    if not host:
        return False
    return any(domain == host or domain.endswith("." + host) for domain in _state_domains(state_text))


def normalize_oauth_provider(value: Any) -> str:
    key = _norm_key(str(value or ""))
    return key if key in KNOWN_OAUTH_PROVIDERS else ""


def normalize_oauth_account(value: Any) -> str:
    """规范化 OAuth 账号名；空值统一落到默认账号 default。"""
    text = str(value or "").strip()
    return text or DEFAULT_OAUTH_ACCOUNT


CRED_FIELDS = ("user_id", "access_token", "cookie")
CONFIG_FIELDS = (
    "name",
    "base_url",
    # 新正交三维
    "site_profile",
    "auth_method",
    "checkin_action",
    "script",
    "script_args",
    "script_timeout",
    "api_variant",
    # 旧字段（向后兼容输入）
    "type",
    "provider",
    "checkin_mode",
    "mode",
    "enabled",
    "cookie_file",
    "token_file",
    "referer_path",
    "auto_refresh_cookie",
    "browser_state",
    "browser_profile",
    "login_selector",
    "oauth_provider",
    "oauth_account",
    "oauth_account_id",
    "proxy",
)

# ── 旧 type + checkin_mode → 新 site_profile + auth_method + checkin_action 迁移 ──

KNOWN_PROFILES = ("newapi", "sub2api")
KNOWN_AUTH_METHODS = ("access_token", "cookie", "browser", "oauth")
KNOWN_ACTIONS = ("api", "relogin", "visit", "browser_script")


def _infer_auth_method(checkin_action: str, *, sub2api_browser: bool, has_token: bool) -> str:
    """按规则推断登录方式：relogin → oauth；浏览器刷新 → browser；有 token → access_token；否则 cookie。"""
    if checkin_action in {"relogin", "browser_script"}:
        return "oauth"
    if sub2api_browser:
        return "browser"
    return "access_token" if has_token else "cookie"


def migrate_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """把旧 type + checkin_mode 语义迁移为新三维字段；已是新格式则规范化。

    返回新 dict（原 entry 不被修改）。幂等：对新格式输入只补全/规范化。
    relogin 站点统一补 oauth_provider（缺失时按 browser_state 域名猜，兜底 linuxdo）。
    """
    out = dict(entry)
    has_token = bool(str(out.get("access_token") or "").strip())
    has_state = bool(str(out.get("browser_state") or "").strip())

    if out.get("site_profile") or out.get("auth_method") or out.get("checkin_action"):
        # 已是新格式：规范化三维 + 推断缺失项
        profile = str(out.get("site_profile") or out.get("type") or out.get("provider") or "newapi").strip().lower()
        if profile not in KNOWN_PROFILES:
            profile = "newapi"
        action = str(out.get("checkin_action") or "api").strip().lower()
        if action in {"browser_oauth", "oauth_relogin"}:
            action = "relogin"
        if action not in KNOWN_ACTIONS:
            action = "api"
        auth = str(out.get("auth_method") or "").strip().lower()
        if auth in {"browser_oauth", "relogin", "oauth_relogin"}:
            auth = "oauth"
            action = "relogin"
        elif auth == "browser" and action == "relogin":
            # 旧版三维写法曾要求 browser+relogin；新模型中 OAuth 重登必须显式使用 oauth。
            auth = "oauth"
        if auth not in KNOWN_AUTH_METHODS:
            auth = _infer_auth_method(action, sub2api_browser=False, has_token=has_token)
        out["site_profile"] = profile
        out["auth_method"] = auth
        out["checkin_action"] = action
    else:
        # 旧格式映射
        old_type = str(out.get("type") or out.get("provider") or "newapi").strip().lower()
        if old_type not in KNOWN_PROFILES:
            old_type = "newapi"
        old_mode = str(out.get("checkin_mode") or out.get("mode") or "").strip().lower()

        sub2api_browser = False
        api_variant = "auto"
        if old_type == "sub2api":
            profile = "sub2api"
            action = "api"
            if old_mode == "browser":
                sub2api_browser = True
        else:  # newapi
            profile = "newapi"
            if old_mode in {"browser_oauth", "relogin", "oauth_relogin"}:
                action = "relogin"
            elif old_mode == "login_grant":
                action = "visit"
            else:  # legacy / challenge / 空
                action = "api"
                api_variant = "legacy" if old_mode == "legacy" else "auto"

        auth = _infer_auth_method(action, sub2api_browser=sub2api_browser, has_token=has_token)
        # browser 登录方式但无 state 且有 token：退回 access_token（避免必然失败）
        if auth == "browser" and not has_state and has_token and action != "relogin":
            auth = "access_token"

        out["site_profile"] = profile
        out["auth_method"] = auth
        out["checkin_action"] = action
        if profile == "newapi" and action == "api":
            out["api_variant"] = api_variant
        # 清理旧字段（迁移写回时不再保留 type/checkin_mode）
        out.pop("type", None)
        out.pop("provider", None)
        out.pop("checkin_mode", None)
        out.pop("mode", None)

    # 统一：OAuth 登录 / relogin 站点补 oauth_provider + oauth_account
    if out.get("auth_method") == "oauth" or out.get("checkin_action") == "relogin":
        prov = normalize_oauth_provider(out.get("oauth_provider"))
        if not prov:
            prov = guess_oauth_provider(str(out.get("browser_state") or "")) or "linuxdo"
        out["oauth_provider"] = prov
        out["oauth_account"] = normalize_oauth_account(out.get("oauth_account") or out.get("oauth_account_id"))

    return out


def normalize_script_args(value: Any) -> dict[str, Any]:
    """规范化 browser_script 的脚本参数；支持 dict 或 JSON 字符串。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def parse_script_timeout(value: Any, default: int = 120) -> int:
    """解析 browser_script 超时秒数，限制在 1 秒以上。"""
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, timeout)


def parse_enabled(value: Any, default: bool = True) -> bool:
    """把 ACCOUNTS.json 中的 enabled 值解析为 bool。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled", "启用"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled", "禁用", "关闭"}:
        return False
    return default


def normalize_base_url(value: str) -> str:
    value = (value or "").strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def _norm_key(value: str) -> str:
    return (value or "").strip().lower()


def _account_entries(path: Path | None = None) -> list[dict[str, Any]]:
    """读取 ACCOUNTS.json，保留原始账号条目顺序。"""
    path = path or ACCOUNTS_PATH
    if not path.exists():
        return []
    raw = _read_json_file(path, what="accounts")
    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]

    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if str(key).startswith("_") or key == "oauth_states" or not isinstance(value, dict):
                continue
            entry = {"name": value.get("name") or key}
            entry.update(value)
            entries.append(entry)
    elif isinstance(raw, list):
        if any(not isinstance(item, dict) for item in raw):
            raise ConfigError("accounts 数组中的每一项都必须是对象")
        entries = [item.copy() for item in raw]
    else:
        raise ConfigError("accounts 顶层必须是数组、对象映射或包含 accounts 的对象")
    return entries


def _normalize_account_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """标准化单个账号条目：统一字段名/类型，并迁移到新正交三维字段。"""
    out = entry.copy()
    base_url = normalize_base_url(str(out.get("base_url") or out.get("url") or ""))
    if base_url:
        out["base_url"] = base_url
    if "name" in out:
        out["name"] = str(out.get("name") or base_url)
    elif base_url:
        out["name"] = base_url
    if "enabled" in out:
        out["enabled"] = parse_enabled(out.get("enabled"), True)
    for field in CRED_FIELDS:
        if field in out:
            out[field] = str(out.get(field) or "")
    # 旧 type + checkin_mode → 新 site_profile + auth_method + checkin_action
    out = migrate_fields(out)
    if "script_args" in out:
        out["script_args"] = normalize_script_args(out.get("script_args"))
    if "script_timeout" in out:
        out["script_timeout"] = parse_script_timeout(out.get("script_timeout"))
    return out


def site_config_from_mapping(
    data: dict[str, Any],
    *,
    overrides: dict[str, Any] | None = None,
):
    """把任意兼容配置映射规范化为唯一的 ``SiteConfig`` 构造路径。"""
    from providers.base import SiteConfig

    raw = dict(data or {})
    if overrides:
        raw.update(overrides)
    row = _normalize_account_entry(raw)
    base_url = normalize_base_url(str(row.get("base_url") or row.get("url") or ""))
    return SiteConfig(
        name=str(row.get("name") or base_url),
        base_url=base_url,
        site_profile=str(row.get("site_profile") or "newapi"),
        auth_method=str(row.get("auth_method") or "cookie"),
        checkin_action=str(row.get("checkin_action") or "api"),
        script=str(row.get("script") or ""),
        script_args=normalize_script_args(row.get("script_args")),
        script_timeout=parse_script_timeout(row.get("script_timeout"), 120),
        api_variant=str(row.get("api_variant") or "auto"),
        cookie=str(row.get("cookie") or ""),
        user_id=str(row.get("user_id") or row.get("new_api_user") or ""),
        access_token=str(row.get("access_token") or row.get("authorization") or ""),
        cookie_file=str(row.get("cookie_file") or row.get("token_file") or ""),
        browser_state=str(row.get("browser_state") or ""),
        browser_profile=str(row.get("browser_profile") or ".browser_profile"),
        login_selector=str(row.get("login_selector") or ""),
        oauth_provider=normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo",
        oauth_account=normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id")),
        proxy=str(row.get("proxy") or ""),
        referer_path=str(row.get("referer_path") or "/profile"),
        enabled=parse_enabled(row.get("enabled"), True),
        auto_refresh_cookie=parse_enabled(row.get("auto_refresh_cookie"), True),
    )


def load_raw_sites(path: Path | None = None) -> list[dict[str, Any]]:
    """读取 sites.json，返回站点配置数组（不含凭据）。"""
    path = path or SITES_CONFIG_PATH
    if not path.exists():
        return []
    raw = _read_json_file(path, what="sites")
    raw_sites = raw.get("sites", []) if isinstance(raw, dict) else raw
    if not isinstance(raw_sites, list):
        raise ValueError("sites.json 必须是数组，或包含 sites 数组的对象。")
    return [site for site in raw_sites if isinstance(site, dict)]


# ── 共享 OAuth 登录态（linux.do / github，跨 relogin 站点共享）──────────────────

def _read_full(path: Path | None = None) -> dict[str, Any]:
    """读取整份 ACCOUNTS.json 为 dict；损坏配置必须显式失败。"""
    path = path or ACCOUNTS_PATH
    if not path.exists():
        return {}
    raw = _read_json_file(path, what="accounts")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return {"accounts": raw}
    raise ConfigError("accounts 顶层必须是数组或对象")


def _normalize_oauth_state_entry(data: Any) -> dict[str, Any]:
    """规范化单个 OAuth 账号状态条目。"""
    if not isinstance(data, dict):
        return {}
    state = str(data.get("state") or "").strip()
    if not state:
        return {}
    return {
        "state": state,
        "username": str(data.get("username") or ""),
        "updated_at": str(data.get("updated_at") or ""),
    }


def normalize_oauth_states(states: Any) -> dict[str, dict[str, Any]]:
    """兼容旧/新 oauth_states，统一为 {provider: {accounts: {account: {...}}}}。"""
    if not isinstance(states, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for prov, data in states.items():
        pk = normalize_oauth_provider(prov)
        if not pk or not isinstance(data, dict):
            continue
        accounts: dict[str, dict[str, Any]] = {}
        raw_accounts = data.get("accounts")
        if isinstance(raw_accounts, dict):
            for account, state_data in raw_accounts.items():
                entry = _normalize_oauth_state_entry(state_data)
                if entry:
                    accounts[normalize_oauth_account(account)] = entry
        # 旧格式：oauth_states.{provider}.state → 默认账号 default
        legacy_entry = _normalize_oauth_state_entry(data)
        if legacy_entry:
            accounts.setdefault(DEFAULT_OAUTH_ACCOUNT, legacy_entry)
        if accounts:
            out[pk] = {"accounts": accounts}
    return out


def load_oauth_states(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """读取顶层 oauth_states，并兼容旧格式迁移为多账号结构。"""
    full = _read_full(path)
    return normalize_oauth_states(full.get("oauth_states"))


def oauth_account_state(provider: str, account: str = DEFAULT_OAUTH_ACCOUNT, path: Path | None = None) -> dict[str, Any]:
    """取某 provider/account 的 OAuth 状态条目；无则返回 {}。"""
    if isinstance(account, Path):  # 兼容旧 oauth_state_text(provider, path) 位置参数写法
        path = account
        account = DEFAULT_OAUTH_ACCOUNT
    prov = normalize_oauth_provider(provider)
    if not prov:
        return {}
    account_key = normalize_oauth_account(account)
    states = load_oauth_states(path)
    return dict(((states.get(prov) or {}).get("accounts") or {}).get(account_key) or {})


def list_oauth_accounts(provider: str, path: Path | None = None) -> list[str]:
    """列出某 provider 已保存的 OAuth 账号名；默认账号排在第一位。"""
    prov = normalize_oauth_provider(provider)
    if not prov:
        return []
    accounts = ((load_oauth_states(path).get(prov) or {}).get("accounts") or {})
    names = sorted(str(name) for name in accounts.keys())
    if DEFAULT_OAUTH_ACCOUNT in names:
        names.remove(DEFAULT_OAUTH_ACCOUNT)
        names.insert(0, DEFAULT_OAUTH_ACCOUNT)
    return names


def oauth_state_text(provider: str, account: str = DEFAULT_OAUTH_ACCOUNT, path: Path | None = None) -> str:
    """取某 provider/account 的共享登录态 base64 文本；无则空串。"""
    return str(oauth_account_state(provider, account, path).get("state") or "")


def _document_metadata(path: Path) -> dict[str, Any]:
    """保留 ACCOUNTS.json 顶层未知元数据，避免局部更新时丢失。"""
    full = _read_full(path)
    if not isinstance(full, dict):
        return {}
    return {key: value for key, value in full.items() if key not in {"accounts", "oauth_states"}}


def _write_accounts_with_oauth(path: Path, accounts: list[dict[str, Any]], oauth_states: dict[str, dict[str, Any]]) -> None:
    payload: dict[str, Any] = _document_metadata(path)
    payload["accounts"] = accounts
    oauth = normalize_oauth_states(oauth_states)
    if oauth:
        payload["oauth_states"] = oauth
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def save_oauth_state(
    provider: str,
    account: str,
    state: str | None = None,
    username: str = "",
    path: Path | None = None,
) -> None:
    """局部更新某 provider/account 的共享登录态，保留其它 provider/account 与 accounts。

    兼容旧调用 save_oauth_state(provider, state)；内部新代码应显式传 account。
    """
    if state is None:
        state = account
        account = DEFAULT_OAUTH_ACCOUNT
    elif len(str(account or "")) > 200:
        # 兼容旧调用 save_oauth_state(provider, state, username)。
        username = str(state or "")
        state = account
        account = DEFAULT_OAUTH_ACCOUNT

    prov = normalize_oauth_provider(provider)
    if not prov:
        raise ValueError(f"未知 OAuth 提供商：{provider}")
    account_key = normalize_oauth_account(account)
    path = path or ACCOUNTS_PATH
    with _file_lock(path):
        accounts = _account_entries(path)
        oauth = load_oauth_states(path)
        provider_bucket = oauth.setdefault(prov, {"accounts": {}})
        provider_accounts = provider_bucket.setdefault("accounts", {})
        provider_accounts[account_key] = {
            "state": str(state or "").strip(),
            "username": username,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        _write_accounts_with_oauth(path, accounts, oauth)


def delete_oauth_state(provider: str, account: str = DEFAULT_OAUTH_ACCOUNT, path: Path | None = None) -> bool:
    """删除某 provider/account 的共享登录态；删除成功返回 True。"""
    prov = normalize_oauth_provider(provider)
    if not prov:
        return False
    account_key = normalize_oauth_account(account)
    path = path or ACCOUNTS_PATH
    with _file_lock(path):
        oauth = load_oauth_states(path)
        accounts = _account_entries(path)
        provider_accounts = ((oauth.get(prov) or {}).get("accounts") or {})
        if account_key not in provider_accounts:
            return False
        provider_accounts.pop(account_key, None)
        if provider_accounts:
            oauth[prov] = {"accounts": provider_accounts}
        else:
            oauth.pop(prov, None)
        _write_accounts_with_oauth(path, accounts, oauth)
    return True


def load_accounts(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """读取 ACCOUNTS.json，返回 {匹配键 -> 凭据/状态}；同时建立 name 与 base_url 两套索引。
    
    注意：此函数返回索引格式，用于向后兼容。新代码应使用 load_unified_accounts()。
    """
    path = path or ACCOUNTS_PATH
    if not path.exists():
        return {}
    raw = _read_json_file(path, what="accounts")

    if isinstance(raw, dict) and "accounts" in raw:
        raw = raw["accounts"]

    entries: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key.startswith("_") or key == "oauth_states" or not isinstance(value, dict):
                continue  # 跳过 _说明 / 顶层 oauth_states 之类的非账号键
            entry = {"name": value.get("name") or key}
            entry.update(value)
            entries.append(entry)
    elif isinstance(raw, list):
        if any(not isinstance(item, dict) for item in raw):
            raise ConfigError("accounts 数组中的每一项都必须是对象")
        entries = list(raw)
    else:
        raise ConfigError("accounts 顶层必须是数组、对象映射或包含 accounts 的对象")

    index: dict[str, dict[str, Any]] = {}
    for entry in entries:
        # 保留所有字段（含凭据 + 新正交三维站点配置字段）
        cred: dict[str, Any] = {field: str(entry.get(field) or "") for field in CRED_FIELDS}
        if "enabled" in entry:
            cred["enabled"] = parse_enabled(entry.get("enabled"), True)
        # 保留站点配置字段（新三维 + 旧字段兼容）
        for field in CONFIG_FIELDS:
            if field in entry:
                cred[field] = entry[field]
        name = _norm_key(str(entry.get("name") or ""))
        base = _norm_key(normalize_base_url(str(entry.get("base_url") or "")))
        if name:
            index[f"name:{name}"] = cred
        if base:
            index[f"url:{base}"] = cred
    return index


def credentials_for(name: str, base_url: str, accounts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """按 name 优先、base_url 兜底，查出某站点的凭据/状态；查不到返回空凭据。"""
    by_name = accounts.get(f"name:{_norm_key(name)}")
    if by_name:
        return by_name
    by_url = accounts.get(f"url:{_norm_key(normalize_base_url(base_url))}")
    if by_url:
        return by_url
    return {field: "" for field in CRED_FIELDS}


def load_unified_accounts(path: Path | None = None, sites_path: Path | None = None) -> list[dict[str, Any]]:
    """读取统一账号配置；旧格式会从 sites.json 补全站点字段。

    读取后若检测到 ACCOUNTS.json 仍是旧格式（含 type/checkin_mode），自动迁移写回新三维格式。
    """
    entries = [_normalize_account_entry(entry) for entry in _account_entries(path)]
    legacy_sites = [_normalize_account_entry(site) for site in load_raw_sites(sites_path)]

    site_by_name = {f"name:{_norm_key(str(site.get('name') or ''))}": site for site in legacy_sites if site.get("name")}
    site_by_url = {f"url:{_norm_key(normalize_base_url(str(site.get('base_url') or '')))}": site for site in legacy_sites if site.get("base_url")}

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        name = str(entry.get("name") or "")
        base_url = normalize_base_url(str(entry.get("base_url") or ""))
        site = site_by_name.get(f"name:{_norm_key(name)}") or site_by_url.get(f"url:{_norm_key(base_url)}") or {}
        row = site.copy()
        row.update(entry)
        row = _normalize_account_entry(row)
        if not row.get("base_url"):
            continue
        merged.append(row)
        if row.get("name"):
            seen.add(f"name:{_norm_key(str(row.get('name')))}")
        if row.get("base_url"):
            seen.add(f"url:{_norm_key(str(row.get('base_url')))}")

    if not entries:
        for site in legacy_sites:
            row = _normalize_account_entry(site)
            if row.get("base_url"):
                merged.append(row)

    # 自动迁移：若原文件仍是旧格式，写回新三维格式（仅当来自 ACCOUNTS.json 且确有改动）
    _maybe_migrate_accounts_file(path, merged)
    return merged


# 迁移写回时持久化的字段顺序（凭据/可选字段仅在非空时写入）
_PERSIST_ORDER = (
    "name",
    "base_url",
    "site_profile",
    "auth_method",
    "checkin_action",
    "script",
    "script_args",
    "script_timeout",
    "oauth_provider",
    "oauth_account",
    "api_variant",
    "enabled",
    "user_id",
    "access_token",
    "cookie",
)
_PERSIST_OPTIONAL = (
    "cookie_file",
    "referer_path",
    "browser_profile",
    "login_selector",
    "proxy",
    "auto_refresh_cookie",
)
_KNOWN_ACCOUNT_FIELDS = set(CONFIG_FIELDS) | set(CRED_FIELDS) | {
    "url",
    "authorization",
    "new_api_user",
}


def _account_to_persist(row: dict[str, Any]) -> dict[str, Any]:
    """把内存行整理为写回 ACCOUNTS.json 的紧凑条目。

    - 保留新三维 + 非空凭据/可选字段；
    - OAuth 登录 / relogin 站点：写 oauth_provider + oauth_account；
    - relogin 站点不落盘 browser_state；非 relogin 的 browser/oauth 可保存站点级 browser_state。
    """
    out: dict[str, Any] = {
        str(key): value
        for key, value in row.items()
        if key not in _KNOWN_ACCOUNT_FIELDS and not str(key).startswith("__")
    }
    needs_oauth = row.get("auth_method") == "oauth" or row.get("checkin_action") == "relogin"
    for field in _PERSIST_ORDER:
        if field == "enabled":
            out["enabled"] = bool(row.get("enabled", True))
            continue
        if field == "api_variant":
            # 仅 newapi + api 且非默认 auto 时写入
            if row.get("site_profile") == "newapi" and row.get("checkin_action") == "api":
                variant = str(row.get("api_variant") or "auto").strip().lower()
                if variant and variant != "auto":
                    out["api_variant"] = variant
            continue
        if field == "script":
            if row.get("checkin_action") == "browser_script":
                script = str(row.get("script") or "").strip()
                if script:
                    out["script"] = script
            continue
        if field == "script_args":
            if row.get("checkin_action") == "browser_script":
                script_args = normalize_script_args(row.get("script_args"))
                if script_args:
                    out["script_args"] = script_args
            continue
        if field == "script_timeout":
            if row.get("checkin_action") == "browser_script":
                timeout = parse_script_timeout(row.get("script_timeout"), 120)
                if timeout != 120:
                    out["script_timeout"] = timeout
            continue
        if field == "oauth_provider":
            if needs_oauth:
                out["oauth_provider"] = normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
            continue
        if field == "oauth_account":
            if needs_oauth:
                out["oauth_account"] = normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
            continue
        value = row.get(field)
        if field in CRED_FIELDS:
            value = str(value or "")
            if value:
                out[field] = value
        elif value not in (None, ""):
            out[field] = value

    is_relogin = row.get("checkin_action") == "relogin"
    if not is_relogin:
        # 非 relogin：browser/oauth 的站点级 browser_state 可内联（如 sub2api browser 刷新 token）
        bs = str(row.get("browser_state") or "").strip()
        if bs and row.get("auth_method") in {"browser", "oauth"}:
            out["browser_state"] = bs

    for field in _PERSIST_OPTIONAL:
        value = str(row.get(field) or "").strip()
        if value:
            out[field] = value
    return out


def _is_legacy_file(path: Path) -> bool:
    """检测 ACCOUNTS.json 是否需要迁移写回。

    触发条件（任一）：
    - 含旧字段 type / checkin_mode / provider / mode；
    - 旧 browser+relogin / browser_oauth 需要迁移为 oauth+relogin；
    - relogin 站点仍内联 browser_state（需清理，不能当作共享 OAuth 态）；
    - OAuth 登录 / relogin 站点缺 oauth_provider 或 oauth_account；
    - 顶层 oauth_states 仍是旧 provider.state 格式。
    """
    raw = _read_json_file(path, what="accounts")
    if isinstance(raw, dict) and isinstance(raw.get("oauth_states"), dict):
        for data in raw["oauth_states"].values():
            if isinstance(data, dict) and "state" in data:
                return True
    accounts = raw.get("accounts") if isinstance(raw, dict) and "accounts" in raw else raw
    items: list[Any] = []
    if isinstance(accounts, list):
        items = accounts
    elif isinstance(accounts, dict):
        items = [v for v in accounts.values() if isinstance(v, dict)]
    for item in items:
        if not isinstance(item, dict):
            continue
        if any(k in item for k in ("type", "checkin_mode", "provider", "mode")):
            return True
        action = str(item.get("checkin_action") or "").strip().lower()
        auth = str(item.get("auth_method") or "").strip().lower()
        if auth in {"browser_oauth", "relogin", "oauth_relogin"} or (auth == "browser" and action == "relogin"):
            return True
        if action == "relogin" or auth == "oauth":
            if action == "relogin" and str(item.get("browser_state") or "").strip():
                return True
            if not normalize_oauth_provider(item.get("oauth_provider")):
                return True
            if not str(item.get("oauth_account") or item.get("oauth_account_id") or "").strip():
                return True
    return False


def _collect_oauth_states(merged: list[dict[str, Any]], existing: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """保留已有共享 OAuth 登录态，并补齐 relogin 行的 provider/account。

    旧版 relogin 行里的 browser_state 是站点级浏览器凭证，不能迁移为共享
    OAuth 登录态；否则会把 agentrouter.org 等站点 Cookie 混进 oauth_states，
    导致 OAuth 重登复用错误会话。
    """
    oauth: dict[str, dict[str, Any]] = normalize_oauth_states(existing)
    for row in merged:
        if row.get("checkin_action") != "relogin":
            continue
        prov = normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
        account = normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
        row["auth_method"] = "oauth"
        row["oauth_provider"] = prov  # 回填内存，供 persist 与运行时使用
        row["oauth_account"] = account
        row["browser_state"] = ""
    return oauth


def _maybe_migrate_accounts_file(path: Path | None, merged: list[dict[str, Any]]) -> None:
    """若 ACCOUNTS.json 需迁移，写回新格式（保留共享 oauth_states，relogin 不写 browser_state）。"""
    target = path or ACCOUNTS_PATH
    if not merged or not target.exists():
        return
    if not _is_legacy_file(target):
        return
    try:
        with _file_lock(target):
            if not _is_legacy_file(target):
                return
            existing_oauth = load_oauth_states(target)
            oauth_states = _collect_oauth_states(merged, existing_oauth)
            account_list = [_account_to_persist(row) for row in merged]
            payload: dict[str, Any] = _document_metadata(target)
            payload["accounts"] = account_list
            if oauth_states:
                payload["oauth_states"] = oauth_states
            _atomic_write_text(target, json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[INFO] 已将 {target.name} 迁移为新格式（三维字段 + 共享 oauth_states）")
    except Exception as exc:
        print(f"[WARN] 自动迁移 {target.name} 失败：{exc}")


def build_github_secret_payload(
    accounts: dict[str, dict[str, Any]] | list[dict[str, Any]],
    oauth_states: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成适合 GitHub Secret: ACCOUNTS 的最小化配置。

    该函数只返回内存 payload，不读写本地文件：
    - 只导出启用站点；
    - 只保留运行签到需要的字段；
    - 顶层 oauth_states 只保留启用 OAuth/relogin 站点实际引用的 state。
    """
    if isinstance(accounts, dict):
        account_list: list[dict[str, Any]] = []
        for name, data in accounts.items():
            if str(name).startswith("_") or name == "oauth_states" or not isinstance(data, dict):
                continue
            entry = {"name": data.get("name") or name}
            entry.update(data)
            account_list.append(entry)
    elif isinstance(accounts, list):
        account_list = [entry for entry in accounts if isinstance(entry, dict)]
    else:
        raise ValueError("accounts 必须是字典或列表")

    source_oauth = normalize_oauth_states(oauth_states or {})
    exported_accounts: list[dict[str, Any]] = []
    needed_oauth: set[tuple[str, str]] = set()

    for raw_entry in account_list:
        row = _normalize_account_entry(raw_entry)
        if not parse_enabled(row.get("enabled"), True):
            continue

        base_url = normalize_base_url(str(row.get("base_url") or row.get("url") or ""))
        if not base_url:
            continue

        site_profile = str(row.get("site_profile") or row.get("type") or row.get("provider") or "newapi").strip().lower()
        if site_profile not in KNOWN_PROFILES:
            site_profile = "newapi"
        checkin_action = str(row.get("checkin_action") or "api").strip().lower()
        if checkin_action not in KNOWN_ACTIONS:
            checkin_action = "api"
        auth_method = str(row.get("auth_method") or "cookie").strip().lower()
        if auth_method not in KNOWN_AUTH_METHODS:
            auth_method = "access_token" if str(row.get("access_token") or "").strip() else "cookie"
        if checkin_action == "relogin":
            auth_method = "oauth"

        out: dict[str, Any] = {
            "name": str(row.get("name") or base_url).strip() or base_url,
            "base_url": base_url,
            "site_profile": site_profile,
            "auth_method": auth_method,
            "checkin_action": checkin_action,
        }

        if site_profile == "newapi" and checkin_action == "api":
            api_variant = str(row.get("api_variant") or "auto").strip().lower()
            if api_variant and api_variant != "auto":
                out["api_variant"] = api_variant

        if checkin_action == "browser_script":
            script = str(row.get("script") or "").strip()
            if script:
                out["script"] = script
            script_args = normalize_script_args(row.get("script_args"))
            if script_args:
                out["script_args"] = script_args
            out["script_timeout"] = parse_script_timeout(row.get("script_timeout"), 120)

        user_id = str(row.get("user_id") or row.get("new_api_user") or "").strip()
        if user_id:
            out["user_id"] = user_id

        access_token = str(row.get("access_token") or row.get("authorization") or "").strip()
        if auth_method == "access_token" and access_token:
            out["access_token"] = access_token

        cookie = str(row.get("cookie") or "").strip()
        if auth_method == "cookie" and cookie:
            out["cookie"] = cookie

        browser_state = str(row.get("browser_state") or "").strip()
        if auth_method == "browser" and browser_state:
            out["browser_state"] = browser_state

        if auth_method == "oauth" or checkin_action == "relogin":
            oauth_provider = normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
            oauth_account = normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
            out["oauth_provider"] = oauth_provider
            out["oauth_account"] = oauth_account
            needed_oauth.add((oauth_provider, oauth_account))

        proxy = str(row.get("proxy") or "").strip()
        if proxy:
            out["proxy"] = proxy

        exported_accounts.append(out)

    payload: dict[str, Any] = {"accounts": exported_accounts}
    exported_oauth: dict[str, dict[str, Any]] = {}
    for provider, account in sorted(needed_oauth):
        entry = (((source_oauth.get(provider) or {}).get("accounts") or {}).get(account) or {})
        state_text = str(entry.get("state") or "").strip()
        if not state_text:
            continue
        provider_bucket = exported_oauth.setdefault(provider, {"accounts": {}})
        provider_bucket["accounts"][account] = {"state": state_text}
    if exported_oauth:
        payload["oauth_states"] = exported_oauth
    return payload



def save_accounts(
    accounts: dict[str, dict[str, Any]] | list[dict[str, Any]],
    path: Path | None = None,
    oauth_states: dict[str, dict[str, Any]] | None = None,
) -> None:
    """写回 ACCOUNTS.json（同时保留/更新顶层共享 oauth_states）。

    支持两种账号输入格式：
    - 字典（旧格式，向后兼容）：{name: {user_id, access_token, ...}}
    - 列表（新格式，推荐）：[{name, base_url, site_profile, ...}, ...]

    oauth_states 为 None 时保留磁盘上已有的共享登录态；否则用传入值覆盖。
    写入统一使用 {"accounts": [...], "oauth_states": {...}} 形态。
    """
    path = path or ACCOUNTS_PATH
    if isinstance(accounts, dict):
        # 旧格式（字典）转为数组
        account_list = []
        for name, data in accounts.items():
            if isinstance(data, dict):
                entry = {"name": name}
                entry.update(data)
                account_list.append(entry)
    elif isinstance(accounts, list):
        account_list = accounts
    else:
        raise ValueError("accounts 必须是字典或列表")

    with _file_lock(path):
        if oauth_states is None:
            oauth_states = load_oauth_states(path)
        else:
            oauth_states = normalize_oauth_states(oauth_states)

        persisted = [_account_to_persist(_normalize_account_entry(entry)) for entry in account_list if isinstance(entry, dict)]
        payload: dict[str, Any] = _document_metadata(path)
        payload["accounts"] = persisted
        if oauth_states:
            payload["oauth_states"] = oauth_states
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def _unique_account_index(entries: list[dict[str, Any]], name: str, base_url: str) -> int | None:
    """按规范化 base_url + name 定位唯一账号；歧义时拒绝写入。"""
    name_key = _norm_key(str(name or ""))
    base_key = _norm_key(normalize_base_url(str(base_url or "")))
    matches: list[int] = []
    for index, entry in enumerate(entries):
        entry_name = _norm_key(str(entry.get("name") or ""))
        entry_base = _norm_key(normalize_base_url(str(entry.get("base_url") or entry.get("url") or "")))
        if base_key and name_key:
            matched = entry_base == base_key and entry_name == name_key
        elif base_key:
            matched = entry_base == base_key
        else:
            matched = bool(name_key and entry_name == name_key)
        if matched:
            matches.append(index)
    if len(matches) > 1:
        raise ConfigError(f"账号身份不唯一，拒绝更新：name={name!r}, base_url={base_url!r}")
    return matches[0] if matches else None


def update_account_auth_data(
    name: str,
    base_url: str,
    access_token: str = "",
    browser_state: str = "",
    path: Path | None = None,
) -> bool:
    """按 name/base_url 更新 ACCOUNTS.json 中某站点的 token/state。"""
    token = str(access_token or "").strip()
    state_text = str(browser_state or "").strip()
    if not token and not state_text:
        return False
    path = path or ACCOUNTS_PATH
    with _file_lock(path):
        entries = _account_entries(path)
        if not entries:
            return False
        index = _unique_account_index(entries, name, base_url)
        if index is None:
            return False
        entry = entries[index]
        changed = False
        if token and str(entry.get("access_token") or "").strip() != token:
            entry["access_token"] = token
            changed = True
        if state_text and str(entry.get("browser_state") or "").strip() != state_text:
            entry["browser_state"] = state_text
            changed = True
        if not changed:
            return False
        save_accounts(entries, path=path, oauth_states=load_oauth_states(path))
    return True


def update_account_access_token(
    name: str,
    base_url: str,
    access_token: str,
    path: Path | None = None,
) -> bool:
    """按 name/base_url 更新 ACCOUNTS.json 中某站点的 access_token。

    用于 Sub2API 这类短期 JWT：浏览器刷新出新 token 后立即写回，
    避免下次自动签到继续使用过期 access_token。保留顶层 oauth_states。
    """
    token = str(access_token or "").strip()
    if not token:
        return False
    path = path or ACCOUNTS_PATH
    with _file_lock(path):
        entries = _account_entries(path)
        if not entries:
            return False
        index = _unique_account_index(entries, name, base_url)
        if index is None:
            return False
        entry = entries[index]
        if str(entry.get("access_token") or "").strip() == token:
            return False
        entry["access_token"] = token
        save_accounts(entries, path=path, oauth_states=load_oauth_states(path))
    return True


def save_sites(sites: list[dict[str, Any]], path: Path | None = None) -> None:
    """以 {"sites": [...]} 形态写回 sites.json（GUI 使用）。"""
    path = path or SITES_CONFIG_PATH
    payload = {"sites": sites}
    with _file_lock(path):
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))
