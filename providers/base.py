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
import math
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0"
)

IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "PUT", "DELETE"}

SESSION_COOKIE_NAMES = {"session", "newapi_session", "new-api-session", "new_api_session"}

# 人机验证特征唯一词表（contains_any 匹配时双方都转小写，大小写无关）。
# 只收高置信标记；「验证」「verify」这类宽泛词交由个别 profile/action 按需追加
# （见 profiles/sub2api.py、actions/visit.py），避免把「token 验证失败」这类
# 登录问题误分类为 need_verification。
VERIFICATION_PATTERNS = [
    "turnstile",
    "cloudflare",
    "just a moment",
    "安全验证",
    "challenge-platform",
    "人机",
    "captcha",
    # 「验证码」比宽泛的「验证」具体得多，不会误伤「token 验证失败」这类登录报错，
    # 而图形验证码本身就属于人机验证 —— 缺它被拒时应归 need_verification。
    "验证码",
]

# 网络层重试：对瞬时性错误（429 / 5xx / 连接超时）做指数退避重试。
# 不重试 4xx（除 429）这类确定性错误，避免无意义的重复请求。
# 具体数值集中在 config，这里保留同名别名以兼容既有引用。
from config import LogConfig as _LogConfig  # noqa: E402
from config import RetryConfig as _RetryConfig  # noqa: E402
from config import Timeouts as _Timeouts  # noqa: E402

# base_url 归一化唯一实现在 accounts_store（最底层模块，避免反向循环导入）；
# 这里 re-export 供 profiles/actions 继续从 providers.base 引用。
from accounts_store import normalize_base_url  # noqa: E402, F401

RETRY_MAX_ATTEMPTS = _RetryConfig.MAX_ATTEMPTS   # 含首次在内的总尝试次数
RETRY_BACKOFF_BASE = _RetryConfig.BACKOFF_BASE   # 退避基数（秒）：第 n 次失败后等待约 base * 2**n
RETRY_BACKOFF_CAP = _RetryConfig.BACKOFF_CAP     # 单次退避上限（秒）
RETRY_STATUS_CODES = set(_RetryConfig.STATUS_CODES)
HTTP_REQUEST_TIMEOUT = _Timeouts.HTTP_REQUEST    # 单次 HTTP 请求默认超时（秒）

# New API 内部 quota 与 USD 换算系数：quota / 500000 = $
QUOTA_UNIT = 500_000


# ── 站点原始返回值日志 ──────────────────────────────────────────────────────
def _brief_payload(payload: Any, limit: int) -> str:
    """把响应体压成单行、限长的可读文本。"""
    if isinstance(payload, (dict, list)):
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(payload)
    else:
        text = str(payload)
    text = " ".join(text.split())
    if len(text) > limit:
        return f"{text[:limit]}…（共 {len(text)} 字符，已截断）"
    return text


def log_http_exchange(
    tag: str,
    method: str,
    url: str,
    *,
    payload: Any = None,
    error: BaseException | None = None,
    log: Any = None,
) -> None:
    """记录一次 HTTP 交互的站点原始返回值（写 stderr，经脱敏与限长）。

    为什么必须打出来：签到失败时最有用的信息是站点到底回了什么。此前各层只把
    message 往上传，原始返回值只留在 ApiError.payload 里且默认不打印，于是
    「签到失败」「验证码错误」这类结论无法复核——尤其站点静默改了业务码、或把
    JSON 换成 HTML 挑战页时，日志里看不出任何区别。

    tag 用站点名，前缀 [http:...] 与其它阶段日志一致，批量汇总会原样透出。
    """
    if not _LogConfig.HTTP_BODY:
        return
    from mask_utils import mask_secrets

    limit = _LogConfig.HTTP_BODY_MAX
    if error is not None:
        status = getattr(error, "status", None)
        body = getattr(error, "payload", None)
        detail = _brief_payload(body, limit) if body not in (None, "") else str(error)
        suffix = f" status={status}" if status else ""
        line = f"[http:{tag}] {method.upper()} {url} 失败{suffix} → {detail}"
    else:
        line = f"[http:{tag}] {method.upper()} {url} → {_brief_payload(payload, limit)}"
    line = mask_secrets(line)
    if log:
        log(line)
        return
    print(line, file=sys.stderr, flush=True)


