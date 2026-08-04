"""newapi-checkin 的无 UI、无存储副作用领域契约。"""

from .auth import (
    PasswordLoginCapable,
    TokenRefreshCapable,
    can_optional_oauth,
    effective_auth,
    infer_auth_method,
)
from .enums import (
    ACTION_VALUES,
    AUTH_METHOD_VALUES,
    OK_STATUSES,
    PROFILE_VALUES,
    STATUS_META,
    VALID_RESULT_STATUSES,
    AuthMethod,
    CheckinAction,
    ResultStatus,
    SiteProfileName,
    StatusMeta,
    parse_result_status,
    status_meta,
)
from .events import EVENT_PREFIX, EventLevel, WorkerEvent, emit_event

__all__ = [
    "ACTION_VALUES",
    "AUTH_METHOD_VALUES",
    "OK_STATUSES",
    "PROFILE_VALUES",
    "STATUS_META",
    "VALID_RESULT_STATUSES",
    "AuthMethod",
    "CheckinAction",
    "ResultStatus",
    "SiteProfileName",
    "StatusMeta",
    "parse_result_status",
    "status_meta",
    "PasswordLoginCapable",
    "TokenRefreshCapable",
    "can_optional_oauth",
    "effective_auth",
    "infer_auth_method",
    "EVENT_PREFIX",
    "EventLevel",
    "WorkerEvent",
    "emit_event",
]
