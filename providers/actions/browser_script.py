#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_script 签到方式：运行仓库内自定义异步浏览器脚本。"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import accounts_store
from checkin_core.auth import PasswordLoginCapable, TokenRefreshCapable

from ..base import (
    ApiError,
    CheckinResult,
    QueryStatus,
    SiteConfig,
    SiteProfile,
    normalize_access_token,
    normalize_base_url,
)
from .api import run_http_flow, script_owns_http_flow


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


def _script_handles_oauth(site: SiteConfig) -> bool:
    """脚本是否声明自行处理 OAuth，而不是使用运行器的单层 OAuth 预处理。"""
    args = site.script_args if isinstance(site.script_args, dict) else {}
    value = args.get("script_handles_oauth", False)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


def _should_try_api_first(site: SiteConfig) -> tuple[bool, str]:
    """判断是否值得先跑纯 API 首选阶段。

    返回 (是否尝试, 跳过原因)。script_args.api_first 显式开关优先（含显式 True，
    用于强制重试）；未显式配置时，只有在存在任一可用凭据来源
    （access_token / refresh_token / 脚本账密）时才尝试——否则纯 API 阶段必然空跑，
    只会为 OAuth/纯浏览器站点（如 ABR 福利站）平白增加一次探测延迟与日志噪声。

    跳过原因为空串表示「无需告知」：只有用户显式配置 api_first=false 才值得记一行，
    「没有 API 凭据」是这类站点的固有状态，不构成需要用户处置的信息。
    """
    args = site.script_args if isinstance(site.script_args, dict) else {}
    if "api_first" in args:
        value = args.get("api_first")
        if isinstance(value, str):
            enabled = value.strip().casefold() in {"1", "true", "yes", "on"}
        else:
            enabled = bool(value)
        return enabled, "" if enabled else "script_args.api_first=false，按配置跳过纯 API 首选阶段"

    has_token = bool(normalize_access_token(getattr(site, "access_token", "") or ""))
    has_refresh = bool(str(getattr(site, "refresh_token", "") or "").strip())
    email, password = _script_credentials(site)
    has_credentials = bool(email and password)
    if has_token or has_refresh or has_credentials:
        return True, ""
    # 无任何 API 凭据是 OAuth/纯浏览器站点的常态，不是异常，不输出日志：
    # 每轮固定打印一行既无行动指引，又淹没真正需要关注的诊断。
    return False, ""


# 「脚本是否自管 HTTP 流程」的唯一实现在 api action（两条链路共用同一判定）。
_script_owns_http_flow = script_owns_http_flow


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


def _brief_payload(payload: Any, limit: int = 300) -> str:
    """把接口原始返回压成一行日志（脱敏 + 截断）。

    只报 reason/code 不够用：站点常把真实原因写在别的字段里（如 detail、errors、
    data.message），甚至直接回一段 HTML/JS 挑战页。排查时必须能看到原样返回，
    否则只能另写脚本手打接口。凭据由 mask_secrets 兜底遮蔽。
    """
    if payload is None or payload == "":
        return ""
    from mask_utils import mask_secrets, sanitize_data

    try:
        if isinstance(payload, (dict, list)):
            text = json.dumps(sanitize_data(payload), ensure_ascii=False, separators=(",", ":"))
        else:
            text = str(payload)
    except Exception:
        text = str(payload)
    text = mask_secrets(" ".join(text.split()))
    return text if len(text) <= limit else text[:limit] + f"…(+{len(text) - limit})"


def _describe_failure(exc: ApiError) -> str:
    """把 ApiError 展开成「HTTP 状态 + 服务端判据 + 原始返回」的单行描述。

    以前只打 exc.message，站点回的 401 与 403、以及 reason（如
    REFRESH_TOKEN_INVALID / TOKEN_EXPIRED）都看不到，同一句「未能完成」既可能
    是 token 过期也可能是账号被封，只能另写脚本手打接口才能区分。
    """
    parts: list[str] = []
    status = getattr(exc, "status", None)
    if status:
        parts.append(f"HTTP {status}")
    message = str(getattr(exc, "message", "") or "").strip()
    if message:
        parts.append(message)
    # 响应体只在 ApiError.payload（providers/base.py 的 ApiError 只有
    # status / payload / message / transient 四个字段，没有 body / url）。
    payload = getattr(exc, "payload", None)
    if isinstance(payload, dict):
        for key in ("reason", "code", "error"):
            value = str(payload.get(key) or "").strip()
            if value and value not in message:
                parts.append(f"{key}={value}")
                break
    body = _brief_payload(payload)
    if body and body not in message:
        parts.append(f"body={body}")
    if getattr(exc, "transient", False):
        parts.append("（临时性错误，可重试）")
    return " | ".join(parts) or "未提供失败详情"


