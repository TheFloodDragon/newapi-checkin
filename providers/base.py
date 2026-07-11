#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""签到 provider 的共享基础设施：正交数据模型、抽象接口、HTTP 与文本工具。

三个正交维度（见 providers/__init__.py 的组装入口）：
- site_profile：站点适配器（接口路径/请求头/响应解析/额度换算），newapi / sub2api；
- auth_method ：登录方式（如何获得已认证会话），access_token / cookie / browser / oauth；
- checkin_action：签到方式（如何触发发额度），api / relogin / visit。

本模块提供：
- SiteConfig    ：站点配置（三个正交字段 + 凭据 + 浏览器/网络参数）
- SiteProfile / ProfileClient：站点适配器抽象接口（profiles/ 实现）
- StatusInfo / UserInfo / CheckinReward：profile 解析结果的归一化模型
- CheckinResult / QueryStatus：对外统一结果模型
- ApiError：统一异常
- HTTP / Cookie / JSON 等纯标准库工具
"""

from __future__ import annotations

import gzip
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)

IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

SESSION_COOKIE_NAMES = {"session", "newapi_session", "new-api-session", "new_api_session"}

VERIFICATION_PATTERNS = ["Turnstile", "Cloudflare", "Just a moment", "安全验证", "challenge-platform"]

# 网络层重试：对瞬时性错误（429 / 5xx / 连接超时）做指数退避重试。
# 不重试 4xx（除 429）这类确定性错误，避免无意义的重复请求。
# 具体数值集中在 config.RetryConfig，这里保留同名别名以兼容既有引用。
from config import RetryConfig as _RetryConfig  # noqa: E402

RETRY_MAX_ATTEMPTS = _RetryConfig.MAX_ATTEMPTS   # 含首次在内的总尝试次数
RETRY_BACKOFF_BASE = _RetryConfig.BACKOFF_BASE   # 退避基数（秒）：第 n 次失败后等待约 base * 2**n
RETRY_BACKOFF_CAP = _RetryConfig.BACKOFF_CAP     # 单次退避上限（秒）
RETRY_STATUS_CODES = set(_RetryConfig.STATUS_CODES)

# New API 内部 quota 与 USD 换算系数：quota / 500000 = $
QUOTA_UNIT = 500_000


@dataclass
class SiteConfig:
    """站点配置（正交三维 + 凭据 + 浏览器/网络参数）。"""

    name: str
    base_url: str
    # ── 正交三维 ──
    site_profile: str = "newapi"      # 站点适配器：newapi / sub2api
    auth_method: str = "cookie"       # 登录方式：access_token / cookie / browser / oauth
    checkin_action: str = "api"       # 签到方式：api / relogin / visit / browser_script
    # ── 自定义浏览器脚本 ──
    script: str = ""
    script_args: dict[str, Any] = field(default_factory=dict)
    script_timeout: int = 120
    # ── 凭据 ──
    cookie: str = ""
    user_id: str = ""
    access_token: str = ""
    cookie_file: str = ""
    browser_state: str = ""
    # ── 浏览器 / 网络 ──
    browser_profile: str = ".browser_profile"
    login_selector: str = ""  # 旧字段，仅兼容保留；relogin 已改用 oauth_provider 拼授权 URL
    oauth_provider: str = "linuxdo"  # OAuth 共享第三方登录态：linuxdo / github
    oauth_account: str = "default"   # OAuth 账号名（provider 内多账号）
    proxy: str = ""
    referer_path: str = "/profile"
    # ── 其它 ──
    enabled: bool = True
    auto_refresh_cookie: bool = True
    # newapi + api 专用：接口变体偏好（auto=challenge 优先，legacy=旧接口优先）。
    # 仅影响首次尝试顺序，两种都会在失败时互为兜底；其它 profile 忽略。
    api_variant: str = "auto"


@dataclass
class AuthInfo:
    """HTTP 认证凭据（access_token / cookie 登录方式产出）。"""

    cookie: str = ""
    new_api_user: str = ""
    access_token: str = ""


@dataclass
class CheckinResult:
    site: str
    base_url: str
    status: str
    message: str
    detail: Any = None


@dataclass
class QueryStatus:
    """站点只读状态查询结果（不执行签到）。

    quota_usd：当前余额（已换算为美元），None 表示未知。
    checked_in：今日是否已签到，None 表示该 profile/action 无法判断。
    status：给 GUI/调度器使用的失败分类，避免所有 ok=False 都被误判为登录失效。
      success / need_login / need_verification / need_config / network_error / error
    """

    ok: bool
    quota_usd: float | None = None
    checked_in: bool | None = None
    message: str = ""
    status: str = "success"
    detail: Any = None


# ── profile 解析结果的归一化模型 ───────────────────────────────────────────────

@dataclass
class StatusInfo:
    """签到状态接口的归一化结果。"""

    checked_in_today: bool | None = None
    turnstile_required: bool = False
    quota_usd: float | None = None
    raw: Any = None


@dataclass
class UserInfo:
    """用户信息接口（/api/user/self 等）的归一化结果。"""

    quota_raw: Any = None      # 站点原始额度数值（newapi 为内部 quota，sub2api 为美元）
    username: str = ""
    raw: Any = None


@dataclass
class CheckinReward:
    """签到动作返回的归一化结果。"""

    already_done: bool = False
    quota_awarded: Any = None  # 本次获得额度（原始值）
    current_quota: Any = None  # 当前余额（原始值）
    raw: Any = None
    extra: dict[str, Any] = field(default_factory=dict)  # consecutive_days / total_* 等附加字段


class ApiError(Exception):
    def __init__(self, status: int | None, payload: Any, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload
        self.message = message
        # transient=True 表示瞬时性错误（网络失败/超时/429/5xx），可安全重试；
        # 非 JSON 响应、Cloudflare 验证、4xx 等确定性错误为 False。
        self.transient = transient


# ── 站点适配器抽象接口 ─────────────────────────────────────────────────────────

class ProfileClient(ABC):
    """单站点的已认证 HTTP 客户端，封装该 profile 的接口路径/请求头/响应解析。"""

    base_url: str = ""
    # 该 profile 的额度是否已是美元（sub2api=True，newapi=False 需 /500000 换算）
    quota_is_usd: bool = False

    def quota_to_usd(self, value: Any) -> float | None:
        """把站点原始额度换算为美元；非数字返回 None。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        return v if self.quota_is_usd else v / QUOTA_UNIT

    @abstractmethod
    def fetch_status(self) -> StatusInfo:
        """读取签到状态（今日是否已签、是否需验证、余额）。失败抛 ApiError。"""

    @abstractmethod
    def fetch_user(self) -> UserInfo:
        """读取当前用户信息（含额度）。失败抛 ApiError。"""

    @abstractmethod
    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        """执行一次签到接口调用。失败抛 ApiError。"""

    @abstractmethod
    def classify(self, error: ApiError) -> str:
        """把 ApiError 归类为 already_done / need_login / need_verification / error。"""


