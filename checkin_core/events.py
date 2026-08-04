"""版本化、可脱敏的 worker 诊断事件协议。"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Callable

import time_utils
from mask_utils import sanitize_data

EVENT_PREFIX = "@checkin-event "
EVENT_VERSION = 1


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(slots=True)
class WorkerEvent:
    stage: str
    message: str
    level: EventLevel = EventLevel.INFO
    site: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    version: int = EVENT_VERSION
    created_at: str = field(default_factory=time_utils.utc_iso)

    def to_payload(self) -> dict[str, Any]:
        return sanitize_data(asdict(self))

    def to_line(self) -> str:
        return EVENT_PREFIX + json.dumps(
            self.to_payload(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def from_line(cls, line: str) -> WorkerEvent | None:
        text = str(line or "").strip()
        if not text.startswith(EVENT_PREFIX):
            return None
        try:
            payload = json.loads(text[len(EVENT_PREFIX):])
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("version") != EVENT_VERSION:
            return None
        try:
            level = EventLevel(str(payload.get("level") or EventLevel.INFO))
        except ValueError:
            return None
        fields = payload.get("fields")
        return cls(
            stage=str(payload.get("stage") or ""),
            message=str(payload.get("message") or ""),
            level=level,
            site=str(payload.get("site") or ""),
            fields=dict(fields) if isinstance(fields, dict) else {},
            version=EVENT_VERSION,
            created_at=str(payload.get("created_at") or ""),
        )


def emit_event(
    stage: str,
    message: str,
    *,
    level: EventLevel = EventLevel.INFO,
    site: str = "",
    fields: dict[str, Any] | None = None,
    sink: Callable[[str], None] | None = None,
) -> WorkerEvent:
    event = WorkerEvent(stage=stage, message=message, level=level, site=site, fields=dict(fields or {}))
    line = event.to_line()
    if sink is not None:
        sink(line)
    else:
        print(line, file=sys.stderr, flush=True)
    return event