# ── 额度换算 / 展示 / detail 提取（唯一实现；CLI、GUI、browser 共用）────────────
def quota_usd_value(value: Any, *, is_usd: bool = False) -> float | None:
    """把站点额度数值换算为美元；非数字（含 bool）或非有限值返回 None。

    NaN / Infinity 必须挡在这里：Python 的 json 会把它们写成裸 NaN/Infinity
    （非标准 JSON），并且 NaN 参与任何比较都为 False，会让「额度是否增长」
    的交叉验证静默失效、GUI 总额度变成 NaN。
    """
    if isinstance(value, bool):
        return None
    try:
        usd = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(usd):
        return None
    return usd if is_usd else usd / QUOTA_UNIT


def has_awarded_amount(value: Any, *, is_usd: bool = False) -> bool:
    """本次签到是否有「值得展示的获得额度」。

    0 一律视为「没有金额信息」而不是「获得 $0」：站点签到成功但响应里不带具体
    金额时常回 reward_amount=0 / quota=0，此前会被拼成「签到成功，获得额度：
    $0.0000」——既不是事实（并非奖励 0 元），也让用户以为签到出了问题。
    非数字（None / "" / 文本）同样返回 False，交由调用方走无金额分支。
    """
    usd = quota_usd_value(value, is_usd=is_usd)
    return usd is not None and abs(usd) > 0


def format_usd(value: Any, *, is_usd: bool = False, fallback: str | None = None) -> str:
    """美元展示：>=0.01 两位小数，否则四位小数；非数字返回 fallback（默认原样字符串）。"""
    usd = quota_usd_value(value, is_usd=is_usd)
    if usd is None:
        if fallback is not None:
            return fallback
        return str(value) if value is not None else ""
    return f"${usd:.2f}" if abs(usd) >= 0.01 else f"${usd:.4f}"


def find_first_value(data: Any, keys: list[str]) -> Any:
    """在嵌套 dict/list 中按键名（不区分大小写）BFS 查找第一个非空值。

    使用 deque 做队列，popleft() 为 O(1)，避免 list.pop(0) 的 O(n) 开销；
    seen 记录已访问对象 id，防止循环引用导致的无限遍历。
    """
    wanted = {key.lower() for key in keys}
    queue: deque[Any] = deque([data])
    seen: set[int] = set()

    while queue:
        item = queue.popleft()
        if item is None:
            continue
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)

        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).lower() in wanted and value not in (None, ""):
                    return value
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return None


_QUOTA_AWARDED_KEYS = [
    "quota_awarded",
    "awarded_quota",
    "award_quota",
    "reward_quota",
    "checkin_quota",
    "quota_reward",
]
_CURRENT_QUOTA_KEYS = [
    "current_quota",   # checkin.py 注入的标准字段
    "remaining_quota",
    "available_quota",
    "quota_remaining",
    "user_quota",
    "quota",
    "balance",
]


def detail_is_usd(detail: Any) -> bool:
    """provider 在 detail 里标记 quota_is_usd=true 时，额度无需 /500000 换算。"""
    return bool(find_first_value(detail, ["quota_is_usd"]))


def detail_quota_awarded(detail: Any) -> Any:
    return find_first_value(detail, _QUOTA_AWARDED_KEYS)


def detail_current_quota(detail: Any) -> Any:
    return find_first_value(detail, _CURRENT_QUOTA_KEYS)


def detail_quota_usd(detail: Any) -> float | None:
    """从签到/查询结果 detail 提取当前余额（美元）；未知返回 None。"""
    return quota_usd_value(detail_current_quota(detail), is_usd=detail_is_usd(detail))


