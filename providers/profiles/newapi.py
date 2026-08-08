#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""New API 站点适配器（SiteProfile）。

接口：
- GET  /api/user/checkin?month=YYYY-MM → 签到状态（{success,data:{stats:{checked_in_today}}}）
- GET  /api/user/self                  → 用户信息（含 quota）
- POST /api/user/checkin               → 旧版签到（legacy）
- challenge：新版 WASM PoW，调用 checkin_challenge.js（Node 执行）

认证头：New-Api-User + Authorization: Bearer / Cookie
响应：{success, data}；额度为内部 quota（/500000 = $）。

个别 fork 的签到带额外验证。内置验证路由会按 `verification_mode` 自动/优先分流
Turnstile、点阵字符、base64Captcha 字符图和 GoCaptcha 点选；本模块只在公开配置
漏报、签到接口再次拒绝时，把服务端原文换成可操作的机制配置提示。
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

from config import Timeouts

from accounts_store import normalize_api_variant

from ..base import (
    USER_AGENT,
    ApiError,
    AuthInfo,
    BrowserAuthError,
    CheckinReward,
    ProfileClient,
    SiteConfig,
    SiteProfile,
    StatusInfo,
    UserInfo,
    contains_any,
    extract_message,
    http_request,
    log_http_exchange,
    normalize_base_url,
    parse_json,
    payload_code,
    strip_session_cookie,
    unwrap_data,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
CHALLENGE_HELPER_PATH = SCRIPT_DIR / "checkin_challenge.js"
CHALLENGE_TIMEOUT = Timeouts.NODE_CHALLENGE  # Node 执行 WASM PoW 的超时（秒）

# 阿里云/反爬 JS 挑战页特征（urllib 拿不到 JSON，只会拿到这段混淆 JS 或挑战 HTML）。
ANTIBOT_BLOCK_PATTERNS = [
    "接口返回非 JSON",
    "var arg1=",
    "acw_sc__",
    "aliyun_waf",
    "slidecaptcha",
    "just a moment",
    "cf-challenge",
    "checking your browser",
]

ALREADY_DONE_PATTERNS = ["已签到", "今日已", "已领取", "明天再来", "already"]
# 人机验证词表用 providers.base 唯一实现（newapi 的 classify 先判验证再判登录，
# 不追加宽泛的「验证」，避免「token 验证失败」类登录报错被误分类）。
from ..base import VERIFICATION_PATTERNS  # noqa: E402, F401
LOGIN_PATTERNS = ["登录", "unauthorized", "token", "not logged in", "access token", "未登录", "无权", "权限不足"]
UPGRADED_FLOW_PATTERNS = ["checkin_flow_upgraded", "新版流程", "签到接口已升级"]
CHALLENGE_UNSUPPORTED_PATTERNS = ["404", "not found", "page not found", "no route", "unsupported"]
# Node 辅助脚本的网络类失败。undici 对连接/TLS 问题只回一句 "fetch failed"，拿不到
# 更细的原因，所以按文案识别；命中时回落 legacy 而不是判定签到失败。
CHALLENGE_NETWORK_PATTERNS = [
    "fetch failed", "econnreset", "etimedout", "enotfound", "econnrefused",
    "socket hang up", "network", "timeout",
]
# 内置验证路由漏判时，用服务端拒绝文案给出 verification_mode 配置指引。
CAPTCHA_REQUIRED_PATTERNS = ["请输入验证码", "captcha is required", "验证码不能为空"]
CAPTCHA_MODE_HINT = "bitmap_code / string_captcha / click_shape"
TURNSTILE_MISSING_PATTERNS = ["turnstile token 为空", "turnstile token is empty", "turnstile 校验失败"]
TURNSTILE_MODE_HINT = "turnstile"


def _is_antibot_block(error: ApiError) -> bool:
    """判断 ApiError 是否为「urllib 命中阿里云/反爬 JS 挑战页」而非真实业务错误。

    这类错误的特征：HTTP 200 但响应体是混淆 JS/挑战 HTML（parse_json 抛出「接口返回
    非 JSON」），或 body 里出现 acw_sc__/aliyun_waf 等 WAF 标记。命中时可用浏览器
    预取的额度兜底，而不是把它当成登录失效或站点异常。
    """
    haystacks = [str(error.message or "")]
    if isinstance(error.payload, str):
        haystacks.append(error.payload)
    text = " ".join(haystacks)
    return contains_any(text, ANTIBOT_BLOCK_PATTERNS)


class NewApiClient(ProfileClient):
    quota_is_usd = False

    def __init__(self, site: SiteConfig, auth: AuthInfo) -> None:
        self.site = site
        self.auth = auth
        self.base_url = normalize_base_url(site.base_url)
        self.referer = self.base_url + (site.referer_path if site.referer_path.startswith("/") else "/" + site.referer_path)

    # ── 底层请求 ──
    def request(self, method: str, path: str, body: bytes | None = None, *, retry_non_idempotent: bool = False) -> Any:
        url = path if path.startswith("http") else self.base_url + path
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.base_url,
            "Referer": self.referer,
            "New-Api-User": self.auth.new_api_user,
        }
        cookie = strip_session_cookie(self.auth.cookie) if self.auth.access_token else self.auth.cookie
        if cookie:
            headers["Cookie"] = cookie
        if self.auth.access_token:
            headers["Authorization"] = f"Bearer {self.auth.access_token}"
        if method.upper() in {"POST", "PUT", "PATCH"}:
            headers["Content-Type"] = "application/json;charset=UTF-8"
            if body is None:
                body = b"{}"

        try:
            payload = http_request(
                url,
                method=method,
                headers=headers,
                body=body,
                proxy=self.site.proxy,
                retry_non_idempotent=retry_non_idempotent,
                verify_ssl=getattr(self.site, "verify_ssl", True),
            )
        except ApiError as exc:
            # 站点原始回执是排查的第一手材料：只上抛 message 时，业务码拒绝、
            # 验证码不通过和 WAF 换页在日志里长得一模一样。
            log_http_exchange(self.site.name, method, url, error=exc)
            raise
        log_http_exchange(self.site.name, method, url, payload=payload)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ApiError(None, payload, extract_message(payload))
        return payload

    def _log_stage(self, message: str) -> None:
        """签到流程的阶段日志（stderr；worker 的 stdout 是机器协议通道）。"""
        from mask_utils import mask_secrets

        print(f"[newapi:{self.site.name}] {mask_secrets(str(message))}", file=sys.stderr, flush=True)

    def get_checkin_status_raw(self, month: str | None = None) -> Any:
        month = month or datetime.now().strftime("%Y-%m")
        return self.request("GET", f"/api/user/checkin?{urllib.parse.urlencode({'month': month})}")

    # ── ProfileClient 接口 ──
    def fetch_status(self) -> StatusInfo:
        data = unwrap_data(self.get_checkin_status_raw())
        stats = data.get("stats", {}) if isinstance(data, dict) else {}
        checked_in = stats.get("checked_in_today") if "checked_in_today" in stats else None
        return StatusInfo(checked_in_today=checked_in, raw=data)

    def fetch_user(self) -> UserInfo:
        try:
            data = unwrap_data(self.request("GET", "/api/user/self"))
        except ApiError as exc:
            # 阿里云 WAF 会对 urllib 这类无法执行 JS 挑战的客户端回一段混淆 JS 挑战页
            # （非 JSON）。若浏览器过 WAF 时已读到用户信息，用它兜底，避免把「浏览器已
            # 成功读到额度」误报成「接口返回非 JSON」。
            prefetched = getattr(self.auth, "prefetched_user", None)
            if isinstance(prefetched, dict) and _is_antibot_block(exc):
                quota = prefetched.get("quota")
                username = str(prefetched.get("username") or "")
                return UserInfo(quota_raw=quota, username=username, raw=prefetched)
            raise
        quota = data.get("quota") if isinstance(data, dict) else None
        username = ""
        if isinstance(data, dict):
            username = data.get("username") or data.get("display_name") or ""
        return UserInfo(quota_raw=quota, username=username, raw=data)

    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        variant = normalize_api_variant(self.site.api_variant)
        # 明确记录走了哪条接口变体：challenge 与 legacy 的失败原因完全不同，
        # 汇总里只有一句「签到失败」时无法判断该往哪个方向查。
        self._log_stage(
            "开始接口签到（api_variant="
            + f"{variant}，{'legacy 优先' if variant == 'legacy' else 'challenge 优先'}）"
        )
        try:
            if variant == "legacy":
                data = self._legacy_with_fallback(turnstile)
            else:
                data = self._challenge_with_fallback(turnstile)
        except ApiError as exc:
            # 内置路由已先尝试公开配置；走到这里说明站点漏报了验证方式，给出机制配置
            # 而不是让用户继续填写旧的内置脚本路径。
            if contains_any(exc.message, CAPTCHA_REQUIRED_PATTERNS):
                raise ApiError(
                    exc.status,
                    exc.payload,
                    f"站点签到需要图形验证码（服务端回执：{exc.message}）。"
                    "请在管理界面设置“验证方式”，可选 "
                    f"{CAPTCHA_MODE_HINT}；不确定时保留 auto。",
                ) from exc
            if contains_any(exc.message, TURNSTILE_MISSING_PATTERNS):
                raise ApiError(
                    exc.status,
                    exc.payload,
                    f"站点签到需要 Cloudflare Turnstile 人机验证（服务端回执：{exc.message}）。"
                    f"请把“验证方式”设为 {TURNSTILE_MODE_HINT}，"
                    "或用 --turnstile 手动传入令牌。",
                ) from exc
            raise
        return self._reward_from(data)

    def classify(self, error: ApiError) -> str:
        if contains_any(error.message, ALREADY_DONE_PATTERNS) or payload_code(error.payload) == "already_done":
            return "already_done"
        # 验证特征（如「Turnstile token 为空」）比登录特征更具体，须先判定：
        # 否则会因 message 含宽泛的 "token" 被 LOGIN_PATTERNS 误判为 need_login。
        # HTTP 401 是明确的未授权状态，仍优先归为 need_login。
        if error.status == 401:
            return "need_login"
        if contains_any(error.message, VERIFICATION_PATTERNS):
            return "need_verification"
        if contains_any(error.message, LOGIN_PATTERNS):
            return "need_login"
        return "error"

    # ── 签到接口变体 ──
    def _legacy_checkin(self, turnstile: str = "") -> Any:
        path = "/api/user/checkin"
        if turnstile:
            path += "?" + urllib.parse.urlencode({"turnstile": turnstile})
        # 签到 POST 是幂等的（重复签到 → already_done），瞬时网络错误可安全重试。
        return unwrap_data(self.request("POST", path, retry_non_idempotent=True))

    def _challenge_checkin(self) -> Any:
        if not CHALLENGE_HELPER_PATH.exists():
            raise ApiError(None, None, f"缺少新版签到辅助脚本：{CHALLENGE_HELPER_PATH}")

        env = os.environ.copy()
        env.update(
            {
                "NEWAPI_BASE_URL": self.base_url,
                "NEWAPI_COOKIE": strip_session_cookie(self.auth.cookie) if self.auth.access_token else self.auth.cookie,
                "NEWAPI_ACCESS_TOKEN": self.auth.access_token,
                "NEWAPI_USER_ID": self.auth.new_api_user,
                "NEWAPI_REFERER": self.referer,
                "NEWAPI_USER_AGENT": USER_AGENT,
            }
        )
        try:
            completed = subprocess.run(
                ["node", str(CHALLENGE_HELPER_PATH)],
                cwd=SCRIPT_DIR,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=CHALLENGE_TIMEOUT,
            )
        except FileNotFoundError as exc:
            raise ApiError(
                None,
                None,
                "未找到 Node.js（challenge 新版签到需要 node 执行 WASM PoW）。"
                "请安装 Node.js 并确保在 PATH 中，或将站点 api_variant 改为 legacy。",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ApiError(
                None,
                None,
                f"新版签到辅助脚本执行超时（>{CHALLENGE_TIMEOUT}s），可能是 PoW 难度过高或网络异常，请稍后重试。",
                transient=True,
            ) from exc

        output = (completed.stdout or completed.stderr or "").strip()
        # Node 辅助脚本的原始输出同样要落日志：challenge 流程整段跑在子进程里，
        # 不打出来的话「PoW 失败」「WASM 拉取失败」「站点回执」三种情况在上层
        # 都只表现为一句签到失败。
        self._log_stage(
            f"challenge 辅助脚本 rc={completed.returncode} → {output[:600] or '（无输出）'}"
        )
        try:
            payload = parse_json(output)
        except ApiError as exc:
            raise ApiError(None, output[:300], f"新版签到辅助脚本返回非 JSON：{output[:300]}") from exc
        if completed.returncode != 0 or (isinstance(payload, dict) and payload.get("success") is False):
            raise ApiError(None, payload, extract_message(payload))
        return unwrap_data(payload)

    def _challenge_with_fallback(self, turnstile: str) -> Any:
        try:
            return self._challenge_checkin()
        except ApiError as exc:
            if exc.status in {404, 405} or contains_any(exc.message, CHALLENGE_UNSUPPORTED_PATTERNS):
                return self._legacy_checkin(turnstile)
            # Node challenge 使用独立的 fetch/TLS 指纹，可能被 Cloudflare/WAF 单独
            # 挑战，而同站 legacy API 仍可由 Python HTTP 客户端访问。auto 模式在此
            # 应继续尝试幂等的 legacy 签到；若 legacy 也被拦，parse_json 会返回明确
            # 的验证提示，不再把 300 字符挑战 HTML 倾倒给用户。
            if _is_antibot_block(exc):
                self._log_stage("challenge 请求命中 Cloudflare/WAF，回落 legacy 签到接口")
                return self._legacy_checkin(turnstile)
            # 辅助脚本连不上站点（Node 的 undici 只会给一句 "fetch failed"）时也回落：
            # 这只说明「新版端点这次没探到」，legacy 仍值得一试。实测 sheapi.top 偶发
            # TLS 握手超时，旧实现在这里直接判 error，把一次可用的 legacy 签到丢掉了。
            if contains_any(exc.message, CHALLENGE_NETWORK_PATTERNS):
                return self._legacy_checkin(turnstile)
            raise

    def _legacy_with_fallback(self, turnstile: str) -> Any:
        try:
            return self._legacy_checkin(turnstile)
        except ApiError as exc:
            if contains_any(exc.message, UPGRADED_FLOW_PATTERNS) or contains_any(payload_code(exc.payload), UPGRADED_FLOW_PATTERNS):
                return self._challenge_checkin()
            raise

    @staticmethod
    def _reward_from(data: Any) -> CheckinReward:
        if isinstance(data, dict):
            return CheckinReward(
                quota_awarded=data.get("quota_awarded"),
                current_quota=data.get("quota"),
                raw=data,
            )
        return CheckinReward(raw=data)


class NewApiProfile(SiteProfile):
    key = "newapi"
    quota_is_usd = False

    def build_client(self, site: SiteConfig, auth: AuthInfo) -> ProfileClient:
        return NewApiClient(site, auth)

    def supports_browser_refresh(self) -> bool:
        return True

    def refresh_auth_via_browser(self, site: SiteConfig) -> AuthInfo | None:
        """用浏览器过阿里云 WAF 并导出站点 cookie（供 HTTP 签到复用）。

        仿 millylee 混合式：浏览器只负责“过 WAF + 拿 cookie”，拿到 acw_tc 等 WAF
        cookie 与站点 session 后一起返回，真正的签到由 HTTP api 逻辑发轻量请求完成。
        仅 auth_method=browser 生效，登录态取站点级 browser_state。

        WAF 持续风控（IP 信誉过低）时抛 BrowserAuthError(need_verification)，
        由 action 层翻译为对应状态，避免误报 need_login。
        """
        auth_method = (site.auth_method or "cookie").strip().lower()
        if auth_method != "browser":
            return None
        state_text = (site.browser_state or "").strip()
        if not state_text:
            return None

        try:
            from browser import session as browser_session
        except Exception as exc:
            print(f"[newapi:{site.name}] 加载 browser_session 失败：{exc}", file=sys.stderr, flush=True)
            return None

        def _log(msg: str) -> None:
            print(f"[newapi:{site.name}] {msg}", file=sys.stderr, flush=True)

        try:
            outcome = browser_session.run_sync(
                browser_session.refresh_site_cookies(
                    base_url=normalize_base_url(site.base_url),
                    browser_state_text=state_text,
                    fallback_uid=site.user_id.strip(),
                    proxy=site.proxy or "",
                    log=_log,
                )
            )
        except browser_session.BrowserSessionError as exc:
            raise BrowserAuthError(exc.status, str(exc), detail=exc.detail) from exc
        except Exception as exc:
            raise BrowserAuthError("error", f"浏览器过 WAF 异常：{exc}") from exc

        if not isinstance(outcome, dict):
            return None

        if not outcome.get("ok"):
            message = str(outcome.get("message") or "")
            if outcome.get("waf_blocked"):
                raise BrowserAuthError("need_verification", message, detail={"waf_blocked": True})
            if outcome.get("driver_crashed"):
                raise BrowserAuthError("error", message, detail={"driver_crashed": True})
            # 没导出到 cookie：登录态可能失效
            return None

        cookie = str(outcome.get("cookie") or "")
        if not cookie:
            return None

        # 刷新出的 storage_state 存运行期缓存供下次复用（不写 ACCOUNTS.json：
        # 它每次打开站点都会变，写回会让用户配置被后台任务反复改写）。
        refreshed_state = str(outcome.get("state") or "")
        if refreshed_state and refreshed_state != state_text:
            try:
                from .. import token_cache
                token_cache.save_site_browser_state(site, refreshed_state)
            except Exception:
                pass
            site.browser_state = refreshed_state

        new_api_user = str(outcome.get("new_api_user") or site.user_id or "").strip()

        # 浏览器过 WAF 时已读到的用户信息（含 quota/username）。作为 HTTP 再读被 WAF
        # 重新挑战时的权威兜底：这类站点 urllib 无法执行 JS 挑战，浏览器读数才可靠。
        prefetched_user: dict[str, Any] | None = None
        quota = outcome.get("quota")
        username = outcome.get("username")
        if quota is not None or username:
            prefetched_user = {"quota": quota, "username": username or ""}
            if new_api_user:
                prefetched_user["id"] = new_api_user

        return AuthInfo(cookie=cookie, new_api_user=new_api_user, prefetched_user=prefetched_user)
