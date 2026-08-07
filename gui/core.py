# -*- coding: utf-8 -*-
"""纯逻辑层：不依赖 Qt，可单独测试。

这里集中了旧版 manage_accounts.py 里散落十余处的规则，每条规则只此一份：
- effective_auth ：checkin_action 对 auth_method 的矫正（relogin→oauth 等）；
- SiteRow        ：GUI 行模型与 store 行的双向映射；
- task_params    ：后台任务参数装配（旧版三处手写 dict 的唯一来源）；
- build_form_plan：表单显隐/文案联动（旧版 _sync_type 的 if 森林）；
- StatusStore    ：checkin_result.json + gui_status_cache.json 的合并与落盘；
- bg_log         ：脱敏后台日志（stderr + GUI 日志面板 sink）。
"""

from __future__ import annotations

import json
import sys
import traceback
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import datetime
from typing import Any, Callable

import accounts_store
from checkin_core.auth import can_optional_oauth as _can_optional_oauth
from checkin_core.auth import effective_auth as _effective_auth
from checkin_core.enums import (
    ACTION_VALUES,
    AUTH_METHOD_VALUES,
    PROFILE_VALUES,
    VERIFICATION_MODE_VALUES,
    status_meta,
)
from config import Timeouts as _Timeouts
from mask_utils import is_sensitive_key, mask_secrets

from .status_store import StatusStore as StatusStore

_SCRIPT_TIMEOUT_DEFAULT = _Timeouts.BROWSER_SCRIPT_DEFAULT
SCRIPT_TIMEOUT_DEFAULT = _SCRIPT_TIMEOUT_DEFAULT

# 与 accounts_store.site_config_from_mapping / providers.base.SiteConfig 的默认值
# 保持一致：GUI 用同一套默认值，空值才不会在往返中被解释成「用户改了配置」。
_REFERER_PATH_DEFAULT = "/profile"
_BROWSER_PROFILE_DEFAULT = ".browser_profile"
REFERER_PATH_DEFAULT = _REFERER_PATH_DEFAULT
BROWSER_PROFILE_DEFAULT = _BROWSER_PROFILE_DEFAULT

# ── 词表（有限值由 checkin_core 唯一维护）────────────────────────────────────
TYPES = PROFILE_VALUES
CRED_FIELDS = ("user_id", "access_token", "refresh_token", "cookie")
AUTH_METHODS = AUTH_METHOD_VALUES
CHECKIN_ACTIONS = ACTION_VALUES
API_VARIANTS = ("auto", "legacy")
VERIFICATION_MODES = VERIFICATION_MODE_VALUES
OAUTH_PROVIDERS = accounts_store.KNOWN_OAUTH_PROVIDERS
DEFAULT_OAUTH_ACCOUNT = accounts_store.DEFAULT_OAUTH_ACCOUNT

TYPE_LABELS = {"newapi": "New API", "sub2api": "Sub2API"}
AUTH_METHOD_LABELS = {
    "access_token": "Access Token (Bearer)",
    "cookie": "Cookie",
    "browser": "站点浏览器登录态",
    "oauth": "OAuth 登录态（共享账号）",
}
ACTION_LABELS = {
    "api": "接口签到 (调签到接口)",
    "visit": "访问保活 (只读监控额度)",
    "relogin": "浏览器重登 (自动 OAuth 发额度)",
    "browser_script": "自定义浏览器脚本",
}
OAUTH_PROVIDER_LABELS = {"linuxdo": "Linux.do", "github": "GitHub"}
API_VARIANT_LABELS = {"auto": "自动 (challenge 优先)", "legacy": "旧版接口 (legacy)"}
VERIFICATION_MODE_LABELS = {
    "auto": "自动识别（推荐）",
    "turnstile": "Cloudflare Turnstile",
    "bitmap_code": "点阵字符验证码（固定5位）",
    "string_captcha": "字符图片验证码（base64Captcha）",
    "click_shape": "图形点选验证码（GoCaptcha）",
}

# 「脚本路径」在两种签到方式下挂的是不同钩子，提示必须区分：
# - browser_script：脚本的 run(page, context, site, helpers) 在 Camoufox 里跑；
# - api          ：脚本的 do_checkin(client, log) 走纯 HTTP，用于站点私改的签到玩法
#                  （如 New API fork 的图形验证码），返回 None 则回落默认签到流程。
SCRIPT_HINT_BROWSER = "仓库内相对路径；浏览器里执行 run()，例如 scripts/checkin/100xlabs.py"
SCRIPT_PLACEHOLDER_BROWSER = "scripts/checkin/100xlabs.py"
SCRIPT_HINT_API = "可留空。仅用于覆盖内置验证路由或接入其它自定义 do_checkin 钩子"
SCRIPT_PLACEHOLDER_API = "scripts/custom_checkin.py（仅自定义流程时填）"


# ── 脱敏日志（敏感键规则统一由 mask_utils 维护）───────────────────────────────
LogSink = Callable[[str], None]
_LOG_SINKS: list[LogSink] = []


def add_log_sink(sink: LogSink) -> None:
    """注册 GUI 日志监听器；sink 可能在任意线程被调用，须自行保证线程安全。"""
    if sink not in _LOG_SINKS:
        _LOG_SINKS.append(sink)


def remove_log_sink(sink: LogSink) -> None:
    if sink in _LOG_SINKS:
        _LOG_SINKS.remove(sink)


def _is_sensitive_log_key(key: Any) -> bool:
    return is_sensitive_key(str(key or ""))


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): (f"<redacted:{len(str(v or ''))} chars>" if _is_sensitive_log_key(k) else _redact_log_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(v) for v in value)
    return value