@dataclass
class RuntimeCredentialContext:
    """运行期凭据解析上下文，不属于用户配置，也不得持久化或输出。

    token_basis/state_basis 是 ACCOUNTS/Secret 中凭据种子的不可逆摘要。运行期刷新
    产生的 token/browser_state 只有在 basis 与当前配置一致时才允许覆盖；用户更新
    Secret 后摘要变化，旧 Actions cache 会被自动忽略。

    explicit_fields 区分「未提供」和「显式清空」：GUI/CLI 明确传入的字段始终优先
    于缓存，即使值是空字符串。cache_policy=ignore 用于父进程已解析凭据后的 worker，
    防止子进程再次应用缓存。
    """

    token_basis: str = ""
    state_basis: str = ""
    explicit_fields: frozenset[str] = field(default_factory=frozenset)
    cache_policy: str = "compatible"


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
    script_timeout: int = _Timeouts.BROWSER_SCRIPT_DEFAULT
    # ── 凭据 ──
    cookie: str = ""
    user_id: str = ""
    access_token: str = ""
    # Sub2API 系的长效刷新令牌。access_token 是短期 JWT，过期后纯 HTTP 路径可用它
    # 直接调 /api/v1/auth/refresh 续期，无需为常见的「token 过期」拉起浏览器。
    refresh_token: str = ""
    cookie_file: str = ""
    browser_state: str = ""
    # ── 浏览器 / 网络 ──
    browser_profile: str = ".browser_profile"
    login_selector: str = ""  # 旧字段，仅兼容保留；relogin 已改用 oauth_provider 拼授权 URL
    oauth_provider: str = "linuxdo"  # auth_method=oauth 时使用的共享登录态
    oauth_account: str = "default"
    oauth_fallback_provider: str = ""  # Token 失效后的可选 OAuth；空表示禁用
    oauth_fallback_account: str = "default"
    proxy: str = ""
    referer_path: str = "/profile"
    # ── 其它 ──
    enabled: bool = True
    auto_refresh_cookie: bool = True
    # newapi + api 专用：接口变体偏好（auto=challenge 优先，legacy=旧接口优先）。
    # 仅影响首次尝试顺序，两种都会在失败时互为兜底；其它 profile 忽略。
    api_variant: str = "auto"
    # newapi + api 的验证机制偏好。auto 自动探测；其它值仅表示优先，确认不适用时
    # 仍回落自动分流。有限值由 checkin_core.enums.VerificationMode 维护。
    verification_mode: str = "auto"
    # TLS 证书校验开关。默认开启；仅当站点证书过期/自签名导致 CERTIFICATE_VERIFY_FAILED
    # 时，才在配置里显式设为 false 作为应急兜底（跳过校验有中间人风险，谨慎使用）。
    verify_ssl: bool = True
    # 仅供运行期缓存解析/写回使用；不属于 ACCOUNTS/Secret 配置语义。
    runtime_credentials: RuntimeCredentialContext = field(
        default_factory=RuntimeCredentialContext,
        repr=False,
        compare=False,
    )