class BrowserAuthError(Exception):
    """浏览器刷新认证时的确定性失败（供 action 层翻译成对应状态）。

    status：need_verification（如阿里云 WAF 持续风控）/ need_login / error。
    """

    def __init__(self, status: str, message: str, *, detail: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail


class SiteProfile(ABC):
    """站点适配器：定义接口长什么样，并产出已认证的 ProfileClient。"""

    key: str = ""
    quota_is_usd: bool = False

    @abstractmethod
    def build_client(self, site: SiteConfig, auth: AuthInfo) -> ProfileClient:
        """用认证信息构造该 profile 的 HTTP 客户端。"""

    def supports_browser_refresh(self) -> bool:
        """该 profile 是否支持用浏览器登录态刷新认证（browser + api 组合）。"""
        return False

    def refresh_token_via_browser(self, site: SiteConfig) -> str | None:
        """用 browser_state 刷新出最新 access_token；不支持或失败返回 None。"""
        return None

    def refresh_auth_via_browser(self, site: SiteConfig) -> AuthInfo | None:
        """用浏览器登录态刷新出可用认证（cookie 或 access_token）。

        统一入口：token 型 profile（sub2api）默认包装 refresh_token_via_browser
        的结果为 access_token；cookie 型 profile（newapi 过 WAF）可覆写返回 cookie。
        确定性失败（如 WAF 持续风控）可抛 BrowserAuthError 表达具体状态。
        不支持或无结果返回 None。
        """
        token = self.refresh_token_via_browser(site)
        if token:
            return AuthInfo(access_token=normalize_access_token(token))
        return None


# ── 文本 / 匹配工具 ────────────────────────────────────────────────────────

def contains_any(text: str, patterns: list[str]) -> bool:
    text_lower = text.lower()
    return any(pattern.lower() in text_lower for pattern in patterns)


def payload_code(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("code") or "")
    return ""


# ── Cookie / URL / token 规范化 ────────────────────────────────────────────

def normalize_cookie(value: str) -> str:
    """标准化并去重 Cookie 字符串（重复键保留最后一个）。"""
    value = value.strip()
    if value.lower().startswith("cookie:"):
        value = value.split(":", 1)[1].strip()

    cookie_dict: dict[str, str] = {}
    for item in value.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, val = item.partition("=")
        cookie_dict[key.strip()] = val.strip()

    if cookie_dict:
        return "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    return value


def normalize_access_token(value: str) -> str:
    value = value.strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def cookie_items(cookie: str) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for item in cookie.split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        key, _, val = item.partition("=")
        items.append((key.strip(), val.strip()))
    return items


def strip_session_cookie(cookie: str) -> str:
    """保留 cf_clearance 等辅助 Cookie，移除 session 以优先走 Access token。"""
    return "; ".join(
        f"{key}={value}"
        for key, value in cookie_items(cookie)
        if key.lower() not in SESSION_COOKIE_NAMES
    )


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return value
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


# ── HTTP / JSON ────────────────────────────────────────────────────────────

def decode_response_body(body: bytes, content_encoding: str = "") -> str:
    if "gzip" in content_encoding.lower() or body.startswith(b"\x1f\x8b"):
        body = gzip.decompress(body)
    return body.decode("utf-8", "replace")


def parse_json(text: str) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        preview = text[:300]
        if contains_any(preview, VERIFICATION_PATTERNS):
            raise ApiError(None, preview, "站点要求 Cloudflare/Turnstile 验证，请先在浏览器完成验证并重新导出 Cookie。") from exc
        raise ApiError(None, preview, f"接口返回非 JSON：{preview}") from exc


def extract_message(payload: Any) -> str:
    keys = ("message", "msg", "errmsgcn", "errmsg", "error")
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if value:
                return str(value)
        data = payload.get("data")
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if value:
                    return str(value)
    return str(payload) if payload else "请求失败"


def unwrap_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _build_url_opener(proxy: str = "") -> urllib.request.OpenerDirector:
    """构造不依赖进程隐式代理环境的 opener。"""
    proxy = str(proxy or "").strip()
    if not proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        if parsed.scheme.startswith("socks"):
            raise ApiError(None, None, "标准库 HTTP 客户端不支持 SOCKS 代理，请改用 http/https 代理。")
        raise ApiError(None, None, "代理地址无效，必须是 http:// 或 https:// URL。")
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    )


