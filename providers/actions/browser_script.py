#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到方式：运行仓库内自定义异步浏览器脚本。"""

from __future__ import annotations

import os
import sys
from typing import Any

import accounts_store

from ..base import (
    ApiError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    normalize_access_token,
    normalize_base_url,
)


def _load_runner():
    from browser import script_runner
    return script_runner


def _script_can_self_login(site: SiteConfig) -> bool:
    """脚本是否具备自行登录的凭据（无需现成 browser_state）。

    scripts/checkin/*.py 的账密兜底优先读 script_args 的 email/password，
    未填时回退到环境变量（键名可由 email_env/password_env 覆盖）。只要任一来源
    可用，就该让脚本启动并自行登录，而不是在这里判「登录态缺失」失败。
    """
    args = site.script_args if isinstance(site.script_args, dict) else {}
    if str(args.get("email") or "").strip() and str(args.get("password") or "").strip():
        return True
    email_env = str(args.get("email_env") or "").strip()
    password_env = str(args.get("password_env") or "").strip()
    if email_env and password_env:
        return bool(os.environ.get(email_env, "").strip() and os.environ.get(password_env, "").strip())
    # 未显式指定环境变量名时无法可靠猜出脚本的默认键名，交由脚本自行判断。
    return False


def _script_credentials(site: SiteConfig) -> tuple[str, str]:
    """取脚本可用的账密（script_args 优先，回退环境变量）。"""
    args = site.script_args if isinstance(site.script_args, dict) else {}
    email = str(args.get("email") or "").strip()
    password = str(args.get("password") or "")
    if email and password:
        return email, password
    email_env = str(args.get("email_env") or "").strip()
    password_env = str(args.get("password_env") or "").strip()
    if email_env and password_env:
        return (
            os.environ.get(email_env, "").strip(),
            os.environ.get(password_env, ""),
        )
    return "", ""


def _api_log(site: SiteConfig, message: str) -> None:
    """browser_script 的 API 优先阶段日志（stderr，worker 的 stdout 是协议通道）。"""
    from mask_utils import mask_secrets

    print(f"[api_first:{site.name}] {mask_secrets(str(message))}", file=sys.stderr, flush=True)


def _persist_tokens(
    site: SiteConfig,
    access_token: str,
    refresh_token: str = "",
    log: Any = None,
) -> bool:
    """把新拿到的 token 写入运行期缓存，并同步到内存 site。

    刻意不写 ACCOUNTS.json：token 是短期运行产物（access_token 数小时即过期），
    混进配置会让用户的配置文件被后台任务反复改写，也会让导出的 GitHub Secret
    里带上很快失效的值。缓存文件见 providers/token_cache.py（已 gitignore）。
    """
    from .. import token_cache

    token = normalize_access_token(access_token or "")
    rotated = str(refresh_token or "").strip()
    if not token and not rotated:
        return False
    saved = token_cache.save_tokens(site.name, site.base_url, token, rotated)
    if token:
        site.access_token = token
    if rotated:
        site.refresh_token = rotated
    if log is not None and saved:
        try:
            log("新 token 已写入运行期缓存（未改动 ACCOUNTS.json）")
        except Exception:
            pass
    return saved