@dataclass
class AuthInfo:
    """HTTP 认证凭据（access_token / cookie 登录方式产出）。"""

    cookie: str = ""
    new_api_user: str = ""
    access_token: str = ""
    # 浏览器刷新认证时顺便读到的用户信息（含 quota/username/id）。
    # 阿里云 WAF 类站点（如 anyrouter）用浏览器执行 JS 挑战后能读到额度，但导出的
    # WAF cookie 被 urllib 复用时常被重新挑战（urllib 不执行 JS）。此时把浏览器已
    # 读到的用户信息作为 HTTP fetch_user 被 WAF 拦截时的权威兜底，避免误报失败。
    prefetched_user: dict[str, Any] | None = None


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
    # 接口返回 2xx，但响应体里没有任何「签到确实成立」的正面证据（无奖励额度、
    # 无连续天数、无已签标记）。实测存在站点静默拒绝或端点并非签到接口的情况，
    # 此时若直接报成功就会出现「显示签到成功但额度未到账」。置 True 时由 action
    # 层用签到前后余额差 / 状态接口做交叉验证，验证不到证据则不谎报成功。
    checkin_unconfirmed: bool = False


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
        """把站点原始额度换算为美元；非数字或非有限值返回 None。"""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(v):
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

    def build_lazy_refresh_client(self, site: SiteConfig) -> ProfileClient | None:
        """缓存优先的惰性刷新客户端（oauth / browser 场景）。

        与 refresh_auth_via_browser 的「每次都先启动浏览器刷新」不同：这里用配置里
        已缓存的 access_token 直接构造客户端，仅当接口返回登录失效（401/token 过期）
        时，才在请求层按需启动一次浏览器 OAuth/refresh_token 刷新并重试。
        这样有效期内的 token 可纯 HTTP 直调，避免每次签到都拉起浏览器。

        不支持或没有可用缓存 token 时返回 None，由 build_http_client 回退到
        refresh_auth_via_browser 的即时刷新路径。
        """
        return None

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
    """规范化 access_token；非 ASCII 内容视为无效并返回空串。

    HTTP 头只能承载 latin-1 字符。配置里若残留占位文本（如「<在站点后台采集的
    access_token>」或带省略号的截断值），直接塞进 Authorization 头会在请求发出
    *之前* 抛 UnicodeEncodeError；该异常不是 ApiError，会绕过 need_login 判定，
    导致「明明有可用的 refresh_token 却从不续期」。这里提前判为无 token，让调用
    方走 refresh_token / 账密登录等正常降级路径。
    """
    value = (value or "").strip()
    if value.lower().startswith("authorization:"):
        value = value.split(":", 1)[1].strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not value:
        return ""
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return ""
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


def _build_url_opener(proxy: str = "", verify_ssl: bool = True, cookie_jar: Any = None) -> urllib.request.OpenerDirector:
    """构造不依赖进程隐式代理环境的 opener。

    verify_ssl=False 时禁用 TLS 证书与主机名校验（用于证书过期/自签名的应急兜底）。

    cookie_jar 非空时挂上 HTTPCookieProcessor，让同一会话的多次请求复用 Set-Cookie。
    部分 Sub2API 站点（实测极速蹬）会把会话绑定到网络/客户端指纹，登录时下发的
    cookie 若不带回，后续请求即被拒：
        {"code":401,"message":"Session network fingerprint changed, please login again"}
    默认仍不带 cookie jar，保持其它 profile 的无状态请求语义不变。
    """
    handlers: list[urllib.request.BaseHandler] = []
    if cookie_jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookie_jar))
    if not verify_ssl:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))

    proxy = str(proxy or "").strip()
    if not proxy:
        handlers.append(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(*handlers)
    parsed = urllib.parse.urlsplit(proxy)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        if parsed.scheme.startswith("socks"):
            raise ApiError(None, None, "标准库 HTTP 客户端不支持 SOCKS 代理，请改用 http/https 代理。")
        raise ApiError(None, None, "代理地址无效，必须是 http:// 或 https:// URL。")
    handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def _http_request_once(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: int,
    proxy: str,
    verify_ssl: bool = True,
    cookie_jar: Any = None,
) -> Any:
    """单次 HTTP 请求并解析 JSON；HTTP 错误也尽量解析 body，统一抛 ApiError。"""
    req = urllib.request.Request(url, data=body, headers=headers, method=method.upper())
    opener = _build_url_opener(proxy, verify_ssl=verify_ssl, cookie_jar=cookie_jar)
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
    timeout: int = HTTP_REQUEST_TIMEOUT,
    max_attempts: int = RETRY_MAX_ATTEMPTS,
    proxy: str = "",
    retry_non_idempotent: bool = False,
    verify_ssl: bool = True,
    cookie_jar: Any = None,
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
                verify_ssl=verify_ssl,
                cookie_jar=cookie_jar,
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
