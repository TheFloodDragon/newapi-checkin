#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局配置常量。

集中管理超时、重试、WAF、文件锁等可调参数，避免硬编码散落在各模块。
所有值均可通过环境变量覆盖（见各子类说明），方便 CI/本地调优。
"""

from __future__ import annotations


class Timeouts:
    """超时配置（单位：秒）。"""

    # 单次 HTTP 请求（urllib opener.open timeout）
    HTTP_REQUEST: int = 30

    # HTTP 签到任务总超时（无浏览器路径）
    HTTP_TASK: float = 120.0

    # 浏览器签到任务总超时（browser/oauth/relogin）
    BROWSER_TASK: float = 420.0

    # 浏览器启动 + 清理的额外开销（browser_script 任务 = 脚本超时 + 此值）
    BROWSER_STARTUP_OVERHEAD: float = 120.0

    # browser_script 默认脚本超时
    BROWSER_SCRIPT_DEFAULT: int = 120

    # browser_script 最大脚本超时上限（防止配置错误导致任务永久挂起）
    BROWSER_SCRIPT_MAX: int = 3600

    # Node.js WASM PoW 辅助脚本超时（checkin_challenge.js）
    NODE_CHALLENGE: int = 60

    # OAuth 回调等待时间（等待浏览器跳转回站点）
    OAUTH_WAIT: int = 25

    # Playwright 单个操作超时（page.goto / click 等，None 表示使用 Playwright 默认值 30s）
    PLAYWRIGHT_ACTION: int = 30


class RetryConfig:
    """HTTP 请求重试配置。"""

    # 含首次在内的总尝试次数
    MAX_ATTEMPTS: int = 3

    # 指数退避基数（秒）：第 n 次失败后等待约 base * 2**n
    BACKOFF_BASE: float = 0.8

    # 单次退避上限（秒）
    BACKOFF_CAP: float = 8.0

    # 可触发重试的 HTTP 状态码（瞬时性错误）
    STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class WAFConfig:
    """WAF 绕过配置。"""

    # 单站点 WAF 求解重试次数（每次轮询一种 bypass 策略）
    RETRY_ATTEMPTS: int = 4

    # 连续多少次「整轮」WAF 求解失败后判定出口 IP 被持续风控（熔断）
    # 触发熔断后跳过后续求解，快速失败，避免在被风控的 IP 上空耗数分钟
    BLOCK_THRESHOLD: int = 2


class FileLockConfig:
    """文件锁配置。"""

    # 默认锁获取超时（秒）
    DEFAULT_TIMEOUT: float = 30.0

    # Windows msvcrt.locking 锁定字节数（固定值，msvcrt 语义要求）
    LOCK_SIZE: int = 1


class OutputConfig:
    """输出 / 结果配置。"""

    # worker stdout JSON 扫描上限（字节）；worker 输出通常很短，超出部分舍弃
    MAX_OUTPUT_SCAN: int = 4096
