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
    ApiError,
    AuthInfo,
    CheckinReward,
    ProfileClient,
    SiteConfig,
    SiteProfile,
    StatusInfo,
    UserInfo,
    extract_message,
    http_request,
    log_http_exchange,
    normalize_access_token,
    normalize_base_url,
    normalize_cookie,
    unwrap_data,
)
from . import sub2api_fork_routes as fork_routes
from . import sub2api_protocol as protocol

# 协议语义（响应解析 / 错误归类 / 端点表 / 词表）统一由 sub2api_protocol 提供；
# 本模块只负责「怎么和站点通信」：会话、cookie jar、token 续期、重试、浏览器刷新。
#
# 下面这些名字必须继续在本模块可见：仓库内既有调用与测试都按
# `providers.profiles.sub2api.<name>` 引用（含 monkeypatch），改成只在
# protocol 里存在会静默破坏那些补丁点。
API_PREFIX = protocol.API_PREFIX
CHECKIN_ENDPOINTS = protocol.CHECKIN_ENDPOINTS
LOGIN_PATTERNS = protocol.LOGIN_PATTERNS
VERIFICATION_PATTERNS = protocol.VERIFICATION_PATTERNS
ALREADY_DONE_PATTERNS = protocol.ALREADY_DONE_PATTERNS
UNSUPPORTED_CHECKIN_PATTERNS = protocol.UNSUPPORTED_CHECKIN_PATTERNS


def _persist_refreshed_token(
    site: SiteConfig,
    token: str,
    refresh_token: str = "",
    browser_state: str = "",
) -> None:
    """把刷新出的新凭据写入运行期缓存（token_cache.json）。

    不写回 ACCOUNTS.json：那是用户维护的配置，短期 JWT 与浏览器登录态都属运行期
    产物，混进去会让配置被后台任务反复改写、并加速导出的 GitHub Secret 过期
    （见 providers/token_cache.py）。缓存写入失败不影响本次签到：内存值仍可用。
    """
    if not token and not refresh_token and not browser_state:
        return
    if token:
        site.access_token = token
    if refresh_token:
        site.refresh_token = refresh_token
    if browser_state:
        site.browser_state = browser_state
    try:
        from .. import token_cache

        token_cache.save_site_tokens(site, token, refresh_token, browser_state=browser_state)
    except Exception:
        pass