def _safe_log_value(value: Any, key: str = "") -> str:
    if value is None:
        return ""
    if _is_sensitive_log_key(key):
        text = f"<redacted:{len(str(value or ''))} chars>"
    elif isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(_redact_log_value(value), ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            text = str(_redact_log_value(value))
    else:
        text = str(value)
    return mask_secrets(text.replace("\r", " ").replace("\n", " ").strip())


def bg_log(level: str, message: str, **fields_: Any) -> None:
    """输出后台任务日志：stderr + 已注册的 GUI sink（均为脱敏后文本）。"""
    try:
        extra = " ".join(
            f"{key}={_safe_log_value(value, key)}" for key, value in fields_.items() if value not in (None, "")
        )
        line = f"[{level.upper()}] {mask_secrets(str(message))}"
        if extra:
            line += f" | {extra}"
        print(line, file=sys.stderr, flush=True)
        stamp = datetime.now().strftime("%H:%M:%S")
        for sink in list(_LOG_SINKS):
            try:
                sink(f"{stamp} {line}")
            except Exception:
                pass
    except Exception:
        # 日志失败不能影响 GUI 主流程。
        pass


def error_result(message: str, exc: BaseException | None = None, **fields_: Any) -> dict[str, Any]:
    tb = traceback.format_exc() if exc is not None else ""
    error_text = f"{message}：{exc}" if exc is not None else message
    bg_log("ERROR", error_text, traceback=tb, **fields_)
    return {
        "ok": False,
        "status": "error",
        "message": mask_secrets(error_text),
        "error": mask_secrets(str(exc)) if exc is not None else mask_secrets(message),
        "traceback": tb,
    }


# ── auth 矫正（唯一实现）─────────────────────────────────────────────────────
def effective_auth(checkin_action: str, auth_method: str) -> str:
    """兼容入口；认证约束的唯一实现在 checkin_core.auth。"""
    return _effective_auth(checkin_action, auth_method)


def can_optional_oauth(site_type: str, checkin_action: str, auth_method: str) -> bool:
    """兼容入口；可选 OAuth 矩阵的唯一实现在 checkin_core.auth。"""
    return _can_optional_oauth(site_type, checkin_action, auth_method)


# ── 行模型 ────────────────────────────────────────────────────────────────────
@dataclass
class SiteRow:
    # 仅当前 GUI 进程使用的稳定身份；不写入 ACCOUNTS/Secret/状态快照。
    runtime_id: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False, compare=False)
    name: str = ""
    base_url: str = ""
    type: str = "newapi"
    auth_method: str = "cookie"
    checkin_action: str = "api"
    script: str = ""
    script_args: dict[str, Any] = field(default_factory=dict)
    script_args_text: str = "{}"
    script_timeout: int = _SCRIPT_TIMEOUT_DEFAULT
    api_variant: str = "auto"
    verification_mode: str = "auto"
    oauth_provider: str = "linuxdo"
    oauth_account: str = DEFAULT_OAUTH_ACCOUNT
    oauth_fallback_provider: str = ""
    oauth_fallback_account: str = ""
    enabled: bool = True
    user_id: str = ""
    access_token: str = ""
    # sub2api 的长期凭据：access_token 过期后可纯 HTTP 续期，无需启动浏览器。
    refresh_token: str = ""
    cookie: str = ""
    browser_state: str = ""
    proxy: str = ""
    verify_ssl: bool = True
    # 以下四项 checkin.py / run__all_checkin.py 都会消费，但此前 GUI 既不展示也不
    # 落盘：用户在 ACCOUNTS.json 手写的值，被 GUI 保存一次就静默抹掉。
    cookie_file: str = ""
    referer_path: str = _REFERER_PATH_DEFAULT
    browser_profile: str = _BROWSER_PROFILE_DEFAULT
    auto_refresh_cookie: bool = True
    # 旧字段（providers/base.py 标注仅兼容保留，relogin 已改用 oauth_provider）。
    # 不给输入框，但必须原样往返，否则同样会被保存流程丢掉。
    login_selector: str = ""

    # -- 便捷视图 --
    @property
    def auth(self) -> str:
        """经 checkin_action 矫正后的实际登录方式。"""
        return effective_auth(self.checkin_action, self.auth_method)

    def copy(self) -> "SiteRow":
        return replace(
            self,
            runtime_id=uuid.uuid4().hex,
            script_args=dict(self.script_args),
        )

    def to_legacy(self) -> dict[str, Any]:
        """旧版 GUI 行字典形态（accounts_store.build_github_secret_payload 消费）。"""
        out = {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "runtime_id"
        }
        out["script_args"] = dict(self.script_args)
        out["site_profile"] = self.type
        return out