def _try_api_checkin(site: SiteConfig, profile: SiteProfile) -> CheckinResult | None:
    """浏览器脚本之前的首选方案：纯 HTTP 调站点签到接口（不启动浏览器）。

    三级凭据来源，逐级降级（对应用户要求的「先 API、再登录态、再账密」）：
    1. 配置的 access_token（过期时 profile 会用 refresh_token 纯 HTTP 续期）；
    2. 上述都不可用时，用 script_args/环境变量里的账密做纯 HTTP 登录换新 token
       （站点未启用 Turnstile 时可行，实测极速蹬可走通）；
    3. 仍拿不到明确结论则返回 None，交由浏览器脚本处理（browser_state → 账密 →
       Turnstile 真实点击）。

    只在拿到明确结论（成功 / 今日已签到）时返回结果；其余一律返回 None 以便降级。
    每个阶段都打日志：此前这里完全静默，失败时无法判断卡在哪一级。
    """
    build_client = getattr(profile, "build_client", None)
    if not callable(build_client):
        return None
    base_url = normalize_base_url(site.base_url)
    token = normalize_access_token(getattr(site, "access_token", "") or "")

    from ..base import AuthInfo

    def _attempt(client: Any, stage: str) -> CheckinResult | None:
        """用给定客户端跑一次「读状态 → 签到」；无明确结论返回 None。"""
        try:
            status = client.fetch_status()
        except ApiError as exc:
            kind = profile.classify(exc) if hasattr(profile, "classify") else "error"
            _api_log(site, f"[{stage}] 读取签到状态失败：{exc.message}（判定 {kind}）")
            if kind == "need_login":
                raise
            status = None
        else:
            checked = getattr(status, "checked_in_today", None)
            quota = getattr(status, "quota_usd", None)
            _api_log(
                site,
                f"[{stage}] 状态读取成功：今日已签={checked} 余额="
                + (f"${quota:.2f}" if isinstance(quota, (int, float)) else "未知"),
            )
            if checked:
                return CheckinResult(
                    site.name, base_url, "already_done", "今日已签到。",
                    detail={"checkin_source": "api", "api_first": True, "api_stage": stage},
                )

        _api_log(site, f"[{stage}] 调用签到接口...")
        reward = client.do_checkin("")
        detail = {"checkin_source": "api", "api_first": True, "api_stage": stage}
        if getattr(reward, "already_done", False):
            _api_log(site, f"[{stage}] 接口返回今日已签到")
            return CheckinResult(site.name, base_url, "already_done", "今日已签到。", detail=detail)
        raw = getattr(reward, "raw", None)
        if isinstance(raw, dict) and raw.get("unsupported_checkin"):
            _api_log(site, f"[{stage}] 站点无可用签到端点，交给浏览器脚本")
            return None
        # 无签到成立证据时不谎报成功，交给浏览器脚本二次确认。
        if getattr(reward, "checkin_unconfirmed", False):
            _api_log(site, f"[{stage}] 接口回 200 但无签到证据，交给浏览器脚本确认")
            return None
        awarded = getattr(reward, "quota_awarded", None)
        if awarded is not None:
            detail["quota_awarded"] = awarded
            _api_log(site, f"[{stage}] 签到成功，获得 {awarded}")
            return CheckinResult(site.name, base_url, "success", f"签到成功，获得额度：{awarded}", detail=detail)
        _api_log(site, f"[{stage}] 签到成功")
        return CheckinResult(site.name, base_url, "success", "签到成功。", detail=detail)

    def _renew_via_refresh(reason: str) -> str:
        """用 refresh_token 做纯 HTTP 续期，返回新 access_token（失败返回空串）。

        两处都要用：token 完全缺失时（占位/被清空），以及 token 已过期时。
        之前只在「完全缺失」时调用，导致「token 过期但 refresh_token 有效」这一
        最常见情形被跳过——第 1 级用的是裸 token 客户端（无 refresher），过期即
        失败，明明有效的 refresh_token 从未被使用，直接退化成开浏览器。
        """
        refresh_http = getattr(profile, "refresh_token_via_http", None)
        if not callable(refresh_http):
            return ""
        if not str(getattr(site, "refresh_token", "") or "").strip():
            return ""
        _api_log(site, f"{reason}，尝试用 refresh_token 纯 HTTP 续期")
        try:
            pair = refresh_http(site, log=lambda m: _api_log(site, m)) or {}
        except Exception as exc:
            _api_log(site, f"refresh_token 续期异常：{exc}")
            return ""
        renewed = normalize_access_token(str(pair.get("access_token") or ""))
        if not renewed:
            _api_log(site, "refresh_token 续期未成功")
            return ""
        _persist_tokens(site, renewed, str(pair.get("refresh_token") or "").strip(),
                        log=lambda m: _api_log(site, m))
        return renewed

    # ── 第 0 级：完全没有可用 access_token 时，先用 refresh_token 换一个 ──
    if not token:
        token = _renew_via_refresh("无可用 access_token")

    # ── 第 1 级：已配置的 access_token（含 refresh_token 纯 HTTP 续期）──
    if token:
        _api_log(site, "尝试纯 API 签到（使用已保存的 access_token）")
        try:
            # 必须用纯 HTTP 客户端：build_lazy_refresh_client 注入的 refresher 会
            # 拉起 Camoufox（实测日志里出现过 "Camoufox 运行模式"），那就违背了
            # 「第一级先纯 API」的前提，也让本该几秒完成的探测变成几十秒。
            # refresh_token 的纯 HTTP 续期由 Sub2ApiClient 内部的 _refresh_via_http
            # 完成，无需浏览器。
            client = build_client(site, AuthInfo(access_token=token))
            result = _attempt(client, "token")
            if result is not None:
                return result
        except ApiError as exc:
            kind = profile.classify(exc) if hasattr(profile, "classify") else "error"
            if kind == "already_done":
                _api_log(site, "接口返回今日已签到")
                return CheckinResult(
                    site.name, base_url, "already_done", "今日已签到。",
                    detail={"checkin_source": "api", "api_first": True, "api_stage": "token"},
                )
            _api_log(site, f"token 阶段未能完成（{kind}）：{exc.message}")
        except Exception as exc:
            _api_log(site, f"token 阶段异常：{exc}")
    else:
        _api_log(site, "未配置 access_token，跳过 token 阶段")

    # ── 第 1.5 级：token 过期但 refresh_token 可能仍有效 → 纯 HTTP 续期后重试 ──
    # 这是实测中最常见的情形（access_token 只有几小时有效期），必须在动用账密
    # 之前先试，否则等于浪费一个长期有效的凭据。
    if token:
        renewed = _renew_via_refresh("token 已失效")
        if renewed and renewed != token:
            token = renewed
            try:
                result = _attempt(build_client(site, AuthInfo(access_token=token)), "refresh")
                if result is not None:
                    return result
            except ApiError as exc:
                kind = profile.classify(exc) if hasattr(profile, "classify") else "error"
                if kind == "already_done":
                    return CheckinResult(
                        site.name, base_url, "already_done", "今日已签到。",
                        detail={"checkin_source": "api", "api_first": True, "api_stage": "refresh"},
                    )
                _api_log(site, f"refresh 阶段未能完成（{kind}）：{exc.message}")
            except Exception as exc:
                _api_log(site, f"refresh 阶段异常：{exc}")

    # ── 第 2 级：纯 HTTP 账密登录换新 token（站点未启 Turnstile 时可行）──
    login = getattr(profile, "http_password_login", None)
    email, password = _script_credentials(site)
    if callable(login) and email and password:
        _api_log(site, "token 不可用，尝试纯 HTTP 账密登录换取新 token")
        try:
            fresh = login(site, email, password, log=lambda m: _api_log(site, m))
        except Exception as exc:
            _api_log(site, f"账密登录异常：{exc}")
            fresh = {}
        new_token = normalize_access_token(str((fresh or {}).get("access_token") or ""))
        if new_token:
            new_refresh = str((fresh or {}).get("refresh_token") or "").strip()
            # 写入运行期缓存（不动 ACCOUNTS.json），让下次运行可直接复用。
            _persist_tokens(site, new_token, new_refresh, log=lambda m: _api_log(site, m))
            try:
                result = _attempt(build_client(site, AuthInfo(access_token=new_token)), "password")
                if result is not None:
                    return result
            except ApiError as exc:
                kind = profile.classify(exc) if hasattr(profile, "classify") else "error"
                if kind == "already_done":
                    return CheckinResult(
                        site.name, base_url, "already_done", "今日已签到。",
                        detail={"checkin_source": "api", "api_first": True, "api_stage": "password"},
                    )
                _api_log(site, f"账密阶段未能完成（{kind}）：{exc.message}")
            except Exception as exc:
                _api_log(site, f"账密阶段异常：{exc}")
    elif not (email and password):
        _api_log(site, "未配置脚本账密，跳过纯 HTTP 登录阶段")

    _api_log(site, "纯 API 路径未能完成签到，降级到浏览器脚本")
    return None


