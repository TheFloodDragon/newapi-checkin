#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全局配置常量。

集中管理超时、重试、WAF、文件锁等可调参数，避免硬编码散落在各模块。

所有数值都可用环境变量覆盖，变量名为 ``CHECKIN_`` + 常量名，例如：

    CHECKIN_HTTP_REQUEST=60        # Timeouts.HTTP_REQUEST
    CHECKIN_BROWSER_TASK=600       # Timeouts.BROWSER_TASK
    CHECKIN_WAF_BLOCK_THRESHOLD=3  # WAFConfig.BLOCK_THRESHOLD

覆盖在模块导入时一次性读取。取值非法（非数字、非有限、越界）时忽略该环境变量
并沿用默认值，避免一个手误的环境变量让所有任务瞬间超时。

每个数值都有显式上限：``inf`` / ``nan`` / 超大值必须在这里挡住，否则会一路传到
``subprocess.run(timeout=...)``（Windows 上 ``float('inf')`` 直接抛
OverflowError）或额度汇总里，把一次配置手误变成运行时崩溃。
"""

from __future__ import annotations

import math
import os
import sys

_ENV_PREFIX = "CHECKIN_"


def _warn(name: str, raw: str, reason: str) -> None:
    """非法覆盖只忽略一次并留痕：静默回落会让用户以为覆盖已生效。"""
    print(
        f"[WARN] 环境变量 {_ENV_PREFIX}{name}={raw!r} {reason}，已忽略并沿用默认值。",
        file=sys.stderr,
    )


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    """读取整数型环境变量覆盖；非法或越界时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        _warn(name, raw, "不是合法整数")
        return default
    if value < minimum:
        _warn(name, raw, f"小于下限 {minimum}")
        return default
    if maximum is not None and value > maximum:
        _warn(name, raw, f"超过上限 {maximum}")
        return default
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    """读取浮点型环境变量覆盖；非法、非有限或越界时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        _warn(name, raw, "不是合法浮点数")
        return default
    if not math.isfinite(value):
        # inf / nan 会被 subprocess.run(timeout=) 与额度比较静默接受，
        # 直到运行期才炸；必须在配置边界拒绝。
        _warn(name, raw, "不是有限数值（inf/nan）")
        return default
    if value <= minimum:
        _warn(name, raw, f"不大于下限 {minimum}")
        return default
    if maximum is not None and value > maximum:
        _warn(name, raw, f"超过上限 {maximum}")
        return default
    return value


def _env_flag(name: str, default: bool) -> bool:
    """读取布尔型环境变量覆盖；无法识别时沿用默认值。"""
    raw = os.environ.get(_ENV_PREFIX + name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    _warn(name, raw, "不是布尔值")
    return default


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
    HTTP_REQUEST: int = _env_int("HTTP_REQUEST", 30, maximum=300)

    # HTTP 签到任务总超时（无浏览器路径）
    HTTP_TASK: float = _env_float("HTTP_TASK", 120.0, maximum=1800.0)

    # 浏览器签到任务总超时（browser/oauth/relogin）
    BROWSER_TASK: float = _env_float("BROWSER_TASK", 420.0, maximum=7200.0)

    # 浏览器启动 + 清理的额外开销（browser_script 任务 = 脚本超时 + 此值）
    BROWSER_STARTUP_OVERHEAD: float = _env_float(
        "BROWSER_STARTUP_OVERHEAD", 120.0, maximum=1800.0
    )

    # browser_script 默认脚本超时
    BROWSER_SCRIPT_DEFAULT: int = _env_int("BROWSER_SCRIPT_DEFAULT", 240, maximum=7200)

    # browser_script 最大脚本超时上限（防止配置错误导致任务永久挂起）
    BROWSER_SCRIPT_MAX: int = _env_int("BROWSER_SCRIPT_MAX", 3600, maximum=7200)

    # Node.js WASM PoW 辅助脚本超时（checkin_challenge.js）
    NODE_CHALLENGE: int = _env_int("NODE_CHALLENGE", 60, maximum=600)

    # OAuth 回调等待时间（等待浏览器跳转回站点）
    OAUTH_WAIT: int = _env_int("OAUTH_WAIT", 25, maximum=300)


class RetryConfig:
    """HTTP 请求重试配置。"""

    # 含首次在内的总尝试次数
    MAX_ATTEMPTS: int = _env_int("RETRY_MAX_ATTEMPTS", 3, maximum=10)

    # 指数退避基数（秒）：第 n 次失败后等待约 base * 2**n
    BACKOFF_BASE: float = _env_float("RETRY_BACKOFF_BASE", 0.8, maximum=60.0)

    # 单次退避上限（秒）
    BACKOFF_CAP: float = _env_float("RETRY_BACKOFF_CAP", 8.0, maximum=120.0)

    # 可触发重试的 HTTP 状态码（瞬时性错误）。
    # 可用 CHECKIN_RETRY_STATUS_CODES=429,503 覆盖（逗号分隔）。
    STATUS_CODES: frozenset[int] = _env_int_set(
        "RETRY_STATUS_CODES", frozenset({429, 500, 502, 503, 504})
    )


class WAFConfig:
    """WAF 绕过配置。"""

    # 单站点 WAF 求解重试次数（每次轮询一种 bypass 策略）
    RETRY_ATTEMPTS: int = _env_int("WAF_RETRY_ATTEMPTS", 4, maximum=20)

    # 连续多少次「整轮」WAF 求解失败后判定出口 IP 被持续风控（熔断）
    # 触发熔断后跳过后续求解，快速失败，避免在被风控的 IP 上空耗数分钟
    BLOCK_THRESHOLD: int = _env_int("WAF_BLOCK_THRESHOLD", 2, maximum=20)


class FileLockConfig:
    """文件锁配置（accounts_store 的跨进程读-改-写互斥）。"""

    # 默认锁获取超时（秒）。并发签到任务多时可适当放大。
    DEFAULT_TIMEOUT: float = _env_float("FILE_LOCK_TIMEOUT", 30.0, maximum=300.0)

    # Windows msvcrt.locking 锁定字节数。msvcrt 语义要求固定值，不开放覆盖。
    LOCK_SIZE: int = 1


class OutputConfig:
    """输出 / 结果配置。"""

    # worker stdout JSON 扫描上限（字节）；worker 输出通常很短，超出部分舍弃
    MAX_OUTPUT_SCAN: int = _env_int("MAX_OUTPUT_SCAN", 4096, minimum=512, maximum=1024 * 1024)


class LogConfig:
    """诊断日志配置。

    站点原始返回值是排查「签到到底被拒在哪一步」的第一手依据：光有一句
    「签到失败」时，既看不出是业务码拒绝、验证码不通过还是被 WAF 换了页面。
    默认打开，但必须限长——个别接口会回整页 HTML，原样刷进 CI 日志既没法看，
    也把真正有用的行顶掉了。
    """

    # 是否输出每次 HTTP 请求的站点原始返回值（经脱敏）
    HTTP_BODY: bool = _env_flag("LOG_HTTP_BODY", True)

    # 单条原始返回值的字符上限，超出部分截断并标注
    HTTP_BODY_MAX: int = _env_int("LOG_HTTP_BODY_MAX", 600, minimum=80, maximum=20000)


def _validate() -> None:
    """交叉约束校验：单个值合法但组合矛盾时同样会让任务必然失败。

    只告警不抛异常：这里失败会让所有入口无法导入 config，而这些组合问题
    通常仍能跑完（只是行为不符合预期），留痕比中断更合适。
    """
    if Timeouts.BROWSER_SCRIPT_DEFAULT > Timeouts.BROWSER_SCRIPT_MAX:
        print(
            f"[WARN] BROWSER_SCRIPT_DEFAULT={Timeouts.BROWSER_SCRIPT_DEFAULT} "
            f"大于 BROWSER_SCRIPT_MAX={Timeouts.BROWSER_SCRIPT_MAX}，"
            "缺省脚本超时会突破上限。",
            file=sys.stderr,
        )
    if Timeouts.HTTP_REQUEST >= Timeouts.HTTP_TASK:
        print(
            f"[WARN] HTTP_REQUEST={Timeouts.HTTP_REQUEST} 不小于 "
            f"HTTP_TASK={Timeouts.HTTP_TASK}，单次请求可能吃满整个任务预算。",
            file=sys.stderr,
        )
    if RetryConfig.BACKOFF_BASE > RetryConfig.BACKOFF_CAP:
        print(
            f"[WARN] RETRY_BACKOFF_BASE={RetryConfig.BACKOFF_BASE} 大于 "
            f"RETRY_BACKOFF_CAP={RetryConfig.BACKOFF_CAP}，退避上限失效。",
            file=sys.stderr,
        )


_validate()
