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
"""

from __future__ import annotations

import os
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

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
    normalize_base_url,
    normalize_cookie,
    parse_json,
    payload_code,
    strip_session_cookie,
    unwrap_data,
)

SCRIPT_DIR = Path(__file__).resolve().parent.parent.parent
CHALLENGE_HELPER_PATH = SCRIPT_DIR / "checkin_challenge.js"
CHALLENGE_TIMEOUT = 60  # Node 执行 WASM PoW 的超时（秒）

ALREADY_DONE_PATTERNS = ["已签到", "今日已", "已领取", "明天再来", "already"]
VERIFICATION_PATTERNS = ["Turnstile", "Cloudflare", "Just a moment", "安全验证", "challenge-platform"]
LOGIN_PATTERNS = ["登录", "unauthorized", "token", "not logged in", "access token", "未登录", "无权", "权限不足"]
UPGRADED_FLOW_PATTERNS = ["checkin_flow_upgraded", "新版流程", "签到接口已升级"]
CHALLENGE_UNSUPPORTED_PATTERNS = ["404", "not found", "page not found", "no route", "unsupported"]


class NewApiClient(ProfileClient):
    quota_is_usd = False

    def __init__(self, site: SiteConfig, auth: AuthInfo) -> None:
        self.site = site
        self.auth = auth
        self.base_url = normalize_base_url(site.base_url)
        self.referer = self.base_url + (site.referer_path if site.referer_path.startswith("/") else "/" + site.referer_path)

    # ── 底层请求 ──
    def request(self, method: str, path: str, body: bytes | None = None) -> Any:
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

        payload = http_request(url, method=method, headers=headers, body=body)
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ApiError(None, payload, extract_message(payload))
        return payload

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
        data = unwrap_data(self.request("GET", "/api/user/self"))
        quota = data.get("quota") if isinstance(data, dict) else None
        username = ""
        if isinstance(data, dict):
            username = data.get("username") or data.get("display_name") or ""
        return UserInfo(quota_raw=quota, username=username, raw=data)

    def do_checkin(self, turnstile: str = "") -> CheckinReward:
        variant = (self.site.api_variant or "auto").strip().lower()
        if variant == "legacy":
            data = self._legacy_with_fallback(turnstile)
        else:
            data = self._challenge_with_fallback(turnstile)
        return self._reward_from(data)

    def classify(self, error: ApiError) -> str:
        if contains_any(error.message, ALREADY_DONE_PATTERNS) or payload_code(error.payload) == "already_done":
            return "already_done"
        if error.status == 401 or contains_any(error.message, LOGIN_PATTERNS):
            return "need_login"
        if contains_any(error.message, VERIFICATION_PATTERNS):
            return "need_verification"
        return "error"

    # ── 签到接口变体 ──
    def _legacy_checkin(self, turnstile: str = "") -> Any:
        path = "/api/user/checkin"
        if turnstile:
            path += "?" + urllib.parse.urlencode({"turnstile": turnstile})
        return unwrap_data(self.request("POST", path))

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
            sys.path.insert(0, str(SCRIPT_DIR))
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
            msg = str(exc)
            status = "error" if ("camoufox" in msg.lower() or "启动" in msg) else "need_login"
            raise BrowserAuthError(status, msg) from exc
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

        # 刷新出的 storage_state 回写供下次复用
        refreshed_state = str(outcome.get("state") or "")
        if refreshed_state and refreshed_state != state_text:
            try:
                import accounts_store
                if accounts_store.update_account_auth_data(site.name, site.base_url, browser_state=refreshed_state):
                    site.browser_state = refreshed_state
            except Exception:
                site.browser_state = refreshed_state

        new_api_user = str(outcome.get("new_api_user") or site.user_id or "").strip()
        return AuthInfo(cookie=cookie, new_api_user=new_api_user)
