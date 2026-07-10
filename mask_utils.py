#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""日志脱敏工具：在打印到控制台 / CI 日志前掩码敏感凭据。

借鉴 newapi-ai-check-in/utils/mask_utils.py 的思路，但只用标准库 re，
针对本项目实际会出现在输出里的凭据形态（Cookie / Bearer / cf_clearance 等）。
"""

from __future__ import annotations

import re

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

    # 3) Authorization 头整行（含可能的 sk-... token）
    text = re.sub(
        r"(Authorization[\"']?\s*[:=]\s*[\"']?)(\S+)",
        lambda m: m.group(1) + _mask_value(m.group(2)),
        text,
        flags=re.IGNORECASE,
    )

    return text
