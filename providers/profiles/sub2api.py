#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sub2API 站点适配器（SiteProfile）。

站点：Sub2API 系（如 https://sub.100xlabs.space）
API 前缀：/api/v1，统一响应 {code:0, data:{...}}
认证：Authorization: Bearer <access_token>（浏览器 localStorage 的 auth_token）

接口：
- GET  /api/v1/user/profile      → 标准 Sub2API 当前用户资料 / 余额（JWT auth_token）
- GET  /api/v1/auth/me           → 标准 Sub2API 当前登录用户（JWT auth_token）
- GET  /api/v1/usage             → 标准 Sub2API 用量记录（JWT auth_token，items[].user.balance）
- GET  /v1/usage                 → API Key 余额/用量查询（sk-* API Key，不是前端 auth_token）
- GET  /api/v1/check-in/status   → 非标准 fork 的可选签到状态扩展
- POST /api/v1/check-in          → 非标准 fork 的可选签到扩展

额度单位：直接是 USD（reward_amount=$x.xx），无需换算。
浏览器刷新：配置了 browser_state 时可用浏览器登录态刷新 auth_token。
"""

from __future__ import annotations

import http.cookiejar as http_cookiejar
import json
import sys
from typing import Any

from ..base import (
    USER_AGENT,
    ApiError,
    AuthInfo,
    CheckinReward,
    ProfileClient,
    SiteConfig,
    SiteProfile,
    StatusInfo,
    UserInfo,
    contains_any,
    extract_message,
    http_request,
    normalize_access_token,
    normalize_base_url,
    normalize_cookie,
    unwrap_data,
)
from ..base import VERIFICATION_PATTERNS as _BASE_VERIFICATION_PATTERNS

API_PREFIX = "/api/v1"

# 各 Sub2API fork 的签到端点不统一，按顺序探测（第一个可用的会被缓存复用）：
# - /check-in        ：100xLabs 等 fork 的签到扩展
# - /play/checkin    ：极速蹬（jisudeng）把签到挂在 play 模块下
# 每项为 (签到 POST 路径, 状态 GET 路径)；状态路径为 None 表示该 fork 无状态接口。
CHECKIN_ENDPOINTS: tuple[tuple[str, str | None], ...] = (
    ("/check-in", "/check-in/status"),
    ("/play/checkin", "/play/checkin/status"),
)
LOGIN_PATTERNS = ["unauthorized", "登录", "token", "expired", "invalid", "forbidden", "无效", "过期"]
# 在唯一词表基础上追加「验证 / verify」：sub2api 的 classify 先判 LOGIN 再判验证，
# token 失效类消息已被 need_login 拦截，宽泛词在此语境安全（保持既有行为）。
VERIFICATION_PATTERNS = [*_BASE_VERIFICATION_PATTERNS, "验证", "verify"]
ALREADY_DONE_PATTERNS = ["already", "已签到", "今日已", "已领取"]
UNSUPPORTED_CHECKIN_PATTERNS = ["404", "405", "not found", "no route", "route not found", "method not allowed", "不存在", "未找到"]


def _persist_refreshed_token(site: SiteConfig, token: str, refresh_token: str = "") -> None:
    """把 HTTP 刷新出的新 access_token（及轮换后的 refresh_token）写回 ACCOUNTS.json。

    持久化失败（锁超时/文件损坏/IO 错误）不影响本次签到：内存 token 仍可用。
    """
    if not token:
        return
    site.access_token = token
    if refresh_token:
        site.refresh_token = refresh_token
    try:
        import accounts_store

        accounts_store.update_account_access_token(
            site.name, site.base_url, token, refresh_token=refresh_token
        )
    except Exception:
        # 仅内存生效；下次运行会重新走一次刷新。
        pass
    try:
        import accounts_store

        accounts_store.update_account_access_token(site.name, site.base_url, token)
    except Exception:
        pass



def http_password_login(site: SiteConfig, email: str, password: str, log: Any = None) -> dict[str, str]:
    """纯 HTTP 账密登录，换取 access_token + refresh_token（不启动浏览器）。

    实测（2026-07）极速蹬等 Sub2API 站点在 /api/v1/settings/public 里可能声明
    turnstile_enabled=false，此时 /api/v1/auth/login 接受纯 HTTP 账密登录；反之
    声明为 true（或服务端仍校验）时会返回 400 TURNSTILE_VERIFICATION_FAILED，
    此时必须回落浏览器由 browser.turnstile 完成真实点击。

    另一个实测要点：这类站点会做「会话网络指纹」校验，登录与后续请求必须共享
    同一 cookie 会话，否则报 Session network fingerprint changed。因此这里先访问
    /settings/public 预热 cookie，并把整个流程放在同一个 CookieJar 上。

    返回 {"access_token": ..., "refresh_token": ...}；失败返回 {}。
    凭据只用于本次请求，不写日志、不入结果。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    email = str(email or "").strip()
    password = str(password or "")
    if not email or not password:
        return {}

    base = normalize_base_url(site.base_url)
    jar = http_cookiejar.CookieJar()
    proxy = getattr(site, "proxy", "") or ""
    verify_ssl = getattr(site, "verify_ssl", True)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": base,
        "Referer": base + "/login",
    }

    # 1) 预热：建立 cookie 会话，同时读取站点是否启用 Turnstile。
    turnstile_enabled = None
    try:
        settings = http_request(
            base + API_PREFIX + "/settings/public",
            method="GET",
            headers=headers,
            proxy=proxy,
            verify_ssl=verify_ssl,
            cookie_jar=jar,
        )
        data = unwrap_data(settings)
        if isinstance(data, dict) and "turnstile_enabled" in data:
            turnstile_enabled = bool(data.get("turnstile_enabled"))
    except ApiError:
        pass

    if turnstile_enabled:
        _log("站点声明启用 Turnstile，纯 HTTP 账密登录不可用，需回落浏览器")
        return {}

    # 2) 账密登录。
    try:
        payload = http_request(
            base + API_PREFIX + "/auth/login",
            method="POST",
            headers={**headers, "Content-Type": "application/json"},
            body=json.dumps({"email": email, "password": password}).encode("utf-8"),
            proxy=proxy,
            verify_ssl=verify_ssl,
            cookie_jar=jar,
            retry_non_idempotent=False,
        )
    except ApiError as exc:
        text = f"{exc.status or 0} {exc.message}"
        if "turnstile" in text.lower():
            _log("账密登录被 Turnstile 拦截，需回落浏览器完成人机验证")
        else:
            _log(f"纯 HTTP 账密登录失败：HTTP {text}")
        return {}

    data = unwrap_data(payload)
    if not isinstance(data, dict):
        return {}
    access = normalize_access_token(str(data.get("access_token") or ""))
    refresh = str(data.get("refresh_token") or "").strip()
    if not access:
        _log("账密登录响应未包含 access_token")
        return {}
    _log(f"纯 HTTP 账密登录成功（access_token {len(access)} 字符）")
    return {"access_token": access, "refresh_token": refresh}

