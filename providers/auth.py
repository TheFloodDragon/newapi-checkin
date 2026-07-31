#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""登录方式（auth_method）：把站点凭据加载/规范化为统一的 AuthInfo。

四种登录方式：
- access_token：Bearer token 认证（access_token + user_id）；
- cookie      ：Cookie 认证（cookie + user_id）；
- browser     ：站点级浏览器登录态（browser_state），由 action 层在运行时还原/刷新；
- oauth       ：共享 OAuth provider/account 登录态，由 action 层显式选择并刷新。

cookie_file 三行格式（向后兼容）：第一行 Cookie 或 Access token，第二行 user_id，
第三行 Access token。本模块统一从 SiteConfig 与 cookie_file 合并出 AuthInfo。
"""

from __future__ import annotations

import sys
from pathlib import Path

import accounts_store

from .base import (
    AuthInfo,
    SiteConfig,
    normalize_access_token,
    normalize_cookie,
    strip_session_cookie,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent


def resolve_path(path_text: str) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return SCRIPT_DIR / path


def load_cookie_file(path: Path, *, write_back_cleaned: bool = True) -> AuthInfo:
    """加载凭证文件；Cookie 始终在内存清理，可选回写三行格式文件。"""
    if not path.exists():
        return AuthInfo()
    lines = path.read_text(encoding="utf-8").splitlines()
    raw_cookie = lines[0] if lines else ""
    user_id = lines[1].strip() if len(lines) > 1 else ""
    access_token = normalize_access_token(lines[2]) if len(lines) > 2 else ""

    first_line = raw_cookie.strip()
    first_line_lower = first_line.lower()
    first_line_is_access_token = first_line_lower.startswith(("authorization:", "bearer "))
    if first_line and not first_line_is_access_token and "=" not in first_line and ";" not in first_line:
        first_line_is_access_token = True

    if first_line_is_access_token:
        cookie = ""
        access_token = access_token or normalize_access_token(first_line)
    else:
        cookie = normalize_cookie(raw_cookie)

    # Cookie 始终在内存清理；仅开关启用时回写，并保留第 3 行 Access token。
    if write_back_cleaned and cookie != raw_cookie.strip() and cookie:
        try:
            extra = f"{access_token}\n" if access_token else ""
            with accounts_store.file_lock(path):
                accounts_store.atomic_write_text(path, f"{cookie}\n{user_id}\n{extra}")
            print(f"[DEBUG] 已清理 {path.name} 中的重复 Cookie 字段", file=sys.stderr)
        except (OSError, accounts_store.ConfigError) as e:
            # 回写失败不致命：本次仍用已清理的内存 Cookie 继续。
            print(f"[WARN] 无法回写清理后的 Cookie 文件 {path}: {e}", file=sys.stderr)

    return AuthInfo(cookie=cookie, new_api_user=user_id, access_token=access_token)


def load_auth(site: SiteConfig) -> AuthInfo:
    """合并 SiteConfig 与 cookie_file，产出统一 AuthInfo（适用于 access_token / cookie 登录方式）。"""
    auth = AuthInfo(
        cookie=normalize_cookie(site.cookie),
        new_api_user=site.user_id.strip(),
        access_token=normalize_access_token(site.access_token),
    )
    if site.cookie_file:
        file_auth = load_cookie_file(
            resolve_path(site.cookie_file),
            write_back_cleaned=site.auto_refresh_cookie,
        )
        auth.cookie = auth.cookie or file_auth.cookie
        auth.new_api_user = auth.new_api_user or file_auth.new_api_user
        auth.access_token = auth.access_token or file_auth.access_token
    return auth


def auth_for_method(site: SiteConfig) -> AuthInfo:
    """按 auth_method 投影凭据，只保留该登录方式真正该用的那一份。

    为什么需要：load_auth() 会把 SiteConfig 与 cookie_file 里的 cookie 和
    access_token 一起返回，而 newapi 客户端见到 token 就发 Bearer 并剥掉 session
    cookie。于是用户在 GUI 明确选了 Cookie 登录，实际生效的却是 token（已实测请求头
    带 Authorization 且 session 被剥离）。两套凭据可能属于不同账号，混用等于签到到
    另一个身份上，认证报错也无法定位是哪一份失效。

    投影只在 access_token / cookie 两种纯 HTTP 方式生效：browser / oauth 的凭据由
    action 层在运行期刷新后注入，不走这里。user_id 两种方式都保留——它是
    New-Api-User 头，不属于任何一种凭据的私有字段。

    access_token 模式不整串清空 cookie：cf_clearance / acw_tc 这类 WAF 辅助 cookie
    不是身份凭据，丢掉会让受 Cloudflare / 阿里云保护的站点从「能过 WAF」退化成拿不到
    JSON。沿用 strip_session_cookie 的既有边界，只剥离会构成第二身份的 session。

    注意：Sub2API 的 access_token / refresh_token 按设计属于「接口凭据」（见 README），
    其客户端在 auth 未给出时仍会回落 site 上的值，本函数不改变那条约定。
    """
    auth_method = (site.auth_method or "cookie").strip().lower()
    raw = load_auth(site)
    if auth_method == "cookie":
        return AuthInfo(cookie=raw.cookie, new_api_user=raw.new_api_user)
    if auth_method == "access_token":
        return AuthInfo(
            access_token=raw.access_token,
            cookie=strip_session_cookie(raw.cookie),
            new_api_user=raw.new_api_user,
        )
    return raw


def has_http_credentials(auth: AuthInfo) -> bool:
    """是否具备 HTTP 认证所需的最低凭据（cookie 或 access_token）。"""
    return bool(auth.cookie or auth.access_token)