def row_from_store(raw: dict[str, Any]) -> SiteRow:
    """store 行 → GUI 行（含合法性矫正），对应旧版 _rows() 的单行逻辑。"""
    url = accounts_store.normalize_base_url(str(raw.get("base_url", "") or raw.get("url", "")))
    site_type = str(raw.get("site_profile") or raw.get("type") or raw.get("provider") or "newapi").strip().lower()
    if site_type not in TYPES:
        site_type = "newapi"
    auth_method = str(raw.get("auth_method") or "").strip().lower()
    if auth_method not in AUTH_METHODS:
        auth_method = "access_token" if raw.get("access_token") else "cookie"
    checkin_action = str(raw.get("checkin_action") or "").strip().lower()
    if checkin_action not in CHECKIN_ACTIONS:
        checkin_action = "api"
    auth_method = effective_auth(checkin_action, auth_method)
    api_variant = str(raw.get("api_variant") or "auto").strip().lower()
    if api_variant not in API_VARIANTS:
        api_variant = "auto"
    verification_mode = accounts_store.normalize_verification_mode(
        raw.get("verification_mode")
    )
    script_args = accounts_store.normalize_script_args(raw.get("script_args"))
    # OAuth 流程的登录态统一存顶层 oauth_states，行内不携带站点级 state。
    keep_state = not (checkin_action == "relogin" or (checkin_action == "browser_script" and auth_method == "oauth"))
    return SiteRow(
        name=str(raw.get("name") or url),
        base_url=url,
        type=site_type,
        auth_method=auth_method,
        checkin_action=checkin_action,
        script=str(raw.get("script") or ""),
        script_args=script_args,
        script_args_text=json.dumps(script_args, ensure_ascii=False, indent=2) if script_args else "{}",
        script_timeout=accounts_store.parse_script_timeout(raw.get("script_timeout")),
        api_variant=api_variant,
        verification_mode=verification_mode,
        oauth_provider=accounts_store.normalize_oauth_provider(raw.get("oauth_provider")) or "linuxdo",
        oauth_account=accounts_store.normalize_oauth_account(raw.get("oauth_account") or raw.get("oauth_account_id")),
        oauth_fallback_provider=accounts_store.normalize_oauth_provider(raw.get("oauth_fallback_provider")),
        oauth_fallback_account=accounts_store.normalize_oauth_account(raw.get("oauth_fallback_account")),
        enabled=accounts_store.parse_enabled(raw.get("enabled"), True),
        user_id=str(raw.get("user_id") or ""),
        access_token=str(raw.get("access_token") or ""),
        refresh_token=str(raw.get("refresh_token") or ""),
        cookie=str(raw.get("cookie") or ""),
        browser_state=str(raw.get("browser_state") or "") if keep_state else "",
        proxy=str(raw.get("proxy") or ""),
        verify_ssl=accounts_store.parse_enabled(raw.get("verify_ssl"), True),
        # token_file 是 cookie_file 的旧名，accounts_store 两者都接受。
        cookie_file=str(raw.get("cookie_file") or raw.get("token_file") or ""),
        referer_path=str(raw.get("referer_path") or _REFERER_PATH_DEFAULT),
        browser_profile=str(raw.get("browser_profile") or _BROWSER_PROFILE_DEFAULT),
        auto_refresh_cookie=accounts_store.parse_enabled(raw.get("auto_refresh_cookie"), True),
        login_selector=str(raw.get("login_selector") or ""),
    )


def load_rows() -> list[SiteRow]:
    return [row_from_store(raw) for raw in accounts_store.load_unified_accounts()]


def new_row(site_type: str) -> SiteRow:
    return SiteRow(name="新站点", type=site_type if site_type in TYPES else "newapi")


# ── OAuth 登录态访问 ─────────────────────────────────────────────────────────
def oauth_state_text(oauth_states: dict[str, Any], provider: str, account: str) -> str:
    entry = ((oauth_states.get(provider) or {}).get("accounts") or {}).get(account) or {}
    return str(entry.get("state") or "").strip()


def oauth_state_entry(oauth_states: dict[str, Any], provider: str, account: str) -> dict[str, Any]:
    return dict(((oauth_states.get(provider) or {}).get("accounts") or {}).get(account) or {})


def has_shared_oauth(oauth_states: dict[str, Any]) -> bool:
    return any(((oauth_states.get(p) or {}).get("accounts") or {}) for p in OAUTH_PROVIDERS)


def normalized_fallback(row: SiteRow) -> tuple[str, str]:
    """当前流程真正会持久化的 OAuth 兜底组合；不适用场景一律 ("", "")。"""
    if not can_optional_oauth(row.type, row.checkin_action, row.auth):
        return "", ""
    provider = accounts_store.normalize_oauth_provider(row.oauth_fallback_provider)
    if not provider:
        return "", ""
    return provider, accounts_store.normalize_oauth_account(row.oauth_fallback_account)