def _to_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _pick_first_number(data: Any, keys: tuple[str, ...] = ("remaining", "balance", "quota")) -> float | int | None:
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                nested = _pick_first_number(value, keys)
                if nested is not None:
                    return nested
            number = _to_number(value)
            if number is not None:
                return number
        for value in data.values():
            nested = _pick_first_number(value, keys)
            if nested is not None:
                return nested
    elif isinstance(data, list):
        for item in data:
            nested = _pick_first_number(item, keys)
            if nested is not None:
                return nested
    return None


def _extract_usage_user_balance(data: Any) -> float | int | None:
    """优先从 /api/v1/usage 的 items[].user.balance 提取余额。

    用量记录里还会嵌套 api_key.quota、group.daily_limit_usd 等数字字段；不能做
    盲目递归，否则可能把 API Key 配额 0 误当成用户余额。
    """
    items: Any = None
    if isinstance(data, dict):
        items = data.get("items")
    elif isinstance(data, list):
        items = data
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        user = item.get("user")
        balance = _pick_first_number(user, ("balance", "remaining", "credit", "credits", "quota"))
        if balance is not None:
            return balance
    return None


def _extract_standard_balance(data: Any) -> float | int | None:
    """从标准 Sub2API JWT 接口返回中提取余额。

    源码中：
    - /api/v1/user/profile 直接返回 user，含 balance；
    - /api/v1/auth/me 直接返回当前用户，含 balance；
    - /api/v1/usage 返回分页 items，单条记录里包含 user.balance。
    """
    if not isinstance(data, (dict, list)):
        return None
    usage_balance = _extract_usage_user_balance(data)
    if usage_balance is not None:
        return usage_balance
    return _pick_first_number(data, ("balance", "remaining", "credit", "credits", "quota"))


