#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hCaptcha 浏览器交互核心。

本模块只编排浏览器中正常呈现的 hCaptcha 控件：读取页面签发的响应、使用真实
鼠标操作复选框，并在需要时把挑战截图交给可注入的视觉客户端。模型密钥、截图
字节和模型原始回复不会写入日志。
"""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Callable, Mapping, Sequence
from urllib.parse import urlparse
from dataclasses import dataclass, field, fields
from typing import Any, Literal

HCaptchaStatus = Literal[
    "success",
    "not_present",
    "not_configured",
    "unsupported",
    "uncertain",
    "timeout",
    "failed",
]

_TOKEN_JS = """() => {
    const fields = document.querySelectorAll(
        'textarea[name="h-captcha-response"], input[name="h-captcha-response"]'
    );
    for (const field of fields) {
        const value = String(field.value || field.textContent || '').trim();
        if (value) return {present: true, token: value};
    }
    return {present: fields.length > 0, token: ''};
}"""

_PROMPT_SELECTORS = (
    ".prompt-text",
    ".challenge-header .prompt-text",
    ".challenge-header",
    "[class*='prompt']",
)
_GRID_SELECTORS = (".task-image", "[class*='task-image']")
_SUBMIT_SELECTORS = (
    "button.button-submit",
    "button[type='submit']",
    ".button-submit",
    "[class*='submit']",
)
_CHECKBOX_SELECTORS = ("#checkbox", "[role='checkbox']", ".checkbox")
_CHALLENGE_MARKER_SELECTORS = (".challenge-container", ".challenge-view")
_INTERACTION_SELECTORS = (
    ".challenge-image",
    ".image-wrapper",
    "canvas",
    ".challenge-view img",
    *_CHALLENGE_MARKER_SELECTORS,
)
_SCREENSHOT_SELECTORS = (*_CHALLENGE_MARKER_SELECTORS, "body")
_ADD_GRID_LABELS_JS = r"""(selectors) => {
    const seen = new Set();
    const nodes = [];
    for (const selector of selectors) {
        for (const node of document.querySelectorAll(selector)) {
            if (!seen.has(node)) {
                seen.add(node);
                nodes.push(node);
            }
        }
    }
    nodes.forEach((node, index) => {
        if (getComputedStyle(node).position === 'static') {
            node.dataset.nfHcaptchaOldPosition = node.style.position || '';
            node.style.position = 'relative';
        }
        const badge = document.createElement('span');
        badge.dataset.nfHcaptchaIndex = String(index + 1);
        badge.textContent = String(index + 1);
        Object.assign(badge.style, {
            position: 'absolute', left: '4px', top: '4px', zIndex: '2147483647',
            minWidth: '22px', height: '22px', padding: '1px 4px', borderRadius: '4px',
            color: '#fff', background: '#d00', font: 'bold 16px/20px sans-serif',
            textAlign: 'center', pointerEvents: 'none', boxSizing: 'border-box'
        });
        node.appendChild(badge);
    });
    return nodes.length;
}"""
_REMOVE_GRID_LABELS_JS = r"""() => {
    for (const badge of document.querySelectorAll('[data-nf-hcaptcha-index]')) badge.remove();
    for (const node of document.querySelectorAll('[data-nf-hcaptcha-old-position]')) {
        node.style.position = node.dataset.nfHcaptchaOldPosition || '';
        delete node.dataset.nfHcaptchaOldPosition;
    }
}"""


@dataclass(slots=True)
class HCaptchaOptions:
    """求解限制和视觉后端配置；时间值均为毫秒。"""

    enabled: bool = True
    model: str | None = None
    base_url: str | None = None
    max_rounds: int = 2
    total_timeout_ms: int = 120_000
    round_timeout_ms: int = 30_000
    presence_timeout_ms: int = 8_000
    post_action_wait_ms: int = 5_000
    poll_interval_ms: int = 200
    confidence_threshold: float = 0.65
    frame_depth: int = 4

    @classmethod
    def from_value(cls, value: HCaptchaOptions | Mapping[str, Any] | None) -> HCaptchaOptions:
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise TypeError("options 必须是 HCaptchaOptions、映射或 None")

        aliases = {
            "timeout_ms": "total_timeout_ms",
            "total_timeout": "total_timeout_ms",
            "round_timeout": "round_timeout_ms",
            "presence_timeout": "presence_timeout_ms",
            "confidence": "confidence_threshold",
        }
        allowed = {field.name for field in fields(cls)}
        values: dict[str, Any] = {}
        for key, item in value.items():
            normalized = aliases.get(str(key), str(key))
            if normalized in allowed:
                values[normalized] = item
        return cls(**values)

    def normalized(self) -> HCaptchaOptions:
        return HCaptchaOptions(
            enabled=bool(self.enabled),
            model=self.model,
            base_url=self.base_url,
            max_rounds=max(1, int(self.max_rounds)),
            total_timeout_ms=max(1, int(self.total_timeout_ms)),
            round_timeout_ms=max(1, int(self.round_timeout_ms)),
            presence_timeout_ms=max(0, int(self.presence_timeout_ms)),
            post_action_wait_ms=max(0, int(self.post_action_wait_ms)),
            poll_interval_ms=max(10, int(self.poll_interval_ms)),
            confidence_threshold=min(1.0, max(0.0, float(self.confidence_threshold))),
            frame_depth=min(4, max(0, int(self.frame_depth))),
        )


@dataclass(slots=True)
class HCaptchaSolveResult:
    """结构化求解结果。令牌可供表单流程使用，但不会出现在 repr 中。"""

    status: HCaptchaStatus
    message: str
    token: str = field(default="", repr=False)
    rounds: int = 0
    challenge_type: str = ""
    screenshot: str = ""
    failure_stage: str = ""
    error_type: str = ""
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def __repr__(self) -> str:
        return (
            f"HCaptchaSolveResult(status={self.status!r}, message={self.message!r}, "
            f"token={'<redacted>' if self.token else ''!r}, rounds={self.rounds!r}, "
            f"challenge_type={self.challenge_type!r}, screenshot={self.screenshot!r})"
        )


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _count(locator: Any) -> int:
    try:
        return int(await _maybe_await(locator.count()))
    except Exception:
        return 0


async def _is_visible(locator: Any) -> bool:
    method = getattr(locator, "is_visible", None)
    if not callable(method):
        return True
    try:
        return bool(await _maybe_await(method()))
    except Exception:
        return False


class _VisibleLocators:
    """保留同一 selector 下所有可见元素，避免隐藏模板遮蔽真实控件。"""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    @property
    def first(self) -> Any:
        return self._items[0]

    def nth(self, index: int) -> Any:
        return self._items[index]

    async def count(self) -> int:
        return len(self._items)


async def _locator_collection(root: Any, selectors: Sequence[str]) -> Any | None:
    for selector in selectors:
        try:
            locator = root.locator(selector)
            visible: list[Any] = []
            for index in range(await _count(locator)):
                item = locator.nth(index)
                if await _is_visible(item):
                    visible.append(item)
            if visible:
                return _VisibleLocators(visible)
        except Exception:
            continue
    return None


async def _first_locator(root: Any, selectors: Sequence[str]) -> Any | None:
    locator = await _locator_collection(root, selectors)
    return locator.first if locator is not None else None


async def _text(locator: Any) -> str:
    if locator is None:
        return ""
    for method_name in ("inner_text", "text_content"):
        try:
            method = getattr(locator, method_name)
            return str(await _maybe_await(method()) or "").strip()
        except Exception:
            continue
    return ""


async def _box(locator: Any) -> dict[str, float] | None:
    try:
        raw = await _maybe_await(locator.bounding_box())
        if not isinstance(raw, Mapping):
            return None
        box = {key: float(raw[key]) for key in ("x", "y", "width", "height")}
        if box["width"] <= 0 or box["height"] <= 0:
            return None
        return box
    except Exception:
        return None


async def _screenshot_locator(page: Any, locator: Any) -> bytes:
    """优先从顶层 Page 裁剪 iframe 区域，避免 Firefox 元素截图返回纯黑图片。"""
    box = await _box(locator)
    if box is not None:
        try:
            image = await _maybe_await(page.screenshot(type="png", clip=box))
            if isinstance(image, (bytes, bytearray, memoryview)) and image:
                return bytes(image)
        except Exception:
            pass
    try:
        image = await _maybe_await(locator.screenshot(type="png"))
        if isinstance(image, (bytes, bytearray, memoryview)) and image:
            return bytes(image)
    except Exception:
        pass
    return b""


def _children(frame: Any) -> list[Any]:
    try:
        value = frame.child_frames
        return list(value() if callable(value) else value)
    except Exception:
        return []


def _frame_url(frame: Any) -> str:
    try:
        value = frame.url
        return str(value() if callable(value) else value or "")
    except Exception:
        return ""


def _is_hcaptcha_url(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "hcaptcha.com" or host.endswith(".hcaptcha.com")


def _walk_frames(root: Any, max_depth: int) -> list[Any]:
    found: list[Any] = []
    seen: set[int] = set()

    def visit(frame: Any, depth: int) -> None:
        if frame is None or id(frame) in seen or depth > max_depth:
            return
        seen.add(id(frame))
        found.append(frame)
        if depth < max_depth:
            for child in _children(frame):
                visit(child, depth + 1)

    visit(root, 0)
    return found


def _numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _point(value: Any) -> tuple[float, float] | None:
    if isinstance(value, Mapping):
        x, y = _numeric(value.get("x")), _numeric(value.get("y"))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        x, y = _numeric(value[0]), _numeric(value[1])
    else:
        return None
    if x is None or y is None or not (0 <= x <= 1000 and 0 <= y <= 1000):
        return None
    return x, y


def _answer_mapping(answer: Any) -> Mapping[str, Any] | None:
    if isinstance(answer, Mapping):
        return answer
    for name in ("model_dump", "dict"):
        method = getattr(answer, name, None)
        if callable(method):
            value = method()
            if isinstance(value, Mapping):
                return value
    return None


def _validated_answer(answer: Any, *, max_actions: int = 16) -> Mapping[str, Any] | None:
    """把默认或注入视觉客户端输出统一收敛到严格动作 schema。"""
    value = _answer_mapping(answer)
    if value is None:
        return None
    challenge_type = str(
        value.get("challenge_type") or value.get("type") or value.get("task_type") or "unknown"
    ).lower()
    confidence = value.get("confidence")
    if challenge_type not in {"grid", "point", "drag", "unknown"}:
        return {"challenge_type": challenge_type, "confidence": confidence}

    canonical: dict[str, Any] = {
        "challenge_type": challenge_type,
        "confidence": confidence,
        "tile_indices": [],
        "points": [],
        "drags": [],
    }
    raw_tiles = value.get("tile_indices", value.get("indices", value.get("tiles", [])))
    raw_points = value.get("points", value.get("point", []))
    raw_drags = value.get("drags", value.get("action", []))
    contradictions = {
        "grid": bool(raw_points) or bool(raw_drags),
        "point": bool(raw_tiles) or bool(raw_drags),
        "drag": bool(raw_tiles) or bool(raw_points),
        "unknown": bool(raw_tiles) or bool(raw_points) or bool(raw_drags) or bool(value.get("actions")),
    }
    if contradictions[challenge_type]:
        return {
            "challenge_type": challenge_type,
            "type": challenge_type,
            "confidence": confidence,
            "_invalid": True,
        }
    if challenge_type == "grid":
        canonical["tile_indices"] = value.get(
            "tile_indices", value.get("actions", value.get("indices", value.get("tiles", [])))
        )
    elif challenge_type == "point":
        points = value.get("points", value.get("actions", value.get("point", [])))
        canonical["points"] = [points] if isinstance(points, Mapping) else points
    elif challenge_type == "drag":
        drags = value.get("drags")
        if drags is None:
            action = value.get("action")
            if action is None and ("start" in value or "from" in value):
                action = value
            drags = [action] if isinstance(action, Mapping) else action or []
        normalized_drags: list[dict[str, Any]] = []
        if isinstance(drags, Sequence) and not isinstance(drags, (str, bytes)):
            for drag in drags:
                if not isinstance(drag, Mapping):
                    canonical["drags"] = drags
                    break
                start = _point(drag.get("start", drag.get("from")))
                end = _point(drag.get("end", drag.get("to")))
                if start is None or end is None:
                    canonical["drags"] = drags
                    break
                normalized_drags.append(
                    {
                        "start": {"x": start[0], "y": start[1]},
                        "end": {"x": end[0], "y": end[1]},
                    }
                )
            else:
                canonical["drags"] = normalized_drags
        else:
            canonical["drags"] = drags

    try:
        from browser.openai_vision import VisionPlan

        return VisionPlan.from_mapping(canonical, max_actions=max_actions).model_dump()
    except Exception:
        return {
            "challenge_type": challenge_type,
            "type": challenge_type,
            "confidence": confidence,
            "_invalid": True,
        }


def _safe_exception_detail(exc: BaseException, *, stage: str) -> dict[str, Any]:
    detail: dict[str, Any] = {"failure_stage": stage, "error_type": type(exc).__name__}
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        detail["http_status"] = status
    return detail


class HCaptchaSolver:
    """单个 Page 的 hCaptcha 求解会话。构造时即开始监听响应。"""

    def __init__(
        self,
        page: Any,
        *,
        options: HCaptchaOptions | Mapping[str, Any] | None = None,
        vision_client: Any = None,
        log: Callable[[str], Any] | None = None,
        screenshot: Callable[[str], Any] | None = None,
    ) -> None:
        self.page = page
        self.options = HCaptchaOptions.from_value(options).normalized()
        self.vision_client = vision_client
        self.log = log
        self.failure_screenshot = screenshot
        self._response_tasks: set[asyncio.Task[Any]] = set()
        self._network_task_type = ""
        self._network_passed = False
        self._network_token = ""
        self._network_failed = False
        self._vision_failure: dict[str, Any] = {}
        self._diagnostic_target: Any = None
        self._closed = False
        self._listener = self._on_response
        try:
            page.on("response", self._listener)
        except Exception:
            pass

    async def __aenter__(self) -> HCaptchaSolver:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    def _safe_log(self, message: str) -> None:
        if callable(self.log):
            try:
                self.log(message)
            except Exception:
                pass

    def _on_response(self, response: Any) -> None:
        if self._closed:
            return
        try:
            url = str(getattr(response, "url", "") or "")
            if not _is_hcaptcha_url(url):
                return
            lowered_url = url.lower()
            if "/getcaptcha" not in lowered_url and "/checkcaptcha" not in lowered_url:
                return
            task = asyncio.create_task(self._inspect_response(response))
        except (RuntimeError, TypeError):
            return
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _inspect_response(self, response: Any) -> None:
        try:
            payload = await _maybe_await(response.json())
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        passed = payload.get("pass")
        if passed is True:
            self._network_passed = True
            for key in ("generated_pass_UUID", "generated_pass_uuid", "token"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    self._network_token = value.strip()
                    break
        elif passed is False and "/checkcaptcha" in str(getattr(response, "url", "") or "").lower():
            self._network_failed = True
        question = payload.get("requester_question")
        question = question if isinstance(question, Mapping) else {}
        raw = str(question.get("example", ""))
        shape = str(payload.get("request_type") or question.get("request_type") or raw)
        lowered = shape.lower()
        if "drag" in lowered:
            self._network_task_type = "drag"
        elif "area" in lowered or "point" in lowered:
            self._network_task_type = "point"
        elif "binary" in lowered or "grid" in lowered:
            self._network_task_type = "grid"

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for method_name in ("remove_listener", "off"):
            try:
                method = getattr(self.page, method_name)
                method("response", self._listener)
                break
            except Exception:
                continue
        tasks = tuple(self._response_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._response_tasks.clear()

    async def _read_response(self) -> tuple[bool, str]:
        try:
            value = await _maybe_await(self.page.evaluate(_TOKEN_JS))
            if isinstance(value, Mapping):
                return bool(value.get("present")), str(value.get("token") or "").strip()
            if isinstance(value, str):
                return bool(value), value.strip()
        except Exception:
            pass
        try:
            locator = self.page.locator(
                'textarea[name="h-captcha-response"], input[name="h-captcha-response"]'
            )
            present = await _count(locator) > 0
            if not present:
                return False, ""
            first = locator.first
            for method_name in ("input_value", "get_attribute"):
                try:
                    method = getattr(first, method_name)
                    value = await _maybe_await(method() if method_name == "input_value" else method("value"))
                    if value:
                        return True, str(value).strip()
                except Exception:
                    continue
            return True, ""
        except Exception:
            return False, ""

    def _all_frames(self) -> list[Any]:
        root = getattr(self.page, "main_frame", None)
        if callable(root):
            root = root()
        frames = _walk_frames(root, self.options.frame_depth) if root is not None else []
        if not frames:
            try:
                frames = list(self.page.frames)
            except Exception:
                frames = []
        return frames

    async def _wait_for_live_challenge(self, frame: Any) -> Any | None:
        """点击复选框后等待挑战真正呈现题面，返回可求解的 frame。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.post_action_wait_ms / 1000
        candidate = frame
        while True:
            _checkbox, current = await self._find_frames()
            if current is not None:
                candidate = current
            if await self._challenge_is_live(candidate):
                return candidate
            if loop.time() >= deadline:
                return candidate if await self._challenge_is_live(candidate) else None
            await asyncio.sleep(self.options.poll_interval_ms / 1000)

    async def _challenge_is_live(self, frame: Any) -> bool:
        """挑战 iframe 是否真的呈现了题目。

        hCaptcha 会在点击复选框之前就把 challenge iframe 挂到 DOM 上，此时它没有
        prompt 也没有 task-image，截图是纯黑图。把这种「空壳」当成已加载的挑战会
        让求解器跳过复选框点击，并把黑图发给模型（已实测：prompt 空、tiles=0、
        图像仅 1179 字节纯黑）。因此必须以题面或图块存在为准。
        """
        if frame is None:
            return False
        if await _first_locator(frame, _PROMPT_SELECTORS) is not None:
            return bool(await _text(await _first_locator(frame, _PROMPT_SELECTORS)))
        return await _locator_collection(frame, _GRID_SELECTORS) is not None

    async def _find_frames(self) -> tuple[Any | None, Any | None]:
        checkbox_frame = None
        challenge_frame = None
        for frame in self._all_frames():
            raw_url = _frame_url(frame)
            if not _is_hcaptcha_url(raw_url):
                continue
            url = raw_url.lower()
            frame_is_checkbox = "frame=checkbox" in url or "checkbox" in url
            frame_is_challenge = "frame=challenge" in url or "challenge" in url
            if checkbox_frame is None and await _first_locator(frame, _CHECKBOX_SELECTORS) is not None:
                checkbox_frame = frame
                frame_is_checkbox = True
            if challenge_frame is None and not frame_is_checkbox:
                prompt = await _first_locator(frame, _PROMPT_SELECTORS)
                grid = await _locator_collection(frame, _GRID_SELECTORS)
                marker = await _first_locator(frame, _CHALLENGE_MARKER_SELECTORS)
                if frame_is_challenge or prompt is not None or grid is not None or marker is not None:
                    challenge_frame = frame
        return checkbox_frame, challenge_frame

    async def _locate(self) -> tuple[bool, str, Any | None, Any | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.presence_timeout_ms / 1000
        response_field_present = False
        while True:
            present, token = await self._read_response()
            response_field_present = response_field_present or present
            checkbox_frame, challenge_frame = await self._find_frames()
            if token or checkbox_frame is not None or challenge_frame is not None:
                return response_field_present, token, checkbox_frame, challenge_frame
            if self._network_token:
                return response_field_present, self._network_token, None, None
            if loop.time() >= deadline:
                return response_field_present, "", None, None
            await asyncio.sleep(self.options.poll_interval_ms / 1000)

    async def _mouse_click_locator(self, locator: Any) -> bool:
        box = await _box(locator)
        if box is None:
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        try:
            await _maybe_await(self.page.mouse.move(x - 24, y - 8, steps=5))
            await _maybe_await(self.page.mouse.move(x, y, steps=7))
            await _maybe_await(self.page.mouse.click(x, y))
            return True
        except Exception:
            return False

    async def _click_checkbox(self, frame: Any) -> bool:
        locator = await _first_locator(frame, _CHECKBOX_SELECTORS)
        return locator is not None and await self._mouse_click_locator(locator)

    async def _wait_for_progress(self) -> tuple[str, Any | None, bool]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.post_action_wait_ms / 1000
        last_challenge = None
        while True:
            _present, token = await self._read_response()
            resolved_token = token or self._network_token
            if resolved_token:
                return resolved_token, None, True
            if self._network_failed:
                self._network_failed = False
                _checkbox, current_challenge = await self._find_frames()
                return "", current_challenge or last_challenge, False
            _checkbox, current_challenge = await self._find_frames()
            if current_challenge is not None:
                last_challenge = current_challenge
            if loop.time() >= deadline:
                return "", last_challenge, False
            await asyncio.sleep(self.options.poll_interval_ms / 1000)

    async def _capture_round(self, frame: Any, round_number: int) -> dict[str, Any]:
        prompt = await _text(await _first_locator(frame, _PROMPT_SELECTORS))
        grid_locator = await _locator_collection(frame, _GRID_SELECTORS)
        tiles: list[dict[str, Any]] = []
        if grid_locator is not None:
            count = await _count(grid_locator)
            for index in range(count):
                tile = grid_locator.nth(index)
                try:
                    image = await _maybe_await(tile.screenshot(type="png"))
                except Exception:
                    image = b""
                tiles.append({"index": index + 1, "image": image})

        hint = self._network_task_type or ("grid" if tiles else "unknown")
        challenge = await _first_locator(frame, _SCREENSHOT_SELECTORS)
        action_target = challenge
        if hint in {"point", "drag"}:
            interaction = await _first_locator(frame, _INTERACTION_SELECTORS)
            if interaction is not None:
                action_target = interaction
        image = b""
        labels_added = False
        if tiles:
            try:
                labels_added = bool(await _maybe_await(frame.evaluate(_ADD_GRID_LABELS_JS, list(_GRID_SELECTORS))))
            except Exception:
                labels_added = False
        try:
            if action_target is not None:
                image = await _screenshot_locator(self.page, action_target)
            if not image:
                body = await _first_locator(frame, ("body",))
                if body is not None:
                    image = await _screenshot_locator(self.page, body)
                    action_target = body if image else None
        finally:
            if labels_added:
                try:
                    await _maybe_await(frame.evaluate(_REMOVE_GRID_LABELS_JS))
                except Exception:
                    pass

        self._diagnostic_target = challenge or action_target
        return {
            "round": round_number,
            "prompt": prompt,
            "task_type": hint,
            "image": image,
            "tiles": tiles,
            "action_target": action_target,
        }

    async def _default_vision_client(self) -> Any | None:
        if self.vision_client is not None:
            return self.vision_client
        try:
            from browser.openai_vision import OpenAIVisionClient

            # 由 VisionClientConfig 统一解析单个 JSON Secret 及兼容环境变量。
            # 不在这里预判独立 API key，否则只配置 HCAPTCHA_VISION_CONFIG 时会被
            # 错误地当成「视觉客户端未配置」。
            client = OpenAIVisionClient(
                options={
                    "model": self.options.model,
                    "base_url": self.options.base_url,
                    # 为浏览器动作和结果确认预留时间；阻塞 HTTP 必须先于单轮预算结束。
                    "timeout_ms": max(100, self.options.round_timeout_ms - 1_000),
                    "max_actions": 16,
                }
            )
        except Exception:
            return None
        self.vision_client = client
        return client

    async def _ask_vision(self, request: Mapping[str, Any]) -> Mapping[str, Any] | None:
        client = await self._default_vision_client()
        if client is None:
            self._vision_failure = {"failure_stage": "client_init", "error_type": "VisionClientError"}
            return None
        method = None
        for name in ("solve_hcaptcha", "solve", "analyze"):
            candidate = getattr(client, name, None)
            if callable(candidate):
                method = candidate
                break
        if method is None and callable(client):
            method = client
        if method is None:
            self._vision_failure = {"failure_stage": "client_method", "error_type": "VisionClientError"}
            return None
        try:
            signature = inspect.signature(method)
            signature.bind(**request)
        except (TypeError, ValueError):
            self._vision_failure = {"failure_stage": "client_signature", "error_type": "TypeError"}
            return None
        try:
            answer = await _maybe_await(method(**request))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 传输/服务错误必须转成结构化失败，且不泄露密钥或原始响应正文。
            self._vision_failure = _safe_exception_detail(exc, stage="vision_request")
            return None
        return _validated_answer(answer)

    async def _apply_grid(self, frame: Any, answer: Mapping[str, Any], tile_count: int) -> bool:
        raw = answer.get("actions", answer.get("indices", answer.get("tiles", [])))
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            return False
        indices: list[int] = []
        for value in raw:
            try:
                index = int(value)
            except (TypeError, ValueError):
                return False
            if index < 1 or index > tile_count or index in indices:
                return False
            indices.append(index)
        locator = await _locator_collection(frame, _GRID_SELECTORS)
        if locator is None:
            return False
        for index in indices:
            if not await self._mouse_click_locator(locator.nth(index - 1)):
                return False
        submit = await _first_locator(frame, _SUBMIT_SELECTORS)
        return submit is not None and await self._mouse_click_locator(submit)

    async def _apply_point(
        self, frame: Any, answer: Mapping[str, Any], target: Any = None
    ) -> bool:
        raw = answer.get("actions", answer.get("points", answer.get("point")))
        if isinstance(raw, Mapping):
            raw = [raw]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            return False
        points = [_point(value) for value in raw]
        if any(point is None for point in points):
            return False
        if target is None:
            target = await _first_locator(frame, _INTERACTION_SELECTORS)
        box = await _box(target) if target is not None else None
        if box is None:
            return False
        for point in points:
            assert point is not None
            x = box["x"] + box["width"] * point[0] / 1000
            y = box["y"] + box["height"] * point[1] / 1000
            await _maybe_await(self.page.mouse.move(x, y, steps=7))
            await _maybe_await(self.page.mouse.click(x, y))
        submit = await _first_locator(frame, _SUBMIT_SELECTORS)
        if submit is not None:
            await self._mouse_click_locator(submit)
        return True

    async def _apply_drag(
        self, frame: Any, answer: Mapping[str, Any], target: Any = None
    ) -> bool:
        raw = answer.get("drags", answer.get("action", answer))
        if isinstance(raw, Mapping):
            raw = [raw]
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
            return False
        parsed: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for item in raw:
            if not isinstance(item, Mapping):
                return False
            start = _point(item.get("start", item.get("from")))
            end = _point(item.get("end", item.get("to")))
            if start is None or end is None:
                return False
            parsed.append((start, end))
        if target is None:
            target = await _first_locator(frame, _INTERACTION_SELECTORS)
        box = await _box(target) if target is not None else None
        if box is None:
            return False
        for start, end in parsed:
            sx = box["x"] + box["width"] * start[0] / 1000
            sy = box["y"] + box["height"] * start[1] / 1000
            ex = box["x"] + box["width"] * end[0] / 1000
            ey = box["y"] + box["height"] * end[1] / 1000
            await _maybe_await(self.page.mouse.move(sx, sy, steps=5))
            await _maybe_await(self.page.mouse.down())
            try:
                await _maybe_await(self.page.mouse.move(ex, ey, steps=15))
            finally:
                await _maybe_await(self.page.mouse.up())
        return True

    async def _apply_answer(
        self,
        frame: Any,
        answer: Mapping[str, Any],
        tile_count: int,
        action_target: Any = None,
    ) -> str:
        confidence = _numeric(answer.get("confidence"))
        if confidence is None or confidence < self.options.confidence_threshold:
            return "uncertain"
        task_type = str(
            answer.get("challenge_type")
            or answer.get("type")
            or answer.get("task_type")
            or self._network_task_type
        ).lower()
        if task_type == "grid":
            return "applied" if await self._apply_grid(frame, answer, tile_count) else "uncertain"
        if task_type == "point":
            return (
                "applied"
                if await self._apply_point(frame, answer, target=action_target)
                else "uncertain"
            )
        if task_type == "drag":
            return (
                "applied"
                if await self._apply_drag(frame, answer, target=action_target)
                else "uncertain"
            )
        return "unsupported"

    async def _save_failure(self, status: HCaptchaStatus) -> str:
        if not callable(self.failure_screenshot) or self._diagnostic_target is None:
            return ""
        name = f"hcaptcha-{status}.png"
        try:
            signature = inspect.signature(self.failure_screenshot)
            signature.bind(name, target=self._diagnostic_target)
            return str(
                await _maybe_await(
                    self.failure_screenshot(name, target=self._diagnostic_target)
                )
                or ""
            )
        except Exception:
            # 无法保证只截挑战区域时宁可不落盘，避免保存整个已登录页面。
            return ""

    async def _result(
        self,
        status: HCaptchaStatus,
        message: str,
        *,
        token: str = "",
        rounds: int = 0,
        challenge_type: str = "",
        failure_stage: str = "",
        error_type: str = "",
        http_status: int | None = None,
    ) -> HCaptchaSolveResult:
        screenshot = ""
        if status not in ("success", "not_present"):
            screenshot = await self._save_failure(status)
        self._safe_log(message)
        return HCaptchaSolveResult(
            status,
            message,
            token=token,
            rounds=rounds,
            challenge_type=challenge_type,
            screenshot=screenshot,
            failure_stage=failure_stage,
            error_type=error_type,
            http_status=http_status,
        )

    async def solve(self, *, trigger: Callable[[], Any] | None = None) -> HCaptchaSolveResult:
        if not self.options.enabled:
            return await self._result("not_configured", "hCaptcha 求解器未启用")
        try:
            async with asyncio.timeout(self.options.total_timeout_ms / 1000):
                if trigger is not None:
                    await _maybe_await(trigger())
                response_field_present, token, checkbox_frame, challenge_frame = await self._locate()
                if token:
                    return await self._result("success", "hCaptcha 已由页面完成", token=token)
                if checkbox_frame is None and challenge_frame is None:
                    if response_field_present or self._network_passed:
                        return await self._result("timeout", "检测到 hCaptcha，但未等到验证令牌")
                    return await self._result("not_present", "页面中未发现 hCaptcha")

                # 预挂载但没有题面的 challenge iframe 不算已加载的挑战；仍需先点复选框。
                if checkbox_frame is not None and not await self._challenge_is_live(challenge_frame):
                    if not await self._click_checkbox(checkbox_frame):
                        return await self._result("failed", "无法点击 hCaptcha 复选框")
                    self._safe_log("已点击 hCaptcha 复选框，等待自动通过或图片挑战")
                    token, progressed_frame, passed = await self._wait_for_progress()
                    if token or passed:
                        return await self._result("success", "hCaptcha 已自动通过", token=token)
                    challenge_frame = await self._wait_for_live_challenge(
                        progressed_frame or challenge_frame
                    )

                if not await self._challenge_is_live(challenge_frame):
                    return await self._result("failed", "hCaptcha 挑战未加载")
                if await self._default_vision_client() is None:
                    return await self._result("not_configured", "hCaptcha 视觉客户端未配置")

                for round_number in range(1, self.options.max_rounds + 1):
                    try:
                        async with asyncio.timeout(self.options.round_timeout_ms / 1000):
                            request = await self._capture_round(challenge_frame, round_number)
                            action_target = request.pop("action_target", None)
                            self._vision_failure = {}
                            answer = await self._ask_vision(request)
                            if answer is None:
                                failure = dict(self._vision_failure)
                                http_status = failure.get("http_status")
                                message = "hCaptcha 视觉客户端未返回结构化结果"
                                if isinstance(http_status, int):
                                    message = f"hCaptcha 视觉服务请求失败（HTTP {http_status}）"
                                return await self._result(
                                    "failed",
                                    message,
                                    rounds=round_number,
                                    failure_stage=str(failure.get("failure_stage") or "vision_request"),
                                    error_type=str(failure.get("error_type") or ""),
                                    http_status=http_status if isinstance(http_status, int) else None,
                                )
                            challenge_type = str(
                                answer.get("challenge_type")
                                or answer.get("type")
                                or answer.get("task_type")
                                or request.get("task_type")
                                or "unknown"
                            ).lower()
                            self._safe_log(
                                f"hCaptcha 第 {round_number}/{self.options.max_rounds} 轮："
                                f"模型判定 {challenge_type}，开始执行受约束动作"
                            )
                            applied = await self._apply_answer(
                                challenge_frame,
                                answer,
                                len(request["tiles"]),
                                action_target=action_target,
                            )
                            if applied == "unsupported":
                                return await self._result(
                                    "unsupported",
                                    "hCaptcha 挑战类型不受支持",
                                    rounds=round_number,
                                    challenge_type=challenge_type,
                                )
                            if applied == "uncertain":
                                return await self._result(
                                    "uncertain",
                                    "hCaptcha 视觉结果不确定，未执行点击",
                                    rounds=round_number,
                                    challenge_type=challenge_type,
                                )
                            token, next_frame, passed = await self._wait_for_progress()
                            if token or passed:
                                return await self._result(
                                    "success",
                                    "hCaptcha 验证成功",
                                    token=token,
                                    rounds=round_number,
                                    challenge_type=challenge_type,
                                )
                            if next_frame is not None:
                                challenge_frame = next_frame
                    except TimeoutError:
                        return await self._result(
                            "timeout", "hCaptcha 单轮求解超时", rounds=round_number
                        )
                return await self._result(
                    "failed", "hCaptcha 已达到最大求解轮数", rounds=self.options.max_rounds
                )
        except TimeoutError:
            return await self._result("timeout", "hCaptcha 总体求解超时")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure = _safe_exception_detail(exc, stage="solver_exception")
            return await self._result(
                "failed",
                "hCaptcha 求解失败",
                failure_stage=str(failure.get("failure_stage") or "solver_exception"),
                error_type=str(failure.get("error_type") or ""),
                http_status=failure.get("http_status") if isinstance(failure.get("http_status"), int) else None,
            )


async def solve(
    page: Any,
    *,
    trigger: Callable[[], Any] | None = None,
    options: HCaptchaOptions | Mapping[str, Any] | None = None,
    vision_client: Any = None,
    log: Callable[[str], Any] | None = None,
    screenshot: Callable[[str], Any] | None = None,
) -> HCaptchaSolveResult:
    """便捷入口：创建求解器、执行并确保移除监听器。"""

    solver = HCaptchaSolver(
        page,
        options=options,
        vision_client=vision_client,
        log=log,
        screenshot=screenshot,
    )
    try:
        return await solver.solve(trigger=trigger)
    finally:
        await solver.aclose()


__all__ = ["HCaptchaOptions", "HCaptchaSolveResult", "HCaptchaSolver", "solve"]