# 协议语义层的模块级别名。
#
# 为什么保留下划线名字而不是直接改调用点：测试里存在
# ``monkeypatch.setattr(sub2api, "_extract_standard_balance", ...)`` 这类模块级
# 打桩，而本模块内部函数通过全局名解析这些符号，改名会让打桩静默失效（补丁打在
# 一个再也没人读的名字上，测试照常「通过」却什么都没验证）。
_brief = protocol.brief
_describe_api_error = protocol.describe_api_error
_to_number = protocol.to_number
_pick_first_number = protocol.pick_first_number
_extract_usage_user_balance = protocol.extract_usage_user_balance
_extract_standard_balance = protocol.extract_standard_balance
_extract_username = protocol.extract_username


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
        # 路由差异由独立的 fork 路由模块选择；本客户端只消费通用端点序列。
        self._checkin_endpoints = fork_routes.select_checkin_endpoints(
            self.base_url,
            CHECKIN_ENDPOINTS,
        )
        self._checkin_endpoint: tuple[str, str | None] | None = None
        # 会话级 cookie jar：部分 Sub2API 站点（实测极速蹬）把会话绑定到网络/客户端
        # 指纹，服务端下发的 cookie 必须在后续请求带回，否则报
        # 「Session network fingerprint changed, please login again」。
        # urllib 默认不保留 cookie，因此这里显式维护一个 jar 供本客户端全程复用。
        self._cookie_jar = http_cookiejar.CookieJar()
        # 纯 HTTP 的 refresh_token 刷新（不启动浏览器）；由 profile 注入。
        self._refresh_token = str(getattr(site, "refresh_token", "") or "").strip()
        self._http_refresh_used = False
        # 最近一次 refresh_token 续期失败的可诊断原因（端点/状态码/服务端 reason）。
        # 供调用方打日志：失败结论本身没有诊断价值，用户需要知道是凭据被作废、
        # 站点限流，还是网络不通。
        self._refresh_failure = ""

    def _refresh_via_http(self) -> str:
        """用 refresh_token 直接调 /auth/refresh 换新 access_token（不启动浏览器）。

        Sub2API 的 access_token 是短期 JWT，但 refresh_token 有效期长得多。
        只要浏览器捕获登录态时存下了 refresh_token，纯 HTTP 路径就能自行续期，
        无需为「token 过期」这一常见情况拉起 Camoufox。失败返回空串。
        """
        if not self._refresh_token:
            self._refresh_failure = "配置里没有 refresh_token"
            return ""
        if self._http_refresh_used:
            self._refresh_failure = "本次运行已尝试过 refresh_token 续期，不再重试"
            return ""
        self._http_refresh_used = True
        endpoint = self.base_url + API_PREFIX + "/auth/refresh"
        try:
            payload = http_request(
                endpoint,
                method="POST",
                headers={**self._headers(), "Content-Type": "application/json"},
                body=json.dumps({"refresh_token": self._refresh_token}).encode("utf-8"),
                proxy=self.site.proxy,
                verify_ssl=getattr(self.site, "verify_ssl", True),
                cookie_jar=self._cookie_jar,
            )
        except ApiError as exc:
            # 保留服务端给出的判据：以前这里直接 return ""，调用方只能打一句
            # 「refresh_token 已失效」，状态码和 reason（如 REFRESH_TOKEN_INVALID）
            # 全部丢失，排查时只能另写脚本手打这个端点才看得到真实原因。
            self._refresh_failure = _describe_api_error(exc, endpoint)
            return ""
        data = unwrap_data(payload)
        if not isinstance(data, dict):
            self._refresh_failure = f"{endpoint} 返回结构无法识别：{_brief(payload)}"
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
        # 公共头取自协议层（与 profile 的纯 HTTP 账密登录共用同一份，避免两条
        # 路径呈现不同指纹）；认证头按本客户端当前凭据追加。
        headers = protocol.base_headers(self.base_url)
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
            try:
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
            except ApiError as exc:
                # 站点原始回执是排查第一手材料：端点探测失败、业务码拒绝和登录
                # 失效在上层都只剩一句 message，看不出到底是哪一种。
                log_http_exchange(self.site.name, method, url, error=exc)
                raise
            log_http_exchange(self.site.name, method, url, payload=payload)
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

    # 响应解析与错误归类的实现在 sub2api_protocol；这里保留同名静态方法，
    # 既维持既有调用点（含测试对类方法的直接引用），又不再各写一份解析逻辑。
    _extract_usage_balance = staticmethod(protocol.extract_api_key_usage)
    _is_unsupported_checkin_error = staticmethod(protocol.is_unsupported_checkin_error)

    def _candidate_endpoints(self) -> tuple[tuple[str, str | None], ...]:
        """待探测的签到端点；已探测出可用端点时只返回它。"""
        if self._checkin_endpoint is not None:
            return (self._checkin_endpoint,)
        return self._checkin_endpoints

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
            # 各 fork 表达「今日已签」的字段名不同（100xLabs 用 checked_in_today，
            # 极速蹬用 today_checked / has_checked_in），键表与优先级见 protocol。
            checked_in = protocol.checked_in_flag(data)
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
                "probed_endpoints": [path for path, _ in self._checkin_endpoints],
                "user": user.raw,
            },
            extra={"unsupported_checkin": True, "standard_sub2api": True},
        )

    def classify(self, error: ApiError) -> str:
        return protocol.classify_error(error)

    @staticmethod
    def _reward_from(data: Any) -> CheckinReward:
        """委托给协议层；保留为 staticmethod，历史调用方按类访问它。"""
        return protocol.reward_from(data)


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
        时作为启动浏览器之前的最后一次纯 HTTP 尝试。失败返回空 dict，由调用方
        降级到浏览器脚本。

        实测要点：
        - 站点若启用 Turnstile（/settings/public 的 turnstile_enabled），登录接口会
          回 400 TURNSTILE_VERIFICATION_FAILED，纯 HTTP 拿不到令牌，直接放弃；
        - 必须先访问一次站点并复用同一个 CookieJar，否则会被判
          「Session network fingerprint changed」。
        """
        def _log(message: str) -> None:
            if log:
                log(message)

        base = normalize_base_url(site.base_url)
        jar = http_cookiejar.CookieJar()
        # 与客户端请求共用同一份公共头：此前这里复制了一份完全相同的 5 个头，
        # 改 User-Agent/Referer 时漏改一处就会让两条路径呈现不同指纹。
        common = protocol.base_headers(base)

        # 1) 读公开设置：启用 Turnstile 时纯 HTTP 登录必然被拒，不必白跑一次。
        try:
            settings = http_request(
                base + API_PREFIX + "/settings/public",
                method="GET",
                headers=common,
                proxy=site.proxy,
                verify_ssl=getattr(site, "verify_ssl", True),
                cookie_jar=jar,
            )
        except ApiError as exc:
            _log(f"读取站点公开设置失败：{exc.message}")
            settings = None
        data = unwrap_data(settings)
        if isinstance(data, dict) and bool(data.get("turnstile_enabled")):
            _log("站点声明启用 Turnstile，纯 HTTP 账密登录不可用，需回落浏览器")
            return {}

        # 2) 登录（沿用同一 CookieJar，满足会话指纹校验）。
        try:
            payload = http_request(
                base + API_PREFIX + "/auth/login",
                method="POST",
                headers={**common, "Content-Type": "application/json"},
                body=json.dumps({"email": email, "password": password}).encode("utf-8"),
                proxy=site.proxy,
                verify_ssl=getattr(site, "verify_ssl", True),
                cookie_jar=jar,
            )
        except ApiError as exc:
            _log(f"纯 HTTP 账密登录失败：{exc.message}")
            return {}

        body = unwrap_data(payload)
        if not isinstance(body, dict):
            _log("登录响应结构无法识别")
            return {}
        if body.get("temp_token") or body.get("two_factor_required"):
            _log("账号启用了两步验证，纯 HTTP 登录不可用")
            return {}
        token = normalize_access_token(str(body.get("access_token") or ""))
        if not token:
            _log("登录响应未包含 access_token")
            return {}
        refresh = str(body.get("refresh_token") or "").strip()
        _log(f"纯 HTTP 账密登录成功（access_token {len(token)} 字符）")
        return {"access_token": token, "refresh_token": refresh}

    def refresh_token_via_http(self, site: SiteConfig, log: Any = None) -> dict[str, str]:
        """仅用 refresh_token 做纯 HTTP 续期，不启动浏览器。

        用于 access_token 缺失/损坏（例如配置里是占位文本）但 refresh_token 仍
        有效的情况：此时没有可用 token 去触发客户端内部的惰性刷新，需要一个不
        依赖 access_token 的入口。返回 {"access_token": ..., "refresh_token": ...}，
        失败返回 {}。
        """
        def _log(message: str) -> None:
            if log:
                log(message)

        refresh = str(getattr(site, "refresh_token", "") or "").strip()
        if not refresh:
            _log(f"{normalize_base_url(site.base_url)} 未配置 refresh_token，跳过纯 HTTP 续期")
            return {}
        client = Sub2ApiClient(site, AuthInfo())
        token = client._refresh_via_http()
        if not token:
            # 带上站点、端点、状态码与服务端 reason：只说「已失效」时用户无法区分
            # 凭据真被作废（REFRESH_TOKEN_INVALID）、站点限流还是网络不通，
            # 之前排查只能另写脚本手打这个端点。
            _log(
                f"refresh_token 续期失败（{client._refresh_failure or '原因未知'}）；"
                f"refresh_token 长度 {len(refresh)}，如确认已被服务端作废需重新捕获登录态"
            )
            return {}
        _log(f"refresh_token 续期成功（新 access_token {len(token)} 字符）")
        return {"access_token": token, "refresh_token": client._refresh_token or refresh}

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
                    # token 与 browser_state 都是运行期产物，一并写独立缓存。
                    # browser_state 每次打开站点都会变（cookie/localStorage 刷新），
                    # 写回 ACCOUNTS.json 会让用户配置被后台任务反复改写。
                    _persist_refreshed_token(site, token, refresh_token, refreshed_state)
                    _log(f"已通过{label}刷新 auth_token")
                    return token
            except Exception as exc:
                _log(f"使用{label}刷新 token 失败：{exc}")
        return None