# ── 任务参数装配（唯一实现；旧版三处手写 dict）───────────────────────────────
def task_params(
    row: SiteRow,
    oauth_states: dict[str, Any],
    *,
    explicit_credential_fields: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    auth = row.auth
    oauth_provider = accounts_store.normalize_oauth_provider(row.oauth_provider) or "linuxdo"
    oauth_account = accounts_store.normalize_oauth_account(row.oauth_account)
    browser_state = row.browser_state.strip()
    if auth == "oauth":
        browser_state = oauth_state_text(oauth_states, oauth_provider, oauth_account)
    fallback_provider, fallback_account = normalized_fallback(row)
    return {
        "name": row.name.strip(),
        "base_url": accounts_store.normalize_base_url(row.base_url),
        "site_profile": row.type,
        "auth_method": auth,
        "checkin_action": row.checkin_action,
        "script": row.script.strip(),
        "script_args": accounts_store.normalize_script_args(row.script_args),
        "script_timeout": accounts_store.parse_script_timeout(row.script_timeout),
        "api_variant": row.api_variant,
        "verification_mode": accounts_store.normalize_verification_mode(
            row.verification_mode
        ),
        "cookie": row.cookie.strip(),
        "access_token": row.access_token.strip(),
        "refresh_token": row.refresh_token.strip(),
        "user_id": row.user_id.strip(),
        "oauth_provider": oauth_provider,
        "oauth_account": oauth_account,
        "oauth_fallback_provider": fallback_provider,
        "oauth_fallback_account": fallback_account,
        "browser_state": browser_state,
        "login_selector": row.login_selector.strip(),
        "proxy": row.proxy.strip(),
        "verify_ssl": bool(row.verify_ssl),
        "fallback_uid": row.user_id.strip(),
        # 这四项 checkin.py 会消费；GUI 内单站点执行必须与批量/CI 行为一致，
        # 否则同一份配置在两条路径下表现不同。
        "cookie_file": row.cookie_file.strip(),
        "referer_path": row.referer_path.strip() or _REFERER_PATH_DEFAULT,
        "browser_profile": row.browser_profile.strip() or _BROWSER_PROFILE_DEFAULT,
        "auto_refresh_cookie": bool(row.auto_refresh_cookie),
        # GUI 未保存的显式输入（含显式清空）必须赢过运行缓存；worker 构造
        # SiteConfig 前会消费并移除这个内部字段，不进入配置/日志/结果。
        "_explicit_credential_fields": sorted(
            field
            for field in explicit_credential_fields
            if field in {"access_token", "refresh_token", "browser_state"}
        ),
    }


# ── 表单联动（旧版 _sync_type 的声明式重写）───────────────────────────────────
@dataclass
class FormPlan:
    show_variant: bool = False
    show_verification: bool = False
    show_script: bool = False
    # script_args 目前只传给 browser_script 的 run()；api 钩子固定为
    # do_checkin(client, log)，显示参数框会让用户误以为它会被消费。
    show_script_args: bool = False
    # 脚本超时只对浏览器脚本有意义（它限的是 Camoufox 里 run() 的执行时间）。
    # api 方式下的脚本是纯 HTTP 的，超时由 HTTP 层自己管，显示这个框只会误导。
    show_script_timeout: bool = False
    # 「脚本路径」这一个字段在两种签到方式下挂的是不同钩子，提示必须跟着变，
    # 否则 api 方式下会照着 browser_script 的示例去填一个浏览器脚本。
    script_hint: str = SCRIPT_HINT_BROWSER
    script_placeholder: str = SCRIPT_PLACEHOLDER_BROWSER
    show_oauth: bool = False
    show_fallback: bool = False
    show_state_box: bool = False
    state_editable: bool = False
    show_oauth_status: bool = False
    show_browser_ops: bool = False
    show_delete_oauth: bool = False
    creds_enabled: bool = False
    # sub2api 的 access_token / refresh_token 是**接口凭据**，与 auth_method 无关：
    # 即使登录方式是 browser/oauth，签到仍会先走纯 API（token → refresh_token 续期），
    # 只有全失败才拉浏览器。因此这两个框不能跟着 creds_enabled 一起灰掉，否则用户
    # 手工粘贴的有效 token 根本无法保存，表现为「填了仍显示没有」。
    token_enabled: bool = False
    capture_text: str = "浏览器登录捕获"
    verify_text: str = "检测登录态"
    oauth_status: str = ""
    mode_hint: str = ""
    # sub2api 的 access_token 是短期 JWT，refresh_token 才决定能否纯 HTTP 续期
    # （不必每次签到都拉起浏览器）。除状态文案外还给出输入框：站点风控（如
    # Turnstile）可能让浏览器捕获失败，此时手工粘贴是唯一可行的补救途径。
    show_refresh_status: bool = False
    show_refresh_input: bool = False
    refresh_status: str = ""
    # browser_profile 只在 browser/oauth 登录方式下被 checkin.py 使用（持久化浏览器
    # 目录前缀）；其他方式下展示它只会让人误以为配了就会生效。
    show_browser_profile: bool = False


_SCRIPT_FALLBACK_HINT = (
    "💡 自定义脚本始终先使用当前站点浏览器登录态；失效后最多通过可选 OAuth 自动登录并重试一次。"
    "不选择 OAuth 时将直接提示签到失败。可选账号来自顶层共享 OAuth 登录态。"
)
_NO_SHARED_OAUTH_HINT = "暂无共享 OAuth 登录态；请切换登录方式为“OAuth 登录态（共享账号）”后捕获，或点击“刷新账号”。"


def token_defect(value: str) -> str:
    """检出「看起来填了、实际不可用」的 access_token；正常返回空串。

    HTTP 头只能承载 latin-1，providers.base.normalize_access_token 会把含非 ASCII
    的值静默判为空 token。最常见的来源是从站点后台的截断显示里复制，值中间带了
    Unicode 省略号（U+2026）。不提示的话用户只会看到「未配置 access_token」，
    完全无从判断是自己粘贴的值有问题。
    """
    text = (value or "").strip()
    # 与 providers.base.normalize_access_token 保持同样的前缀剥离顺序，避免这里
    # 判「健康」而运行时判「无 token」（或反之）。
    if text.lower().startswith("authorization:"):
        text = text.split(":", 1)[1].strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    if not text:
        return ""
    # 占位文本先判：模板占位符往往本身就带中文（如「<在站点后台采集的 access_token>」），
    # 若先报「含非 ASCII」会把真正的原因盖住。
    if text.startswith("<") and text.endswith(">"):
        return "Token 仍是占位文本，请粘贴真实值。"
    bad = sorted({ch for ch in text if not ch.isascii()})
    if bad:
        shown = " ".join(f"{ch!r}(U+{ord(ch):04X})" for ch in bad[:3])
        return f"Token 含非 ASCII 字符 {shown}，无法用于 HTTP 请求，会被视为未配置。"
    if text.count(".") != 2:
        return "Token 不是 JWT 结构（应为 3 段以 . 分隔），可能复制不完整。"
    return ""


def _refresh_token_status(row: SiteRow) -> str:
    """凭据卡下方的 token 健康度文案：先报硬缺陷，再报 refresh_token 有无。"""
    defect = token_defect(row.access_token)
    prefix = f"⚠ {defect}\n" if defect else ""
    if row.refresh_token.strip():
        return prefix + "已保存 refresh_token：Token 过期可纯 HTTP 自动续期，无需启动浏览器。"
    return prefix + (
        "未保存 refresh_token：Token 过期后需启动浏览器重新登录；"
        "用「浏览器登录捕获」重新捕获一次即可自动存入。"
    )


def _fallback_status(oauth_states: dict[str, Any], provider: str, account: str) -> str:
    saved = oauth_state_entry(oauth_states, provider, account)
    state_len = len(str(saved.get("state") or ""))
    label = f"{OAUTH_PROVIDER_LABELS.get(provider, provider)} / {account}"
    return f"{label}（{state_len} 字符）" if state_len else f"{label}（未保存登录态）"


def build_form_plan(row: SiteRow, oauth_states: dict[str, Any]) -> FormPlan:
    auth = row.auth
    action = row.checkin_action
    is_browser = auth == "browser"
    is_oauth = auth == "oauth"
    is_script = action == "browser_script"
    # api 方式也能挂脚本：站点私改的签到玩法（如 New API fork 的图形验证码）都做成
    # 脚本，通过这个字段启用，通用适配器不必为个别站点背分支。
    # 见 providers/actions/api.py 的 _script_checkin 与 scripts/newapi_captcha.py。
    allow_api_script = action == "api"
    needs_oauth = is_oauth or action == "relogin" or (is_script and auth == "oauth")
    allow_fallback = can_optional_oauth(row.type, action, auth)
    fallback_provider, fallback_account = normalized_fallback(row)

    plan = FormPlan(
        show_variant=row.type == "newapi" and action == "api",
        show_verification=row.type == "newapi" and action == "api",
        show_script=is_script or allow_api_script,
        show_script_args=is_script,
        show_script_timeout=is_script,
        script_hint=SCRIPT_HINT_API if allow_api_script else SCRIPT_HINT_BROWSER,
        script_placeholder=SCRIPT_PLACEHOLDER_API if allow_api_script else SCRIPT_PLACEHOLDER_BROWSER,
        show_oauth=needs_oauth,
        show_fallback=allow_fallback,
        show_state_box=is_browser or needs_oauth or allow_fallback,
        state_editable=is_browser,
        show_oauth_status=needs_oauth or allow_fallback,
        show_browser_ops=is_browser or needs_oauth,
        show_delete_oauth=needs_oauth,
        creds_enabled=auth in ("access_token", "cookie"),
        # sub2api 永远可以手填接口凭据：签到链路先纯 API（token → refresh_token
        # 续期），浏览器只是最后的兜底，所以 browser/oauth 登录方式下这两个框也必须可编辑。
        token_enabled=row.type == "sub2api" or auth in ("access_token", "cookie"),
        # refresh_token 决定 Token 过期时能否纯 HTTP 续期（不必每次拉起浏览器）。
        show_refresh_status=row.type == "sub2api",
        show_refresh_input=row.type == "sub2api",
        # browser_profile 只在浏览器参与的登录方式下会被 checkin.py 使用。
        show_browser_profile=is_browser or needs_oauth,
        refresh_status=_refresh_token_status(row),
    )

    if needs_oauth:
        prov = accounts_store.normalize_oauth_provider(row.oauth_provider) or "linuxdo"
        account = accounts_store.normalize_oauth_account(row.oauth_account)
        state_len = len(oauth_state_text(oauth_states, prov, account))
        label = f"{OAUTH_PROVIDER_LABELS.get(prov, prov)} / {account}"
        plan.oauth_status = (
            f"已保存 {label} 登录态（{state_len} 字符）。"
            if state_len
            else f"尚未保存 {label} 登录态；请输入账号名后点击“捕获 OAuth 登录态”。"
        )
        plan.capture_text = "捕获 OAuth 登录态"
        plan.verify_text = "检测 OAuth 登录态"
        plan.mode_hint = (
            "💡 自定义脚本会用已保存的 OAuth 登录态启动浏览器，并由脚本控制页面点击。脚本路径请使用仓库内相对路径。"
            if is_script
            else "💡 OAuth 登录态按“提供商 + 账号”保存，可被多个站点复用；浏览器重登会自动使用 OAuth 登录方式。"
        )
    elif is_browser:
        if is_script and allow_fallback:
            if fallback_provider:
                plan.oauth_status = f"可选 OAuth：{_fallback_status(oauth_states, fallback_provider, fallback_account)}"
            else:
                plan.oauth_status = (
                    _NO_SHARED_OAUTH_HINT
                    if not has_shared_oauth(oauth_states)
                    else "可选 OAuth 当前未启用；可从已保存的共享账号中选择。"
                )
            plan.mode_hint = _SCRIPT_FALLBACK_HINT
        else:
            plan.mode_hint = "💡 站点浏览器登录态仅用于当前站点，不会作为共享 OAuth 账号使用。"
    elif allow_fallback:
        if fallback_provider:
            plan.oauth_status = _fallback_status(oauth_states, fallback_provider, fallback_account)
        elif not has_shared_oauth(oauth_states):
            plan.oauth_status = _NO_SHARED_OAUTH_HINT
        if is_script:
            plan.mode_hint = _SCRIPT_FALLBACK_HINT
    return plan


# ── 快照 / 校验 / 持久化 ─────────────────────────────────────────────────────
def _snapshot_row(row: SiteRow) -> dict[str, Any]:
    auth = row.auth
    fallback_provider, fallback_account = normalized_fallback(row)
    return {
        "name": row.name.strip(),
        "base_url": accounts_store.normalize_base_url(row.base_url),
        "type": row.type if row.type in TYPES else "newapi",
        "auth_method": auth,
        "checkin_action": row.checkin_action,
        "script": row.script.strip(),
        "script_args_text": row.script_args_text,
        "script_timeout": accounts_store.parse_script_timeout(row.script_timeout),
        "api_variant": row.api_variant if row.api_variant in API_VARIANTS else "auto",
        "oauth_provider": accounts_store.normalize_oauth_provider(row.oauth_provider) or "linuxdo",
        "oauth_account": accounts_store.normalize_oauth_account(row.oauth_account),
        "oauth_fallback_provider": fallback_provider,
        "oauth_fallback_account": fallback_account,
        "enabled": bool(row.enabled),
        "user_id": row.user_id.strip(),
        "access_token": row.access_token.strip(),
        "refresh_token": row.refresh_token.strip(),
        "cookie": row.cookie.strip(),
        "browser_state": row.browser_state.strip() if auth == "browser" and row.checkin_action != "relogin" else "",
        "proxy": row.proxy.strip(),
        "verify_ssl": accounts_store.parse_enabled(row.verify_ssl, True),
        # 这几项也要进快照，否则改动它们不会把配置标记为「未保存」。
        "cookie_file": row.cookie_file.strip(),
        "referer_path": row.referer_path.strip() or _REFERER_PATH_DEFAULT,
        "browser_profile": row.browser_profile.strip() or _BROWSER_PROFILE_DEFAULT,
        "auto_refresh_cookie": accounts_store.parse_enabled(row.auto_refresh_cookie, True),
        "login_selector": row.login_selector.strip(),
    }


def config_snapshot(rows: list[SiteRow], oauth_states: dict[str, Any]) -> str:
    """内存配置的规范化指纹，用于脏状态比较。"""
    payload = {
        "accounts": [_snapshot_row(row) for row in rows],
        "oauth_states": accounts_store.normalize_oauth_states(deepcopy(oauth_states)),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def credential_snapshot(row: SiteRow) -> dict[str, str]:
    """GUI 成功保存时的凭据基线；用稳定 runtime_id 关联，仅在当前进程内存在。"""
    return {
        "name": row.name.strip(),
        "base_url": accounts_store.normalize_base_url(row.base_url),
        "access_token": row.access_token.strip(),
        "refresh_token": row.refresh_token.strip(),
        "browser_state": row.browser_state.strip(),
    }


def credential_snapshots(rows: list[SiteRow]) -> dict[str, dict[str, str]]:
    """按 SiteRow.runtime_id 建立基线，避免对象销毁后 CPython id 被复用。"""
    return {row.runtime_id: credential_snapshot(row) for row in rows}


def changed_credential_fields(row: SiteRow, saved: dict[str, str] | None) -> set[str]:
    """返回相对上次成功保存真正变化的凭据字段（空值变化也保留）。"""
    current = credential_snapshot(row)
    if saved is None:
        return {"access_token", "refresh_token", "browser_state"}
    return {
        field
        for field in ("access_token", "refresh_token", "browser_state")
        if current[field] != str(saved.get(field) or "")
    }


def validate_rows(rows: list[SiteRow]) -> str | None:
    """保存前校验；返回错误文案，None 表示通过。"""
    for row in rows:
        if not row.name.strip():
            return "存在空的站点名称。"
        if not row.base_url.strip():
            return f"「{row.name}」缺少站点地址。"
    names = [row.name.strip() for row in rows]
    if len(names) != len(set(names)):
        return "站点名称重复，请改为唯一名称。"
    for row in rows:
        # browser_script 必须有脚本；api 的脚本是可选增强，留空即走默认签到。
        if row.checkin_action == "browser_script" and not row.script.strip():
            return f"「{row.name or '未命名站点'}」选择了自定义浏览器脚本，但未填写脚本路径。"
        if row.checkin_action not in {"browser_script", "api"} or not row.script.strip():
            continue
        error = _script_path_error(row.script.strip())
        if error:
            return f"「{row.name or '未命名站点'}」的脚本路径{error}"
        text = row.script_args_text.strip() or "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return f"「{row.name or '未命名站点'}」的脚本参数不是合法 JSON：{exc}"
        if not isinstance(parsed, dict):
            return f"「{row.name or '未命名站点'}」的脚本参数必须是 JSON 对象。"
    return None


def _script_path_error(script_path: str) -> str | None:
    """脚本路径是否可用；可用返回 None，否则返回接在「脚本路径」后面的原因。

    在保存时就查，而不是等到签到当天才在日志里失败：路径写错（打错字、写了绝对路径）
    是最常见的配置错误，而签到往后台跑，报错要隔很久才会被看到。
    """
    try:
        from browser import script_loader

        script_loader.resolve_script_path(script_path)
    except Exception as exc:  # noqa: BLE001 - 校验失败只需给文案
        return f"不可用：{exc}"
    return None


def validate_export(rows: list[SiteRow]) -> str | None:
    enabled_rows = [row for row in rows if row.enabled]
    if not enabled_rows:
        return "没有启用的站点可导出到 GitHub Secret。"
    for row in enabled_rows:
        if not row.name.strip():
            return "启用站点中存在空的站点名称。"
        if not row.base_url.strip():
            return f"「{row.name or '未命名站点'}」缺少站点地址。"
    names = [row.name.strip() for row in enabled_rows]
    if len(names) != len(set(names)):
        return "启用站点名称重复，请改为唯一名称或禁用重复项。"
    return None


def persist_accounts(rows: list[SiteRow]) -> list[dict[str, Any]]:
    """GUI 行 → ACCOUNTS.json 账号条目（假定 validate_rows 已通过）。"""
    accts: list[dict[str, Any]] = []
    for row in rows:
        t = row.type if row.type in TYPES else "newapi"
        auth = row.auth
        action = row.checkin_action if row.checkin_action in CHECKIN_ACTIONS else "api"
        acct: dict[str, Any] = {
            "name": row.name,
            "base_url": accounts_store.normalize_base_url(row.base_url),
            "site_profile": t,
            "auth_method": auth,
            "checkin_action": action,
            "enabled": bool(row.enabled),
            "user_id": row.user_id,
            "access_token": row.access_token,
            "refresh_token": row.refresh_token,
            "cookie": row.cookie,
        }
        if action in {"api", "browser_script"} and row.script.strip():
            acct["script"] = row.script.strip()
        if action == "browser_script":
            text = row.script_args_text.strip() or "{}"
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = accounts_store.normalize_script_args(row.script_args)
            acct["script_args"] = parsed if isinstance(parsed, dict) else {}
            acct["script_timeout"] = accounts_store.parse_script_timeout(row.script_timeout)
        if auth == "oauth" or action == "relogin":
            acct["oauth_provider"] = accounts_store.normalize_oauth_provider(row.oauth_provider) or "linuxdo"
            acct["oauth_account"] = accounts_store.normalize_oauth_account(row.oauth_account)
        fallback_provider, fallback_account = normalized_fallback(row)
        if fallback_provider:
            acct["oauth_fallback_provider"] = fallback_provider
            acct["oauth_fallback_account"] = fallback_account
        if t == "newapi" and action == "api":
            acct["api_variant"] = row.api_variant if row.api_variant in API_VARIANTS else "auto"
            acct["verification_mode"] = accounts_store.normalize_verification_mode(
                row.verification_mode
            )
        if row.proxy.strip():
            acct["proxy"] = row.proxy.strip()
        if not accounts_store.parse_enabled(row.verify_ssl, True):
            acct["verify_ssl"] = False
        state_text = row.browser_state.strip()
        if state_text and auth == "browser" and action != "relogin":
            acct["browser_state"] = state_text
        # 以下几项 checkin.py / run__all_checkin.py 都会消费。此前 GUI 不写回，
        # 用户在 ACCOUNTS.json 手写的值会被「保存全部」静默抹掉。只在非默认值时
        # 落盘，避免给每个账号都塞进一堆等于默认的噪声键。
        if row.cookie_file.strip():
            acct["cookie_file"] = row.cookie_file.strip()
        referer = row.referer_path.strip()
        if referer and referer != _REFERER_PATH_DEFAULT:
            acct["referer_path"] = referer
        if not accounts_store.parse_enabled(row.auto_refresh_cookie, True):
            acct["auto_refresh_cookie"] = False
        if auth in ("browser", "oauth"):
            profile = row.browser_profile.strip()
            if profile and profile != _BROWSER_PROFILE_DEFAULT:
                acct["browser_profile"] = profile
        # login_selector 是仅兼容保留的旧字段，没有输入框，但必须原样往返。
        if row.login_selector.strip():
            acct["login_selector"] = row.login_selector.strip()
        accts.append(acct)
    return accts


def apply_credential_cache_changes(
    rows: list[SiteRow],
    saved_credentials: dict[str, dict[str, str]],
) -> int:
    """保存配置后标记「凭据已变」，但不再删除任何运行期缓存。

    以前这里会在凭据变化、改名、删行时删除/清空 token_cache 条目。那是有害的：
    删除不可逆——用户改错又改回来、或只是重排/改名，本来仍然有效的运行期 token
    与登录态就被永久丢掉，下次运行必须重新走一遍 Turnstile / OAuth 登录。

    现在只写一个时间戳（`token_invalidated_at` / `state_invalidated_at`），由读取侧
    按「标记时间 vs 写入时间」决定是否使用；带 basis 的条目本来就由 basis 判定，
    配置改回原值即可自动重新命中。返回被标记的条目/字段组数量。
    """
    try:
        from providers import token_cache
    except Exception:
        return 0

    changed = 0
    current_ids = {row.runtime_id for row in rows}
    all_fields = {"access_token", "refresh_token", "browser_state"}

    # 已删除的行：标记而非清空，重建同名渠道时不会误继承旧凭据，也不丢数据。
    for row_id, old in saved_credentials.items():
        if row_id in current_ids:
            continue
        if token_cache.mark_credentials_changed(old.get("name", ""), old.get("base_url", ""), all_fields):
            changed += 1

    for row in rows:
        current = credential_snapshot(row)
        old = saved_credentials.get(row.runtime_id)
        name, base = current["name"], current["base_url"]
        if old is None or (old.get("name", ""), old.get("base_url", "")) != (name, base):
            # 新行或改了身份：旧身份与新身份都打标记，等价于「这套凭据换了」。
            if old is not None and token_cache.mark_credentials_changed(
                old.get("name", ""), old.get("base_url", ""), all_fields
            ):
                changed += 1
            if token_cache.mark_credentials_changed(name, base, all_fields):
                changed += 1
            continue

        fields = changed_credential_fields(row, old)
        mark: set[str] = set()
        if fields & {"access_token", "refresh_token"}:
            mark.update({"access_token", "refresh_token"})
        if "browser_state" in fields:
            mark.add("browser_state")
        if mark and token_cache.mark_credentials_changed(name, base, mark):
            changed += 1
    return changed


def reconcile_token_cache(rows: list[SiteRow]) -> int:
    """旧调用兼容：把当前所有凭据视作显式变化，按字段组失效缓存。"""
    return apply_credential_cache_changes(rows, {})


# ── 剪贴板导入 / 凭据导出 ────────────────────────────────────────────────────
def parse_clipboard_site(text: str) -> tuple[dict[str, Any] | None, str]:
    """剪贴板 JSON → 站点字段子集；返回 (data, error)。"""
    if not (text or "").strip():
        return None, "剪贴板为空。"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None, "剪贴板内容不是合法 JSON。"
    if isinstance(data, list) and data:
        data = data[0]
    if not isinstance(data, dict):
        return None, "JSON 结构无法识别。"
    if "name" not in data and "base_url" not in data and len(data) == 1:
        key, value = next(iter(data.items()))
        if isinstance(value, dict):
            value.setdefault("name", key)
            data = value
    return data, ""


_CLIPBOARD_SITE_FIELDS = frozenset({
    "name",
    "base_url",
    "url",
    "site_profile",
    "type",
    "provider",
    "auth_method",
    "checkin_action",
    "script",
    "script_args",
    "script_timeout",
    "api_variant",
    "oauth_provider",
    "oauth_account",
    "oauth_account_id",
    "oauth_fallback_provider",
    "oauth_fallback_account",
    "enabled",
    "user_id",
    "access_token",
    "refresh_token",
    "cookie",
    "browser_state",
    "proxy",
    "verify_ssl",
    "cookie_file",
    "token_file",
    "referer_path",
    "browser_profile",
    "auto_refresh_cookie",
    "login_selector",
})


def merge_clipboard_site(row: SiteRow, data: dict[str, Any]) -> SiteRow:
    """把 collector/凭据 JSON 安全合并到现有行，并复用统一规范化规则。

    未出现在剪贴板中的字段保持原值；只接受配置 schema 内的已知字段，避免把
    任意 JSON 键带进 GUI 模型。runtime_id 属于当前 GUI 身份，合并后必须保持不变。
    """
    merged = row.to_legacy()
    for key in _CLIPBOARD_SITE_FIELDS:
        if key in data:
            merged[key] = data[key]
    # collector 使用 site_profile；旧剪贴板可能只给 type/provider。显式的新字段优先。
    if "site_profile" in data:
        merged["site_profile"] = data["site_profile"]
    elif "type" in data:
        merged["site_profile"] = data["type"]
    elif "provider" in data:
        merged["site_profile"] = data["provider"]
    normalized = row_from_store(merged)
    normalized.runtime_id = row.runtime_id
    return normalized


def cred_json(row: SiteRow) -> str | None:
    cred = {k: getattr(row, k) for k in CRED_FIELDS if getattr(row, k)}
    if not cred:
        return None
    return json.dumps(cred, ensure_ascii=False, indent=2)


# ── 额度 / 状态文案 ──────────────────────────────────────────────────────────
def format_usd(value: float) -> str:
    """美元展示（唯一实现在 providers.base；GUI 侧输入已是 USD 数值）。"""
    from providers.base import format_usd as _format_usd

    return _format_usd(value, is_usd=True)


def detail_quota_usd(detail: dict[str, Any] | None) -> float | None:
    """从签到结果 detail 提取美元额度（唯一实现在 providers.base）。"""
    from providers.base import detail_quota_usd as _detail_quota_usd

    return _detail_quota_usd(detail)


def _failure_meta(status: str):
    meta = status_meta(status)
    return meta if meta.failure_prefix else status_meta("error")


def failure_label(status: str, *, compact: bool = False) -> str:
    meta = _failure_meta(status)
    return meta.compact_label if compact else meta.gui_label


def failure_toast(status: str, message: str) -> str:
    prefix = _failure_meta(status).failure_prefix
    return f"{prefix}：{message}" if message else prefix


# ── 稳定任务身份 / 站点租约 ──────────────────────────────────────────────────
@dataclass(frozen=True)
class TaskSnapshot:
    row_id: str
    name: str
    base_url: str
    status_key: str
    channel_key: str
    site_group_key: str
    params: dict[str, Any]


def make_task_snapshot(
    row: SiteRow,
    oauth_states: dict[str, Any],
    *,
    explicit_credential_fields: set[str] | frozenset[str] = frozenset(),
) -> TaskSnapshot:
    return TaskSnapshot(
        row_id=row.runtime_id,
        name=row.name.strip() or "未命名站点",
        base_url=accounts_store.normalize_base_url(row.base_url),
        status_key=StatusStore.status_key(row),
        channel_key=StatusStore.task_key(row),
        site_group_key=StatusStore.site_group_key(row),
        params=task_params(
            row,
            oauth_states,
            explicit_credential_fields=explicit_credential_fields,
        ),
    )


@dataclass(frozen=True)
class TaskLease:
    token: str
    site_key: str
    channel_keys: frozenset[str]


class TaskLeaseRegistry:
    """统一单签/批量的站点级租约；状态归属仍按渠道 key 区分。"""

    def __init__(self) -> None:
        self._sites: dict[str, str] = {}
        self._channels: dict[str, str] = {}

    def acquire(self, site_key: str, channel_keys: set[str] | frozenset[str]) -> TaskLease | None:
        site = str(site_key or "").strip()
        channels = frozenset(str(key or "").strip() for key in channel_keys if str(key or "").strip())
        if not site or not channels:
            return None
        if site in self._sites or any(key in self._channels for key in channels):
            return None
        token = uuid.uuid4().hex
        self._sites[site] = token
        for key in channels:
            self._channels[key] = token
        return TaskLease(token=token, site_key=site, channel_keys=channels)

    def acquire_single(self, row: SiteRow) -> TaskLease | None:
        return self.acquire(StatusStore.site_group_key(row), {StatusStore.task_key(row)})

    def acquire_group(self, snapshots: list[TaskSnapshot]) -> TaskLease | None:
        if not snapshots:
            return None
        return self.acquire(
            snapshots[0].site_group_key,
            {snapshot.channel_key for snapshot in snapshots},
        )

    def release(self, lease: TaskLease | None) -> bool:
        if lease is None or self._sites.get(lease.site_key) != lease.token:
            return False
        self._sites.pop(lease.site_key, None)
        for key in lease.channel_keys:
            if self._channels.get(key) == lease.token:
                self._channels.pop(key, None)
        return True

    def is_channel_running(self, channel_key: str) -> bool:
        return str(channel_key or "").strip() in self._channels

    def is_site_running(self, site_key: str) -> bool:
        return str(site_key or "").strip() in self._sites

    @property
    def running_channels(self) -> int:
        return len(self._channels)


# ── 概览统计 ─────────────────────────────────────────────────────────────────
@dataclass
class Stats:
    total: int = 0
    enabled: int = 0
    done: int = 0
    failed: int = 0
    quota_sum: float = 0.0
    quota_known: int = 0


def summarize(rows: list[SiteRow], store: StatusStore) -> Stats:
    stats = Stats(total=len(rows))
    for row in rows:
        if row.enabled:
            stats.enabled += 1
        entry = store.get(StatusStore.status_key(row)) or {}
        if entry.get("checked_in") is True:
            stats.done += 1
        if entry.get("ok") is False:
            stats.failed += 1
        quota = entry.get("quota_usd")
        if isinstance(quota, (int, float)):
            stats.quota_sum += float(quota)
            stats.quota_known += 1
    return stats