def run_action(site: SiteConfig, profile: SiteProfile, turnstile: str = "") -> CheckinResult:
    """执行自定义浏览器脚本。"""
    base_url = normalize_base_url(site.base_url)
    if not str(getattr(site, "script", "") or "").strip():
        return CheckinResult(site.name, base_url, "need_config", "未配置 browser_script 脚本路径")

    # 首选方案：纯 API 签到（用配置的 access_token，不启动浏览器）。成功/已签到直接
    # 返回；登录态失效或接口不可用则降级到浏览器脚本（browser_state → 账密登录）。
    api_result = _try_api_checkin(site, profile)
    if api_result is not None:
        return api_result

    auth_method = (site.auth_method or "").strip().lower()
    fallback_provider = accounts_store.normalize_oauth_provider(
        getattr(site, "oauth_fallback_provider", "")
    )
    fallback_account = accounts_store.normalize_oauth_account(
        getattr(site, "oauth_fallback_account", "")
    )
    fallback_state = (
        accounts_store.oauth_state_text(fallback_provider, fallback_account)
        if fallback_provider else ""
    )

    if auth_method == "oauth":
        oauth_provider = accounts_store.normalize_oauth_provider(site.oauth_provider) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(getattr(site, "oauth_account", ""))
        state_text = accounts_store.oauth_state_text(oauth_provider, oauth_account) or site.browser_state
        detail: dict[str, Any] = {
            "checkin_source": "browser_script",
            "auth_method": auth_method,
            "oauth_provider": oauth_provider,
            "oauth_account": oauth_account,
        }
        initial_oauth_provider = oauth_provider
    elif auth_method == "browser":
        state_text = site.browser_state
        detail = {"checkin_source": "browser_script", "auth_method": auth_method}
        initial_oauth_provider = ""
    else:
        return CheckinResult(
            site.name,
            base_url,
            "need_config",
            "browser_script 仅支持 auth_method=browser/oauth",
            detail={"checkin_source": "browser_script", "auth_method": auth_method},
        )

    use_fallback_first = not str(state_text or "").strip() and bool(fallback_provider)
    if use_fallback_first:
        state_text = fallback_state
        initial_oauth_provider = fallback_provider
        detail.update({
            "oauth_fallback_used": True,
            "oauth_provider": fallback_provider,
            "oauth_account": fallback_account,
        })

    # 没有任何登录态时，不能直接判失败：脚本可能自带账密登录兜底
    # （见 scripts/checkin/*.py 的 _login_with_password，凭据来自 script_args
    # 或环境变量）。此时用空登录态启动浏览器，让脚本自己走登录流程；
    # 只有脚本也没有可用凭据时，才由脚本返回 need_login。
    if not str(state_text or "").strip():
        if _script_can_self_login(site):
            state_text = ""
            initial_oauth_provider = ""
            detail["self_login"] = True
        else:
            if fallback_provider:
                message = f"缺少可选 OAuth {fallback_provider}:{fallback_account} 登录态，签到失败"
            else:
                message = (
                    "站点登录态缓存不存在，且未配置 OAuth 兜底或脚本账密凭据，签到失败"
                )
            return CheckinResult(site.name, base_url, "error", message, detail=detail)

    try:
        runner = _load_runner()
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"加载 browser_script 运行器失败：{exc}", detail=detail)

    def _run(state_value: str, provider_value: str = ""):
        return runner.run_sync(
            site=site,
            browser_state_text=state_value,
            script_path=site.script,
            script_args=site.script_args,
            timeout=site.script_timeout,
            oauth_provider=provider_value,
        )

    try:
        result = _run(state_text, initial_oauth_provider)
        if (
            result.status == "need_login"
            and fallback_provider
            and not use_fallback_first
            and fallback_state.strip()
            and initial_oauth_provider != fallback_provider
        ):
            result = _run(fallback_state, fallback_provider)
            detail.update({
                "oauth_fallback_used": True,
                "oauth_provider": fallback_provider,
                "oauth_account": fallback_account,
            })
        elif result.status == "need_login" and auth_method == "browser" and not fallback_provider:
            result.status = "error"
            result.message = "站点登录态缓存已失效，且未配置 OAuth 兜底，签到失败"
    except Exception as exc:
        return CheckinResult(site.name, base_url, "error", f"浏览器脚本运行异常：{exc}", detail=detail)

    result_detail = result.detail
    if isinstance(result_detail, dict):
        merged_detail = dict(detail)
        merged_detail.update(result_detail)
        result_detail = merged_detail
    elif result_detail is None:
        result_detail = detail
    return CheckinResult(site.name, base_url, result.status, result.message, detail=result_detail)


