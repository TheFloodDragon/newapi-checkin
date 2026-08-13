"""跨 CLI、GUI、provider 与 worker 共享的有限状态契约。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar


class SiteProfileName(StrEnum):
    NEWAPI = "newapi"
    SUB2API = "sub2api"


class AuthMethod(StrEnum):
    ACCESS_TOKEN = "access_token"
    COOKIE = "cookie"
    BROWSER = "browser"
    OAUTH = "oauth"


class CheckinAction(StrEnum):
    API = "api"
    VISIT = "visit"
    RELOGIN = "relogin"
    BROWSER_SCRIPT = "browser_script"


class ApiVariant(StrEnum):
    """newapi + api 的接口变体偏好；两者互为兜底，只决定先试哪条。"""

    AUTO = "auto"
    LEGACY = "legacy"


class VerificationMode(StrEnum):
    AUTO = "auto"
    TURNSTILE = "turnstile"
    BITMAP_CODE = "bitmap_code"
    STRING_CAPTCHA = "string_captcha"
    CLICK_SHAPE = "click_shape"


class ResultStatus(StrEnum):
    SUCCESS = "success"
    ALREADY_DONE = "already_done"
    # 站点活动未开放（未到开始时间/已结束/管理员停用）。属于告警而非失败：
    # 不是本工具或账号的问题，重试不会改变结果，因此不计入失败也不触发重跑。
    NOT_OPEN = "not_open"
    NEED_LOGIN = "need_login"
    NEED_VERIFICATION = "need_verification"
    NEED_CONFIG = "need_config"
    NETWORK_ERROR = "network_error"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StatusMeta:
    label: str
    icon: str
    ok: bool = False
    gui_label: str = ""
    compact_label: str = ""
    failure_prefix: str = ""


STATUS_META: dict[ResultStatus, StatusMeta] = {
    ResultStatus.SUCCESS: StatusMeta("成功", "✅", True, "✅ 签到成功", "✅ 成功"),
    ResultStatus.ALREADY_DONE: StatusMeta("已领取", "🎁", True, "🎁 今日已完成", "🎁 已完成"),
    ResultStatus.NOT_OPEN: StatusMeta("未开放", "🚧", True, "🚧 活动未开放", "🚧 未开放"),
    ResultStatus.NEED_LOGIN: StatusMeta(
        "登录失效", "🔐", gui_label="🔐 登录失效", compact_label="🔐 失效", failure_prefix="登录失效"
    ),
    ResultStatus.NEED_VERIFICATION: StatusMeta(
        "需验证", "⚠️", gui_label="⚠ 需人机验证", compact_label="⚠ 验证", failure_prefix="需要验证"
    ),
    ResultStatus.NEED_CONFIG: StatusMeta(
        "需配置", "🛠️", gui_label="⚙ 配置缺失", compact_label="⚙ 配置", failure_prefix="配置缺失"
    ),
    ResultStatus.NETWORK_ERROR: StatusMeta(
        "网络错误", "❌", gui_label="🌐 站点不可达", compact_label="🌐 不可达", failure_prefix="站点不可达/网络异常"
    ),
    ResultStatus.ERROR: StatusMeta(
        "失败", "❌", gui_label="❌ 查询失败", compact_label="❌ 失败", failure_prefix="查询失败"
    ),
}

PROFILE_VALUES = tuple(item.value for item in SiteProfileName)
AUTH_METHOD_VALUES = tuple(item.value for item in AuthMethod)
ACTION_VALUES = tuple(item.value for item in CheckinAction)
API_VARIANT_VALUES = tuple(item.value for item in ApiVariant)
# 默认先走 legacy：实测全部在用站点的 /api/user/checkin/challenge 与
# /static/wasm/checkin-vm-v4.wasm 均返回 404，challenge 优先只会白启一个 Node
# 子进程（实测每站约 1.2s）再回落。legacy 失败且站点明确提示流程已升级时，
# 仍会自动切 challenge，因此上游若恢复该协议无需改配置。
DEFAULT_API_VARIANT = ApiVariant.LEGACY.value
VERIFICATION_MODE_VALUES = tuple(item.value for item in VerificationMode)
VALID_RESULT_STATUSES = frozenset(item.value for item in ResultStatus)
OK_STATUSES = frozenset({ResultStatus.SUCCESS.value, ResultStatus.ALREADY_DONE.value})

_EnumT = TypeVar("_EnumT", bound=StrEnum)


def parse_enum(enum_type: type[_EnumT], value: object, default: _EnumT) -> _EnumT:
    """把外部字符串收敛为 StrEnum；未知值安全回落。"""
    try:
        return enum_type(str(value or "").strip().lower())
    except ValueError:
        return default


def parse_result_status(value: object, default: ResultStatus = ResultStatus.ERROR) -> ResultStatus:
    return parse_enum(ResultStatus, value, default)


def status_meta(value: object) -> StatusMeta:
    return STATUS_META[parse_result_status(value)]