def _http_request_once(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int,
    proxy: str,
) -> Any:
    """单次 HTTP 请求并解析 JSON；HTTP 错误也尽量解析 body，统一抛 ApiError。"""
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    opener = _build_url_opener(proxy)
    try:
        with opener.open(req, timeout=timeout) as response:
            text = decode_response_body(response.read(), response.headers.get("content-encoding", ""))
            return parse_json(text)
    except urllib.error.HTTPError as exc:
        text = decode_response_body(exc.read(), exc.headers.get("content-encoding", ""))
        try:
            payload = parse_json(text)
        except ApiError:
            payload = text
        transient = exc.code in RETRY_STATUS_CODES
        raise ApiError(exc.code, payload, extract_message(payload), transient=transient) from exc
    except urllib.error.URLError as exc:
        # socket 超时在部分平台会被包进 URLError.reason
        raise ApiError(None, None, f"网络请求失败：{exc.reason}", transient=True) from exc
    except TimeoutError as exc:  # 直接抛出的连接/读取超时（socket.timeout 是其别名）
        raise ApiError(None, None, f"网络请求超时：{exc}", transient=True) from exc
    except OSError as exc:
        # ssl/socket 读取超时有时表现为普通 OSError（如 "The read operation timed out"），也应重试。
        raise ApiError(None, None, f"网络请求失败：{exc}", transient=True) from exc


def _is_retryable(error: ApiError) -> bool:
    """仅瞬时性错误才重试（网络失败/超时/429/5xx）；确定性错误不重试。"""
    return error.transient


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: int = 30,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    proxy: str = "",
    retry_non_idempotent: bool = False,
) -> Any:
    """发送 HTTP 请求并解析 JSON，对可安全重放的瞬时性错误做退避重试。

    默认仅重试幂等方法；POST/PATCH 等可能产生副作用的请求只执行一次，除非
    调用方明确设置 ``retry_non_idempotent=True``。HTTP 429/5xx 与网络失败会被
    标记为瞬时错误，4xx、非 JSON 和验证页不会重试。
    """
    headers = dict(headers or {})
    method_upper = method.upper()
    retry_allowed = method_upper in IDEMPOTENT_METHODS or retry_non_idempotent
    attempts = max(1, max_attempts) if retry_allowed else 1
    last_error: ApiError | None = None
    for attempt in range(attempts):
        try:
            return _http_request_once(
                url,
                method=method_upper,
                headers=headers,
                body=body,
                timeout=timeout,
                proxy=proxy,
            )
        except ApiError as exc:
            last_error = exc
            if attempt >= attempts - 1 or not _is_retryable(exc):
                raise
            # 指数退避 + 抖动，缓解站点限流与瞬时抖动
            delay = min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** attempt))
            delay += random.uniform(0, delay * 0.25)
            time.sleep(delay)
    # 理论上不可达：循环要么 return 要么 raise
    raise last_error if last_error else ApiError(None, None, "请求失败")
