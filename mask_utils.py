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
    "password",
    "refresh_token",
    "secret",
    "state",
    "token",
}


def _mask_value(value: str) -> str:
    """保留首尾各 4 位，中间用 • 替换；过短则整体掩码。"""
    value = value.strip()
    if len(value) <= 8:
        return "•" * len(value) if value else value
    return f"{value[:4]}{'•' * 6}{value[-4:]}"


def mask_secrets(text: str) -> str:
    """掩码文本中的 Cookie 值、Bearer token、Authorization 头等。"""
    if not text:
        return text

    # 1) key=value 形式的敏感 Cookie 字段
    for key in _COOKIE_KEYS:
        text = re.sub(
            rf"({re.escape(key)}=)([^;\s\"',]+)",
            lambda m: m.group(1) + _mask_value(m.group(2)),
            text,
            flags=re.IGNORECASE,
        )

    # 2) Bearer <token>
    text = re.sub(
        r"(Bearer\s+)([A-Za-z0-9._\-]+)",
        lambda m: m.group(1) + _mask_value(m.group(2)),
        text,
        flags=re.IGNORECASE,
    )

    # 3) JSON / repr / query-string 中的常见敏感字段。
    text = re.sub(
        r"(?i)([\"']?(?:access_token|refresh_token|browser_state|password|secret|token|cookie|state)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'&}]+)",
        lambda m: m.group(1) + _mask_value(m.group(2)),
        text,
    )

    # 4) Authorization 头整行（含可能的 sk-... token）
    text = re.sub(
        r"(Authorization[\"']?\s*[:=]\s*[\"']?)(\S+)",
        lambda m: m.group(1) + _mask_value(m.group(2)),
        text,
        flags=re.IGNORECASE,
    )

    # 5) URL 中的 user:password@ 认证信息（代理或误配的站点 URL）。
    text = re.sub(
        r"(?i)(https?://)([^\s/@:]+):([^\s/@]+)@",
        lambda m: f"{m.group(1)}{m.group(2)}:<redacted>@",
        text,
    )

    # 6) 即使没有字段名，也掩码常见 JWT 和 sk-* 凭据。
    text = re.sub(
        r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?)\b",
        lambda m: _mask_value(m.group(1)),
        text,
    )
    text = re.sub(
        r"\b(sk-[A-Za-z0-9_-]{12,})\b",
        lambda m: _mask_value(m.group(1)),
        text,
        flags=re.IGNORECASE,
    )

    return text


def sanitize_data(value: Any, *, key: str = "") -> Any:
    """递归清理将要写入日志、stdout 或结果文件的数据。"""
    if key.lower() in _SENSITIVE_KEYS:
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