def _extract_username(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("username", "name", "email", "id", "user_id"):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    user = data.get("user")
    if isinstance(user, dict):
        return _extract_username(user)
    return ""


class Sub2ApiClient(ProfileClient):
    quota_is_usd = True

    def __init__(
        self,
        site: SiteConfig,
        auth: AuthInfo,
        token_refresher: Any = None,
    ) -> None:
        self.site = site
        self.base_url = normalize_base_url(site.base_url)
        self.access_token = normalize_access_token(auth.access_token or site.access_token)
        self.cookie = normalize_cookie(auth.cookie or site.cookie)
        self._user_cache: UserInfo | None = None
        # 缓存优先的惰性刷新：仅当接口返回登录失效时，才调用一次刷新出新 token。
        self._token_refresher = token_refresher
        self._refresh_used = False
        # 签到端点探测结果缓存：(签到路径, 状态路径)。各 fork 端点不同（见
        # CHECKIN_ENDPOINTS），首次探测成功后复用，避免每次都试错一遍。
        self._checkin_endpoint: tuple[str, str | None] | None = None
        # 会话级 cookie jar：部分 Sub2API 站点（实测极速蹬）把会话绑定到网络/客户端
        # 指纹，服务端下发的 cookie 必须在后续请求带回，否则报
        # 「Session network fingerprint changed, please login again」。
        # urllib 默认不保留 cookie，因此这里显式维护一个 jar 供本客户端全程复用。
        self._cookie_jar = http_cookiejar.CookieJar()
        # 纯 HTTP 的 refresh_token 刷新（不启动浏览器）；由 profile 注入。
        self._refresh_token = str(getattr(site, "refresh_token", "") or "").strip()
        self._http_refresh_used = False

    def _refresh_via_http(self) -> str:
        """用 refresh_token 直接调 /auth/refresh 换新 access_token（不启动浏览器）。

        Sub2API 的 access_token 是短期 JWT，但 refresh_token 有效期长得多。
        只要浏览器捕获登录态时存下了 refresh_token，纯 HTTP 路径就能自行续期，
        无需为「token 过期」这一常见情况拉起 Camoufox。失败返回空串。
        """
        if not self._refresh_token or self._http_refresh_used:
            return ""
        self._http_refresh_used = True
        try:
            payload = http_request(
                self.base_url + API_PREFIX + "/auth/refresh",
                method="POST",
                headers={**self._headers(), "Content-Type": "application/json"},
                body=json.dumps({"refresh_token": self._refresh_token}).encode("utf-8"),
                proxy=self.site.proxy,
                verify_ssl=getattr(self.site, "verify_ssl", True),
                cookie_jar=self._cookie_jar,
            )
        except ApiError:
            return ""
        data = unwrap_data(payload)
        if not isinstance(data, dict):
            return ""
        token = normalize_access_token(str(data.get("access_token") or ""))
        # 服务端可能轮换 refresh_token；记下新值供本次进程后续使用。
        rotated = str(data.get("refresh_token") or "").strip()
        if rotated:
            self._refresh_token = rotated
        return token

    def _maybe_refresh_token(self, error: ApiError) -> bool:
        """接口返回登录失效时，按需刷新一次 access_token；成功刷新返回 True。

        优先纯 HTTP 的 refresh_token 续期（快、不开浏览器）；不可用时才回退到
        注入的 token_refresher（通常会启动浏览器走 OAuth/前端刷新）。
        """
        if error.transient or self.classify(error) != "need_login":
            return False

        http_token = self._refresh_via_http()
        if http_token and http_token != self.access_token:
            self.access_token = http_token
            self._user_cache = None
            _persist_refreshed_token(self.site, http_token, self._refresh_token)
            return True

        if self._token_refresher is None or self._refresh_used:
            return False
        self._refresh_used = True
        try:
            new_token = self._token_refresher()
        except Exception:
            return False
        token = normalize_access_token(str(new_token or ""))
        if not token or token == self.access_token:
            return False
        self.access_token = token
        self._user_cache = None
        return True

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.base_url,
            "Referer": self.base_url + "/",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def request(self, method: str, path: str, body: dict | None = None, *, retry_non_idempotent: bool = False) -> Any:
        url = self.base_url + API_PREFIX + path

        raw_body: bytes | None = None
        if method.upper() in {"POST", "PUT", "PATCH"}:
            raw_body = json.dumps(body or {}).encode("utf-8")

        def _once() -> Any:
            headers = self._headers()
            if raw_body is not None:
                headers["Content-Type"] = "application/json"
            payload = http_request(
                url,
                method=method,
                headers=headers,
                body=raw_body,
                proxy=self.site.proxy,
                retry_non_idempotent=retry_non_idempotent,
                verify_ssl=getattr(self.site, "verify_ssl", True),
                cookie_jar=self._cookie_jar,
            )
            # Sub2API 统一响应：{code:0, data:{...}}；code != 0 视为失败
            if isinstance(payload, dict) and "code" in payload:
                code = payload.get("code")
                if code not in (0, "0", None):
                    raise ApiError(None, payload, extract_message(payload))
            # 部分 fork 不返回 code，而是用 success:false 表达业务失败（与 newapi 一致）。
            # 不校验会把「HTTP 200 但业务失败」误当成功，最终误报「签到成功」。
            if isinstance(payload, dict) and payload.get("success") is False:
                raise ApiError(None, payload, extract_message(payload))
            return payload

        try:
            return _once()
        except ApiError as exc:
            # 登录失效（如 JWT 过期）时，用浏览器 OAuth 刷新一次新 token 再重试。
            if self._maybe_refresh_token(exc):
                return _once()
            raise

    def request_usage(self) -> Any:
        """按用户提供的余额脚本请求 {base_url}/v1/usage。"""
        if not self.access_token:
            raise ApiError(401, None, "缺少 access_token/apiKey，无法请求 /v1/usage")
        return http_request(
            self.base_url + "/v1/usage",
            method="GET",
            headers=self._headers(),
            proxy=self.site.proxy,
            verify_ssl=getattr(self.site, "verify_ssl", True),
            cookie_jar=self._cookie_jar,
        )

    @staticmethod
    def _extract_usage_balance(payload: Any) -> tuple[bool, float | int | None, str] | None:
        """实现 /v1/usage 脚本里的 extractor 等价逻辑。"""
        if not isinstance(payload, dict):
            return None
        quota = payload.get("quota") if isinstance(payload.get("quota"), dict) else {}
        remaining = _to_number(payload.get("remaining"))
        if remaining is None:
            remaining = _to_number(quota.get("remaining"))
        if remaining is None:
            remaining = _to_number(payload.get("balance"))
        if remaining is None:
            return None
        unit = str(payload.get("unit") or quota.get("unit") or "USD")
        if "is_active" in payload:
            is_valid = bool(payload.get("is_active"))
        elif "isValid" in payload:
            is_valid = bool(payload.get("isValid"))
        else:
            is_valid = True
        return is_valid, remaining, unit

    @staticmethod
    def _is_unsupported_checkin_error(error: ApiError) -> bool:
        return error.status in {404, 405} or contains_any(error.message, UNSUPPORTED_CHECKIN_PATTERNS) or contains_any(str(error.payload), UNSUPPORTED_CHECKIN_PATTERNS)

    def _candidate_endpoints(self) -> tuple[tuple[str, str | None], ...]:
        """待探测的签到端点；已探测出可用端点时只返回它。"""
        if self._checkin_endpoint is not None:
            return (self._checkin_endpoint,)
        return CHECKIN_ENDPOINTS

    def _probe_status(self) -> tuple[str, Any] | None:
        """按端点表探测签到状态接口，返回 (状态路径, data)；全部不可用返回 None。

        各 fork 的签到端点不同（/check-in vs /play/checkin），逐个尝试；命中后
        缓存该端点对，后续 do_checkin 直接复用，不再重复试错。
        """
        for checkin_path, status_path in self._candidate_endpoints():
            if not status_path:
                continue
            try:
                data = unwrap_data(self.request("GET", status_path))
            except ApiError as exc:
                if exc.transient:
                    raise
                if not self._is_unsupported_checkin_error(exc):
                    # 401 等登录失效已在 request 内触发过刷新重试；这里若仍失败按需向上暴露。
                    kind = self.classify(exc)
                    if kind in {"need_login", "need_verification"}:
                        raise
                continue
            if isinstance(data, dict):
                self._checkin_endpoint = (checkin_path, status_path)
                return status_path, data
        return None

    # ── ProfileClient 接口 ──
    def fetch_status(self) -> StatusInfo:
        # 带签到扩展的 fork 提供状态接口（100xLabs 为 /check-in/status，极速蹬为
        # /play/checkin/status）。按端点表探测；都不存在时回落到用户资料/用量接口。
        probed = self._probe_status()

        if probed is not None:
            status_path, data = probed
            checked_in = data.get("checked_in_today")
            if checked_in is None:
                checked_in = data.get("checked_in")
            if checked_in is None:
                # 极速蹬的 play/checkin/status 用 today_checked / has_checked_in 表达。
                for key in ("today_checked", "has_checked_in", "is_checked_in", "checked"):
                    if key in data:
                        checked_in = data.get(key)
                        break
            balance = _extract_standard_balance(data)
            quota_usd = self.quota_to_usd(balance) if balance is not None else None
            if quota_usd is None:
                try:
                    user = self.fetch_user()
                    quota_usd = self.quota_to_usd(user.quota_raw)
                except ApiError:
                    quota_usd = None
            return StatusInfo(
                checked_in_today=bool(checked_in) if checked_in is not None else None,
                turnstile_required=False,
                quota_usd=quota_usd,
                raw={"source": status_path, "payload": data},
            )

        # 标准 Sub2API（无签到扩展）：用用户资料/用量接口验证登录态并读取余额。
        user = self.fetch_user()
        return StatusInfo(
            checked_in_today=None,
            turnstile_required=False,
            quota_usd=self.quota_to_usd(user.quota_raw),
            raw={"source": "standard-sub2api", "message": "标准 Sub2API 未提供签到状态接口", "user": user.raw},
        )

    def fetch_user(self) -> UserInfo:
        if self._user_cache is None:
            self._user_cache = self._fetch_user_uncached()
        return self._user_cache

    def _fetch_user_uncached(self) -> UserInfo:
        login_error: ApiError | None = None
        authenticated_raw: dict[str, Any] | None = None
        username = ""

        def remember_or_raise(exc: ApiError) -> None:
            nonlocal login_error
            if exc.transient:
                raise exc
            if self.classify(exc) == "need_login" and login_error is None:
                login_error = exc

        def remember_authenticated(source: str, data: Any) -> None:
            nonlocal authenticated_raw, username
            if authenticated_raw is None:
                authenticated_raw = {"source": source, "payload": data}
            if not username:
                username = _extract_username(data)

        # 1) 标准 Sub2API JWT 前端接口：用户资料，源码路由 GET /api/v1/user/profile。
        for path in ("/user/profile", "/auth/me"):
            try:
                data = unwrap_data(self.request("GET", path))
                if isinstance(data, dict):
                    remember_authenticated(path, data)
                    balance = _extract_standard_balance(data)
                    if balance is not None:
                        return UserInfo(quota_raw=balance, username=username, raw={"source": path, "payload": data})
            except ApiError as exc:
                remember_or_raise(exc)

        # 2) 标准 Sub2API JWT 用量列表：GET /api/v1/usage，返回 data.items[].user.balance。
        try:
            data = unwrap_data(self.request("GET", "/usage?page=1&page_size=1&sort_by=created_at&sort_order=desc"))
            remember_authenticated("/usage", data)
            balance = _extract_usage_user_balance(data)
            if balance is None:
                balance = _extract_standard_balance(data)
            if balance is not None:
                return UserInfo(quota_raw=balance, username=username or _extract_username(data), raw={"source": "/usage", "payload": data})
        except ApiError as exc:
            remember_or_raise(exc)

        # 3) API Key 网关接口：GET /v1/usage。这里通常要求 sk-*，不是前端 auth_token。
        try:
            usage_payload = self.request_usage()
            usage = self._extract_usage_balance(usage_payload)
            if usage is not None:
                is_valid, remaining, unit = usage
                if not is_valid:
                    raise ApiError(401, usage_payload, "API Key 已停用或无效")
                return UserInfo(quota_raw=remaining, username=username, raw={"source": "/v1/usage", "unit": unit, "payload": usage_payload})
        except ApiError as exc:
            if exc.transient:
                raise
            # INVALID_API_KEY 只表示填入的是前端 JWT，不代表 JWT 登录态失效。

        # 4) 非标准 fork / 旧扩展兜底。
        for path in ("/check-in/status", "/subscriptions/summary", "/subscriptions/active", "/usage/dashboard/snapshot-v2"):
            try:
                data = unwrap_data(self.request("GET", path))
                remember_authenticated(path, data)
                balance = _extract_standard_balance(data)
                if balance is not None:
                    return UserInfo(quota_raw=balance, username=username or _extract_username(data), raw={"source": path, "payload": data})
            except ApiError as exc:
                if exc.transient:
                    raise
                continue

        if authenticated_raw is not None:
            return UserInfo(quota_raw=None, username=username, raw={"message": "Sub2API 登录态有效，但未识别到余额字段", **authenticated_raw})
        if login_error is not None:
            raise login_error
        return UserInfo(quota_raw=None, raw={"message": "未能从 Sub2API 标准接口识别 balance/remaining/quota"})

    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        # 标准 Sub2API 源码没有每日签到接口，但各 fork 的签到端点也不统一
        # （100xLabs 用 /check-in，极速蹬用 /play/checkin）。按 CHECKIN_ENDPOINTS
        # 顺序探测，命中即缓存；全部不可用时，只要标准用户资料/用量接口可用，就把
        # “登录态验证 + 余额查询”视为本次保活成功。
        body = {"turnstile_token": turnstile} if turnstile else {}
        last_error: ApiError | None = None

        for checkin_path, status_path in self._candidate_endpoints():
            try:
                # 签到 POST 是幂等的（重复签到 → already_checked_in），瞬时网络错误可安全重试。
                data = unwrap_data(self.request("POST", checkin_path, body, retry_non_idempotent=True))
            except ApiError as exc:
                if exc.transient:
                    raise
                kind = self.classify(exc)
                if kind == "need_verification":
                    raise
                if kind == "already_done":
                    # 「今日已签到」说明这个端点就是对的，记住它。
                    self._checkin_endpoint = (checkin_path, status_path)
                    raise
                last_error = exc
                if self._is_unsupported_checkin_error(exc):
                    continue  # 该 fork 没有这个端点，试下一个
                # 端点存在但站点明确拒绝（如 success:false「签到功能维护中」、
                # need_login）。这不是「站点没有签到接口」，不能退化成 unsupported
                # 的保活成功——那样会把站点给出的真实原因藏进 detail，用户只看到
                # 「已完成余额查询」。直接上抛，由 action 层按 classify 结果分类。
                self._checkin_endpoint = (checkin_path, status_path)
                raise
            self._checkin_endpoint = (checkin_path, status_path)
            # 真实签到成功后余额可能变化；后续读取必须重新探测一次。
            self._user_cache = None
            reward = self._reward_from(data)
            if isinstance(reward.raw, dict):
                reward.raw.setdefault("checkin_endpoint", checkin_path)
            return reward

        exc = last_error or ApiError(404, None, "未找到可用的 Sub2API 签到端点")
        try:
            user = self.fetch_user()
        except ApiError:
            # 标准资料/余额接口也失败时，保留原始 check-in 错误，便于上层触发登录态刷新。
            raise exc
        return CheckinReward(
            current_quota=user.quota_raw,
            raw={
                "unsupported_checkin": True,
                "message": "标准 Sub2API 源码未提供签到接口，已完成登录态验证与余额查询",
                "source": "standard-sub2api",
                "checkin_error": {"status": exc.status, "message": exc.message},
                "probed_endpoints": [path for path, _ in CHECKIN_ENDPOINTS],
                "user": user.raw,
            },
            extra={"unsupported_checkin": True, "standard_sub2api": True},
        )

    def classify(self, error: ApiError) -> str:
        if contains_any(error.message, ALREADY_DONE_PATTERNS):
            return "already_done"
        if error.status == 401 or contains_any(error.message, LOGIN_PATTERNS) or contains_any(str(error.payload), ["unauthorized"]):
            return "need_login"
        if contains_any(error.message, VERIFICATION_PATTERNS):
            return "need_verification"
        return "error"

    @staticmethod
    def _reward_from(data: Any) -> CheckinReward:
        if not isinstance(data, dict):
            # 非 dict 响应（如 HTML、纯文本 "ok"）不构成签到成立的证据。
            return CheckinReward(raw=data, checkin_unconfirmed=True)
        reward = data.get("reward_amount")
        if reward is None:
            reward = data.get("today_reward")
        extra: dict[str, Any] = {}
        if data.get("total_reward") is not None:
            extra["total_reward"] = data["total_reward"]
        if data.get("current_streak") is not None:
            extra["consecutive_days"] = data["current_streak"]
        if data.get("total_check_in_days") is not None:
            extra["total_checkins"] = data["total_check_in_days"]

        already = bool(data.get("already_checked_in"))
        # 签到成立需要正面证据。曾出现过的误报链条：某些 fork 对未生效的签到请求
        # 也回 HTTP 200 且 body 里没有任何奖励字段（例如 {} 或 {"data":null}），
        # 旧实现把它当成 CheckinReward() 空成功，最终报「签到成功」但额度未到账。
        # 因此这里要求至少命中一项可信信号，否则标记 checkin_unconfirmed，
        # 交由 action 层改判（见 providers/actions/api.py）。
        confirmed = (
            already
            or reward is not None
            or data.get("balance") is not None
            or bool(extra)
            or bool(data.get("checked_in_today"))
            or bool(data.get("today_checked"))
            or bool(data.get("success") is True)
            or bool(data.get("checkin_date") or data.get("checked_at") or data.get("check_in_at"))
        )
        return CheckinReward(
            already_done=already,
            quota_awarded=reward,
            current_quota=data.get("balance"),
            raw=data,
            extra=extra,
            checkin_unconfirmed=not confirmed,
        )


class Sub2ApiProfile(SiteProfile):
    key = "sub2api"
    quota_is_usd = True

    def build_client(self, site: SiteConfig, auth: AuthInfo) -> ProfileClient:
        return Sub2ApiClient(site, auth)

    def http_password_login(
        self,
        site: SiteConfig,
        email: str,
        password: str,
        log: Any = None,
    ) -> dict[str, str]:
        """纯 HTTP 账密登录，换取新的 access_token / refresh_token。

        暴露为 profile 方法，供 actions 层在「已保存 token 与 refresh_token 都失效」
        时作为启动浏览器之前的最后一次纯 HTTP 尝试（站点未启用 Turnstile 时可行）。
        失败返回空 dict，由调用方降级到浏览器脚本。
        """
        return http_password_login(site, email, password, log=log)

    def build_lazy_refresh_client(self, site: SiteConfig) -> ProfileClient | None:
        """oauth/browser 场景下的缓存优先客户端：先用已缓存 access_token 调接口，
        仅当接口返回登录失效（如 JWT 过期）时，才用浏览器 OAuth 刷新一次新 token。

        避免每次签到都启动浏览器：只有缓存 token 已存在时才走此路径；无缓存 token 时
        返回 None，交由 build_http_client 回落到及早浏览器刷新。
        """
        cached = normalize_access_token(site.access_token)
        if not cached:
            return None
        return Sub2ApiClient(
            site,
            AuthInfo(access_token=cached),
            token_refresher=lambda: self.refresh_token_via_browser(site),
        )

    def supports_browser_refresh(self) -> bool:
        return True

    def refresh_token_via_browser(self, site: SiteConfig) -> str | None:
        """按 auth_method 显式选择登录态刷新出最新 auth_token。"""
        candidates: list[tuple[str, str]] = []
        auth_method = (site.auth_method or "cookie").strip().lower()
        site_state = (site.browser_state or "").strip()
        fallback_provider = str(getattr(site, "oauth_fallback_provider", "") or "").strip()
        if fallback_provider:
            # 独立的可选 OAuth 兜底：不改变 auth_method；只有缓存 Token 失效时才会调用到这里。
            try:
                import accounts_store
                provider = accounts_store.normalize_oauth_provider(fallback_provider)
                account = accounts_store.normalize_oauth_account(getattr(site, "oauth_fallback_account", ""))
                text = accounts_store.oauth_state_text(provider, account).strip()
                if text:
                    candidates.append((f"可选 OAuth {provider}:{account}", text))
            except Exception:
                pass
        elif auth_method == "browser":
            if site_state:
                candidates.append(("站点 browser_state", site_state))
        elif auth_method == "oauth":
            try:
                import accounts_store
                provider = accounts_store.normalize_oauth_provider(getattr(site, "oauth_provider", "")) or "linuxdo"
                account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
                text = accounts_store.oauth_state_text(provider, account).strip()
                if text:
                    candidates.append((f"共享 {provider}:{account} 登录态", text))
                if site_state and all(site_state != existing for _label, existing in candidates):
                    candidates.append(("站点 browser_state", site_state))
            except Exception:
                pass
        else:
            return None

        if not candidates:
            return None

        try:
            from browser import session as browser_session
        except Exception as exc:
            print(f"[sub2api:{site.name}] 加载 browser_session 失败：{exc}", file=sys.stderr, flush=True)
            return None

        def _log(msg: str) -> None:
            print(f"[sub2api:{site.name}] {msg}", file=sys.stderr, flush=True)

        for label, state_text in candidates:
            try:
                _log(f"尝试使用{label}刷新 auth_token...")
                refreshed = browser_session.run_sync(
                    browser_session.capture_sub2api_token(
                        base_url=normalize_base_url(site.base_url),
                        browser_state_text=state_text,
                        proxy=site.proxy or "",
                        log=_log,
                        return_state=True,
                    )
                )
                token = ""
                refreshed_state = ""
                refresh_token = ""
                if isinstance(refreshed, dict):
                    token = str(refreshed.get("access_token") or "")
                    refreshed_state = str(refreshed.get("state") or "")
                    refresh_token = str(refreshed.get("refresh_token") or "")
                elif isinstance(refreshed, str):
                    token = refreshed
                if token:
                    if refreshed_state or refresh_token:
                        try:
                            import accounts_store
                            if accounts_store.update_account_auth_data(
                                site.name,
                                site.base_url,
                                access_token=token,
                                browser_state=refreshed_state,
                                refresh_token=refresh_token,
                            ):
                                if refreshed_state:
                                    site.browser_state = refreshed_state
                                if refresh_token:
                                    # 存下 refresh_token 后，下次「token 过期」可纯 HTTP 续期，
                                    # 不必再为这一常见情况启动 Camoufox。
                                    site.refresh_token = refresh_token
                        except Exception:
                            pass
                    _log(f"已通过{label}刷新 auth_token")
                    return token
            except Exception as exc:
                _log(f"使用{label}刷新 token 失败：{exc}")
        return None
