#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局配置常量。

集中管理超时、重试、WAF、文件锁等可调参数，避免硬编码散落在各模块。

所有数值都可用环境变量覆盖，变量名为 ``CHECKIN_`` + 常量名，例如：

    CHECKIN_HTTP_REQUEST=60        # Timeouts.HTTP_REQUEST
    CHECKIN_BROWSER_TASK=600       # Timeouts.BROWSER_TASK
    CHECKIN_WAF_BLOCK_THRESHOLD=3  # WAFConfig.BLOCK_THRESHOLD

覆盖在模块导入时一次性读取。取值非法（非数字、<=0）时忽略该环境变量并沿用
默认值，避免一个手误的环境变量让所有任务瞬间超时。
"""

from __future__ import annotations

import os

_ENV_PREFIX = "CHECKIN_"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """读取整数型环境变量覆盖；非法或越界时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    """读取浮点型环境变量覆盖；非法或越界时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > minimum else default


def _env_int_set(name: str, default: frozenset[int]) -> frozenset[int]:
    """读取逗号分隔的整数集合覆盖（如 "429,503"）；非法或为空时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    values: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            values.add(int(part))
        except ValueError:
            return default  # 整体非法即退回默认，避免只解析出一半状态码
    return frozenset(values) if values else default


class Timeouts:
    """超时配置（单位：秒）。"""

    # 单次 HTTP 请求（urllib opener.open timeout）
    HTTP_REQUEST: int = _env_int("HTTP_REQUEST", 30)

    # HTTP 签到任务总超时（无浏览器路径）
    HTTP_TASK: float = _env_float("HTTP_TASK", 120.0)

    # 浏览器签到任务总超时（browser/oauth/relogin）
    BROWSER_TASK: float = _env_float("BROWSER_TASK", 420.0)

    # 浏览器启动 + 清理的额外开销（browser_script 任务 = 脚本超时 + 此值）
    BROWSER_STARTUP_OVERHEAD: float = _env_float("BROWSER_STARTUP_OVERHEAD", 120.0)

    # browser_script 默认脚本超时
    BROWSER_SCRIPT_DEFAULT: int = _env_int("BROWSER_SCRIPT_DEFAULT", 120)

    # browser_script 最大脚本超时上限（防止配置错误导致任务永久挂起）
    BROWSER_SCRIPT_MAX: int = _env_int("BROWSER_SCRIPT_MAX", 3600)

    # Node.js WASM PoW 辅助脚本超时（checkin_challenge.js）
    NODE_CHALLENGE: int = _env_int("NODE_CHALLENGE", 60)

    # OAuth 回调等待时间（等待浏览器跳转回站点）
    OAUTH_WAIT: int = _env_int("OAUTH_WAIT", 25)


class RetryConfig:
    """HTTP 请求重试配置。"""

    # 含首次在内的总尝试次数
    MAX_ATTEMPTS: int = _env_int("RETRY_MAX_ATTEMPTS", 3)

    # 指数退避基数（秒）：第 n 次失败后等待约 base * 2**n
    BACKOFF_BASE: float = _env_float("RETRY_BACKOFF_BASE", 0.8)

    # 单次退避上限（秒）
    BACKOFF_CAP: float = _env_float("RETRY_BACKOFF_CAP", 8.0)

    # 可触发重试的 HTTP 状态码（瞬时性错误）。
    # 可用 CHECKIN_RETRY_STATUS_CODES=429,503 覆盖（逗号分隔）。
    STATUS_CODES: frozenset[int] = _env_int_set(
        "RETRY_STATUS_CODES", frozenset({429, 500, 502, 503, 504})
    )


class WAFConfig:
    """WAF 绕过配置。"""

    # 单站点 WAF 求解重试次数（每次轮询一种 bypass 策略）
    RETRY_ATTEMPTS: int = _env_int("WAF_RETRY_ATTEMPTS", 4)

    # 连续多少次「整轮」WAF 求解失败后判定出口 IP 被持续风控（熔断）
    # 触发熔断后跳过后续求解，快速失败，避免在被风控的 IP 上空耗数分钟
    BLOCK_THRESHOLD: int = _env_int("WAF_BLOCK_THRESHOLD", 2)


class FileLockConfig:
    """文件锁配置（accounts_store 的跨进程读-改-写互斥）。"""

    # 默认锁获取超时（秒）。并发签到任务多时可适当放大。
    DEFAULT_TIMEOUT: float = _env_float("FILE_LOCK_TIMEOUT", 30.0)

    # Windows msvcrt.locking 锁定字节数。msvcrt 语义要求固定值，不开放覆盖。
    LOCK_SIZE: int = 1


class OutputConfig:
    """输出 / 结果配置。"""

    # worker stdout JSON 扫描上限（字节）；worker 输出通常很短，超出部分舍弃
    MAX_OUTPUT_SCAN: int = _env_int("MAX_OUTPUT_SCAN", 4096)