def _describe_missing_token(site: SiteConfig) -> str:
    """说明「为什么没有可用 access_token」，区分留空 / 值损坏两种情形。

    normalize_access_token 会把含非 ASCII 的值静默判为空（HTTP 头只能承载
    latin-1），最常见来源是从站点后台的截断显示里复制、值中间带了 U+2026 省略号。
    不区分的话日志只会说「未配置」，用户明明填了却查不出问题在哪。
    """
    raw = str(getattr(site, "access_token", "") or "").strip()
    if not raw:
        return "配置里 access_token 为空"
    bad = sorted({ch for ch in raw if not ch.isascii()})
    if bad:
        shown = " ".join(f"{ch!r}(U+{ord(ch):04X})" for ch in bad[:3])
        return (
            f"access_token 含非 ASCII 字符 {shown}（共 {len(raw)} 字符），"
            "无法用于 HTTP 头，已视为未配置——多半是从截断显示里复制的残缺值"
        )
    if raw.startswith("<") and raw.endswith(">"):
        return "access_token 仍是占位文本，未填真实值"
    if raw.count(".") != 2:
        return f"access_token 不是 JWT 结构（{len(raw)} 字符，{raw.count('.') + 1} 段），可能复制不完整"
    return f"access_token 被判定为不可用（{len(raw)} 字符）"


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
    saved = token_cache.save_site_tokens(site, token, rotated)
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


def _renew_access_token(site: SiteConfig, profile: SiteProfile, reason: str) -> str:
    """token 完全缺失时主动用 refresh_token 续期；已有 token 的 401 由 client 处理。"""
    base_url = normalize_base_url(site.base_url)
    if not isinstance(profile, TokenRefreshCapable):
        _api_log(site, f"{reason}，但站点适配器（{type(profile).__name__}）不支持纯 HTTP 续期")
        return ""
    configured = str(getattr(site, "refresh_token", "") or "").strip()
    if not configured:
        _api_log(
            site,
            f"{reason}，但未配置 refresh_token（{base_url}）——"
            "请在管理界面「浏览器登录捕获」或手工填写 Refresh Token",
        )
        return ""
    _api_log(site, f"{reason}，尝试用 refresh_token 纯 HTTP 续期（{base_url}，rt {len(configured)} 字符）")
    try:
        pair = profile.refresh_token_via_http(site, log=lambda m: _api_log(site, m)) or {}
    except Exception as exc:
        _api_log(site, f"refresh_token 续期异常（{base_url}）：{type(exc).__name__}: {exc}")
        return ""
    renewed = normalize_access_token(str(pair.get("access_token") or ""))
    if not renewed:
        _api_log(site, f"refresh_token 续期未成功（{base_url}），将降级到账密登录 / 浏览器脚本")
        return ""
    _persist_tokens(
        site,
        renewed,
        str(pair.get("refresh_token") or "").strip(),
        log=lambda m: _api_log(site, m),
    )
    return renewed


def _merge_extra_message(base: str, extra: str) -> str:
    """把附加任务结论拼到签到消息后面（签到消息常自带句号，避免出现「。；」）。"""
    head = str(base or "").rstrip().rstrip("。.")
    tail = str(extra or "").strip()
    if not tail:
        return str(base or "")
    return f"{head}；{tail}" if head else tail


