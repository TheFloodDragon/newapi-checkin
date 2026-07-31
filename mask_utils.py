#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志脱敏工具：在打印到控制台 / CI 日志前掩码敏感凭据。

借鉴 newapi-ai-check-in/utils/mask_utils.py 的思路，但只用标准库 re，
针对本项目实际会出现在输出里的凭据形态（Cookie / Bearer / cf_clearance 等）。
"""

from __future__ import annotations

import re
from typing import Any

# 形如 key=value 的敏感 Cookie 字段（保留键名，掩码值）
_COOKIE_KEYS = (
    "session",
    "newapi_session",
    "new-api-session",
    "new_api_session",
    "cf_clearance",
    "__cf_bm",
    "acw_tc",
    "acw_sc__v2",
    "cdn_sec_tc",
)


_SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "browser_state",
    "cookie",
    "oauth_state",
    "password",
    "refresh_token",
    "secret",
    "state",
    "token",
}

# 精确表挡不住新增字段：实测 api_key / client_secret / proxy_password 会被原样
# 输出。凭据字段名有稳定的后缀习惯，按后缀判断才能覆盖尚未出现的键。
# 判断前先去掉下划线/连字符，让 api_key、api-key、apiKey 归一到同一形态。
_SENSITIVE_SUFFIXES = (
    "token",
    "secret",
    "password",
    "passwd",
    "apikey",
    "privatekey",
    "publickey",
    "credential",
    "credentials",
    "cookie",
)
# 出现在任意位置即视为敏感（cookie 常以 site_cookie / cookies 形式出现）。
_SENSITIVE_CONTAINS = ("cookie",)


def is_sensitive_key(key: str) -> bool:
    """字段名是否应整体脱敏。

    先做规范化（小写 + 去掉下划线/连字符），再按精确表、后缀和包含关系判断，
    这样 api_key / api-key / apiKey / client_secret / proxy_password 都能覆盖。
    """
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    if normalized.replace("-", "_") in _SENSITIVE_KEYS:
        return True
    compact = normalized.replace("_", "").replace("-", "")
    if compact.endswith(_SENSITIVE_SUFFIXES):
        return True
    return any(marker in compact for marker in _SENSITIVE_CONTAINS)


def _mask_value(value: str) -> str:
    """保留首尾各 4 位，中间用 • 替换；过短则整体掩码。"""
    value = value.strip()
    if len(value) <= 8:
        return "•" * len(value) if value else value
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


# 预编译脱敏正则：mask_secrets 会被每行输出/每个结果字段调用，
# 预编译避免每次重复编译，降低脱敏开销。
_COOKIE_PATTERNS = tuple(
    re.compile(rf"({re.escape(key)}=)([^;\s\"',]+)", re.IGNORECASE) for key in _COOKIE_KEYS
)
# opaque token 可能含 base64 的 + / =：旧字符集只吃 [A-Za-z0-9._-]，
# "Bearer abc+secret/xyz=" 只掩码到 abc，后半段原样泄露（已实测）。
_BEARER_RE = re.compile(r"(Bearer\s+)(\S+)", re.IGNORECASE)
_FIELD_RE = re.compile(
    r"(?i)([\"']?(?:access_token|refresh_token|browser_state|oauth_state|password|passwd"
    r"|secret|client_secret|token|cookie|state|api[_-]?key|private[_-]?key)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s,;\"'&}]+)"
)
_AUTH_RE = re.compile(r"(Authorization[\"']?\s*[:=]\s*[\"']?)(\S+)", re.IGNORECASE)
# 代理 URL 不只有 http(s)：CLI 明确支持 socks5，旧正则完全不匹配，
# socks5://user:pass@host 会原样进日志（已实测）。用户名同样属于凭据，一并隐藏。
_URL_CRED_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^\s/@:]+):([^\s/@]*)@")
_JWT_RE = re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?)\b")
_SK_RE = re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b", re.IGNORECASE)


def _mask_group2(m: re.Match) -> str:
    return m.group(1) + _mask_value(m.group(2))


def _mask_group1(m: re.Match) -> str:
    return _mask_value(m.group(1))


def mask_secrets(text: str) -> str:
    """掩码文本中的 Cookie 值、Bearer token、Authorization 头等。"""
    if not text:
        return text

    # 1) key=value 形式的敏感 Cookie 字段
    for pattern in _COOKIE_PATTERNS:
        text = pattern.sub(_mask_group2, text)

    # 2) Bearer <token>
    text = _BEARER_RE.sub(_mask_group2, text)

    # 3) JSON / repr / query-string 中的常见敏感字段。
    text = _FIELD_RE.sub(_mask_group2, text)

    # 4) Authorization 头整行（含可能的 sk-... token）
    text = _AUTH_RE.sub(_mask_group2, text)

    # 5) URL 中的 user:password@ 认证信息（代理或误配的站点 URL）。
    #    用户名也可能是账号标识，同样掩码，不再原样保留。
    text = _URL_CRED_RE.sub(
        lambda m: f"{m.group(1)}<redacted>:<redacted>@",
        text,
    )

    # 6) 即使没有字段名，也掩码常见 JWT 和 sk-* 凭据。
    text = _JWT_RE.sub(_mask_group1, text)
    text = _SK_RE.sub(_mask_group1, text)

    return text


def sanitize_data(value: Any, *, key: str = "") -> Any:
    """递归清理将要写入日志、stdout 或结果文件的数据。"""
    if is_sensitive_key(key):
        if value in (None, ""):
            return value
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): sanitize_data(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_data(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_data(item) for item in value]
    if isinstance(value, str):
        return mask_secrets(value)
    return value