def query_action(site: SiteConfig, profile: SiteProfile) -> QueryStatus:
    """只读查询不运行脚本，避免刷新状态时误触发点击签到。

    首选纯 API 查额度：若配置了 access_token 且 profile 支持 HTTP 查询（如 sub2api），
    直接用 access_token 读取余额/签到状态，不启动浏览器。查询失败（登录态失效等）
    时回落到「需执行脚本」提示，由测试签到走浏览器降级。
    """
    if not str(getattr(site, "script", "") or "").strip():
        return QueryStatus(ok=False, message="未配置 browser_script 脚本路径", status="need_config")
    auth_method = (site.auth_method or "").strip().lower()
    if auth_method not in {"browser", "oauth"}:
        return QueryStatus(ok=False, message="browser_script 仅支持 auth_method=browser/oauth", status="need_config")

    # 纯 API 首选：用配置的 access_token 直接查额度（不启动浏览器）。
    token = normalize_access_token(getattr(site, "access_token", "") or "")
    build_client = getattr(profile, "build_client", None)
    if token and callable(build_client):
        try:
            from ..base import AuthInfo

            client = build_client(site, AuthInfo(access_token=token))
            user = client.fetch_user()
            quota_usd = client.quota_to_usd(user.quota_raw)
            checked_in: bool | None = None
            try:
                status = client.fetch_status()
                if status.checked_in_today is not None:
                    checked_in = status.checked_in_today
                if quota_usd is None and status.quota_usd is not None:
                    quota_usd = status.quota_usd
            except Exception:
                pass
            if quota_usd is not None:
                return QueryStatus(
                    ok=True,
                    quota_usd=quota_usd,
                    checked_in=checked_in,
                    message="查询成功（access_token 直查）",
                    status="success",
                )
        except Exception:
            # 登录态失效 / 接口不可用：回落到下方「需执行脚本」提示。
            pass

    return QueryStatus(ok=True, message="browser_script 站点需通过测试签到/定时签到执行脚本", status="success")