def _run_http_extras(site: SiteConfig, client: Any, result: CheckinResult | None) -> CheckinResult | None:
    """签到已成立时，用同一个 HTTP 客户端执行站点脚本声明的附加日常任务。

    为什么放在纯 HTTP 首选路径里而不是只放在浏览器脚本里：脚本只有在纯 API 失败时
    才会执行，若附加任务（如极速蹬的每日答题）只写在脚本的 run() 里，一旦某天 token
    仍有效、纯 API 直接签到成功，附加任务就被整天跳过。

    通用层不关心任务内容，只按约定调用站点脚本里的 ``run_http_extras(client, log)``，
    拿回 {detail 键: 摘要}；摘要含 outcome/message。脚本没定义该钩子就原样跳过。
    附加任务的任何失败都只写进 detail，绝不改写签到结论。
    """
    if result is None or result.status not in {"success", "already_done"}:
        return result
    script_path = str(getattr(site, "script", "") or "").strip()
    if not script_path:
        return result
    try:
        from browser import script_loader

        hooks = script_loader.load_script_hooks(script_path)
    except Exception as exc:  # noqa: BLE001
        _api_log(site, f"加载站点脚本以执行附加任务失败：{type(exc).__name__}: {exc}")
        return result
    if hooks.run_http_extras is None:
        return result

    try:
        extras = hooks.run_http_extras(client, log=lambda message: _api_log(site, message))
    except Exception as exc:  # noqa: BLE001 - 附加任务异常绝不能影响签到结论
        extras = {"extras": {"outcome": "error", "message": f"附加任务异常：{type(exc).__name__}: {exc}"}}
    if not isinstance(extras, dict) or not extras:
        return result

    if not isinstance(result.detail, dict):
        result.detail = {} if result.detail is None else {"checkin_detail": result.detail}
    for key, summary in extras.items():
        if not isinstance(summary, dict):
            continue
        message = str(summary.get("message") or "")
        _api_log(site, f"{key}：{message}")
        result.detail[str(key)] = summary
        if summary.get("outcome") in {"submitted", "already_done"}:
            result.message = _merge_extra_message(result.message, message)
    return result


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
    # 结果里的 base_url 由 run_http_flow 按站点配置统一填写，这里不再各自拼一份。
    token = normalize_access_token(getattr(site, "access_token", "") or "")
    script_owns_http_flow = _script_owns_http_flow(site)

    from ..base import AuthInfo

    def _attempt(client: Any, stage: str) -> CheckinResult | None:
        """复用 api action 的唯一 HTTP 状态机；本层只增加阶段日志与降级决策。"""

        def observe(event: str, payload: Any) -> None:
            if event == "status":
                checked = getattr(payload, "checked_in_today", None)
                quota = getattr(payload, "quota_usd", None)
                _api_log(
                    site,
                    f"[{stage}] 状态读取成功：今日已签={checked} 余额="
                    + (f"${quota:.2f}" if isinstance(quota, (int, float)) else "未知"),
                )
                raw_status = _brief_payload(getattr(payload, "raw", None))
                if raw_status:
                    _api_log(site, f"[{stage}] 状态接口原始返回：{raw_status}")
            elif event == "status_error":
                kind = client.classify(payload)
                _api_log(site, f"[{stage}] 读取签到状态失败：{_describe_failure(payload)}（判定 {kind}）")
            elif event == "checkin_start":
                _api_log(site, f"[{stage}] 调用签到接口...")
            elif event == "reward":
                raw_reward = _brief_payload(getattr(payload, "raw", None))
                if raw_reward:
                    _api_log(site, f"[{stage}] 签到接口原始返回：{raw_reward}")
            elif event == "checkin_error":
                _api_log(site, f"[{stage}] 签到接口失败：{_describe_failure(payload)}")

        if script_owns_http_flow:
            _api_log(site, f"[{stage}] 站点脚本接管 HTTP 状态与签到，跳过 Sub2API 标准端点探测")
        result = run_http_flow(
            site,
            client,
            # browser_script 也可以显式声明纯 HTTP do_checkin 钩子。没有该钩子的
            # 既有脚本仍会回落 profile 默认签到，不会把 run() 浏览器入口误当 HTTP。
            allow_site_hook=True,
            observer=observe,
            skip_profile_probes=script_owns_http_flow,
        )
        if not isinstance(result.detail, dict):
            result.detail = {} if result.detail is None else {"checkin_detail": result.detail}
        result.detail.setdefault("api_first", True)
        result.detail.setdefault("api_stage", stage)
        # 脚本 result_message 的覆盖已由 run_http_flow 统一处理，这里不再重复一份。
        if result.detail.get("unsupported_checkin"):
            _api_log(site, f"[{stage}] 站点无可用签到端点，交给浏览器脚本")
            return None
        if result.status not in {"success", "already_done"}:
            _api_log(site, f"[{stage}] HTTP 状态机未完成（{result.status}）：{result.message}")
            return None
        _api_log(site, f"[{stage}] {result.message}")
        return _run_http_extras(site, client, result)

    # ── 第 0 级：完全没有可用 access_token 时，主动用 refresh_token 换一个 ──
    # 已有 token 的 401 由 Sub2ApiClient 内部最多续期一次；外层不再重复轮换。
    if not token:
        token = _renew_access_token(site, profile, "无可用 access_token")

    # ── 第 1 级：已配置的 access_token（含 refresh_token 纯 HTTP 续期）──
    if token:
        _api_log(site, "尝试纯 API 签到（使用已保存的 access_token）")
        client: Any = None
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
            # 分类能力属于已认证的 ProfileClient，而不是 SiteProfile。构造客户端
            # 本身失败时还没有 client，只能保守降级，不能引用未绑定变量。
            kind = client.classify(exc) if client is not None else "error"
            _api_log(site, f"token 阶段未能完成（{kind}）：{_describe_failure(exc)}")
        except Exception as exc:
            _api_log(site, f"token 阶段异常：{type(exc).__name__}: {exc}")
    else:
        # 说明「为什么没有 token」：配置留空、值损坏（非 ASCII/占位）还是被清掉，
        # 排查时结论完全不同。以前只有一句「未配置」，无法区分。
        _api_log(site, f"跳过 token 阶段：{_describe_missing_token(site)}")

    # ── 第 2 级：纯 HTTP 账密登录换新 token（站点未启 Turnstile 时可行）──
    email, password = _script_credentials(site)
    if isinstance(profile, PasswordLoginCapable) and email and password:
        _api_log(site, "token 不可用，尝试纯 HTTP 账密登录换取新 token")
        try:
            fresh = profile.http_password_login(site, email, password, log=lambda m: _api_log(site, m))
        except Exception as exc:
            _api_log(site, f"账密登录异常：{exc}")
            fresh = {}
        new_token = normalize_access_token(str((fresh or {}).get("access_token") or ""))
        if new_token:
            new_refresh = str((fresh or {}).get("refresh_token") or "").strip()
            # 写入运行期缓存（不动 ACCOUNTS.json），让下次运行可直接复用。
            _persist_tokens(site, new_token, new_refresh, log=lambda m: _api_log(site, m))
            password_client: Any = None
            try:
                password_client = build_client(site, AuthInfo(access_token=new_token))
                result = _attempt(password_client, "password")
                if result is not None:
                    return result
            except ApiError as exc:
                kind = password_client.classify(exc) if password_client is not None else "error"
                _api_log(site, f"账密阶段未能完成（{kind}）：{_describe_failure(exc)}")
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
    # 没有任何 API 可用凭据时（典型：OAuth/纯浏览器站点）默认跳过，避免空跑一轮。
    try_api, skip_reason = _should_try_api_first(site)
    if try_api:
        api_result = _try_api_checkin(site, profile)
        if api_result is not None:
            return api_result
    elif skip_reason:
        _api_log(site, skip_reason)

    auth_method = (site.auth_method or "").strip().lower()
    script_handles_oauth = _script_handles_oauth(site)
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
        # 特殊 OAuth 脚本完成首次回跳后会把「provider + 本站 session」一起缓存为
        # site.browser_state。后续应优先复用这份本站态；否则每次都从顶层共享 provider
        # state 重新登录，刚缓存的目标站 session 永远不会被使用。
        if script_handles_oauth and str(site.browser_state or "").strip():
            state_text = site.browser_state
        else:
            state_text = accounts_store.oauth_state_text(oauth_provider, oauth_account) or site.browser_state
        detail: dict[str, Any] = {
            "checkin_source": "browser_script",
            "auth_method": auth_method,
            "oauth_provider": oauth_provider,
            "oauth_account": oauth_account,
        }
        if script_handles_oauth:
            detail["script_handles_oauth"] = True
        # 双层 SSO 等特殊流程需要共享 provider storage state，但不能让运行器提前
        # 按单层 OAuth 规则触发；此时由脚本的 run() 完成完整登录链路。
        initial_oauth_provider = "" if script_handles_oauth else oauth_provider
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
        initial_oauth_provider = "" if script_handles_oauth else fallback_provider
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
            result = _run(fallback_state, "" if script_handles_oauth else fallback_provider)
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
    """只读查询不运行脚本；token 缺失/过期时允许纯 HTTP 续期或账密登录。"""
    if not str(getattr(site, "script", "") or "").strip():
        return QueryStatus(ok=False, message="未配置 browser_script 脚本路径", status="need_config")
    auth_method = (site.auth_method or "").strip().lower()
    if auth_method not in {"browser", "oauth"}:
        return QueryStatus(ok=False, message="browser_script 仅支持 auth_method=browser/oauth", status="need_config")

    from ..base import AuthInfo

    build_client = getattr(profile, "build_client", None)
    if not callable(build_client):
        return QueryStatus(ok=True, message="站点适配器不支持纯 API 查询，需执行脚本", status="success")

    def _query(client: Any, stage: str) -> QueryStatus | None:
        try:
            user = client.fetch_user()
            quota_usd = client.quota_to_usd(user.quota_raw)
            checked_in: bool | None = None
            try:
                status = client.fetch_status()
                if status.checked_in_today is not None:
                    checked_in = status.checked_in_today
                if quota_usd is None and status.quota_usd is not None:
                    quota_usd = status.quota_usd
            except Exception as exc:
                _api_log(site, f"[query:{stage}] 签到状态读取失败：{exc}")
            if quota_usd is not None:
                return QueryStatus(
                    ok=True,
                    quota_usd=quota_usd,
                    checked_in=checked_in,
                    message=f"查询成功（API {stage}）",
                    status="success",
                )
        except ApiError as exc:
            kind = client.classify(exc)
            _api_log(site, f"[query:{stage}] 查询失败（{kind}）：{_describe_failure(exc)}")
        except Exception as exc:
            _api_log(site, f"[query:{stage}] 查询异常：{type(exc).__name__}: {exc}")
        return None

    token = normalize_access_token(getattr(site, "access_token", "") or "")
    if not token:
        token = _renew_access_token(site, profile, "查询时无可用 access_token")
    if token:
        try:
            token_client = build_client(site, AuthInfo(access_token=token))
        except ApiError as exc:
            _api_log(site, f"[query:token] 构造客户端失败：{_describe_failure(exc)}")
        except Exception as exc:
            _api_log(site, f"[query:token] 构造客户端异常：{type(exc).__name__}: {exc}")
        else:
            result = _query(token_client, "token")
            if result is not None:
                return result

    # 纯 token/refresh 均不可用时，仍可尝试无 Turnstile 的 HTTP 账密登录；不启动浏览器。
    email, password = _script_credentials(site)
    if isinstance(profile, PasswordLoginCapable) and email and password:
        try:
            fresh = profile.http_password_login(site, email, password, log=lambda m: _api_log(site, m)) or {}
        except Exception as exc:
            _api_log(site, f"查询账密登录异常：{type(exc).__name__}: {exc}")
            fresh = {}
        new_token = normalize_access_token(str(fresh.get("access_token") or ""))
        if new_token:
            _persist_tokens(site, new_token, str(fresh.get("refresh_token") or ""))
            try:
                password_client = build_client(site, AuthInfo(access_token=new_token))
            except ApiError as exc:
                _api_log(site, f"[query:password] 构造客户端失败：{_describe_failure(exc)}")
            except Exception as exc:
                _api_log(site, f"[query:password] 构造客户端异常：{type(exc).__name__}: {exc}")
            else:
                result = _query(password_client, "password")
                if result is not None:
                    return result

    return QueryStatus(ok=True, message="browser_script 站点需通过测试签到/定时签到执行脚本", status="success")
