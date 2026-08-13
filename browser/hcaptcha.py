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
import io
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
_WIDGET_PRESENT_JS = """() => {
    for (const frame of document.querySelectorAll('iframe')) {
        if (/hcaptcha/i.test(String(frame.src || ''))) return true;
    }
    if (document.querySelector('[data-sitekey]')) return true;
    if (document.querySelector('.h-captcha, [class*="h-captcha"], #h-captcha')) return true;
    return false;
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
    # 为 point/drag 挑战附带一张叠加了 0-1000 坐标网格的辅助图，让模型照刻度读数
    # 而不是凭空估算坐标。纯本地 Pillow 绘制，失败会自动退回只发原图。
    coordinate_grid: bool = True
    # 上传前把截图压成 JPEG。全尺寸 PNG（主图+网格约 690KB）实测会让视觉端点在 60s
    # 超时失败；压缩后 23s 返回且置信度更高。
    compress_uploads: bool = True
    # widget 元素已在页面上、但 iframe 内部控件尚未可查询时的额外等待预算。
    # 实测 ABR 福利站从 domcontentloaded 到 checkbox frame 可定位需 10s 以上，
    # 只用 presence_timeout_ms 会在挂载完成前就判定「未发现 hCaptcha」。
    widget_mount_timeout_ms: int = 25_000
    post_action_wait_ms: int = 5_000
    poll_interval_ms: int = 200
    # 单次拟人化 mouse.move 的上限。Camoufox humanize=True 下实测一次移动可达
    # 17s，两段就是 29.5s；不设上限会让点击开销吃光整轮预算。
    move_timeout_ms: int = 3_000
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
            coordinate_grid=bool(self.coordinate_grid),
            compress_uploads=bool(self.compress_uploads),
            widget_mount_timeout_ms=max(0, int(self.widget_mount_timeout_ms)),
            post_action_wait_ms=max(0, int(self.post_action_wait_ms)),
            poll_interval_ms=max(10, int(self.poll_interval_ms)),
            move_timeout_ms=max(100, int(self.move_timeout_ms)),
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


async def _as_coro(value: Any) -> Any:
    """把「可能是同步返回值」的调用结果包装成可被 wait_for 约束的协程。"""
    return await value if inspect.isawaitable(value) else value


# 单次 DOM 探测的硬上限（秒）。detached 的 hCaptcha iframe 上，Playwright 的
# locator 调用会等满自身默认超时（30s）才抛错；hCaptcha 页面常同时挂着 6 个以上
# 这样的框架，逐个探测足以吃掉整个求解预算，表现为「总体求解超时、rounds=0」，
# 掩盖真正的失败原因。预算只在轮询之间检查，无法打断单次阻塞调用，因此每次探测
# 必须自带超时。
_PROBE_TIMEOUT_S = 2.0


async def _probe(value: Any, fallback: Any) -> Any:
    """执行一次可能阻塞的 DOM 探测；超时或异常都返回 fallback。"""
    try:
        if not inspect.isawaitable(value):
            return value
        return await asyncio.wait_for(asyncio.ensure_future(value), timeout=_PROBE_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError):
        return fallback
    except asyncio.CancelledError:
        raise
    except Exception:
        return fallback


async def _count(locator: Any) -> int:
    try:
        value = await _probe(locator.count(), 0)
        return int(value)
    except Exception:
        return 0


async def _is_visible(locator: Any) -> bool:
    method = getattr(locator, "is_visible", None)
    if not callable(method):
        return True
    try:
        return bool(await _probe(method(), False))
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
            return str(await _probe(method(), "") or "").strip()
        except Exception:
            continue
    return ""


async def _box(locator: Any) -> dict[str, float] | None:
    try:
        raw = await _probe(locator.bounding_box(), None)
        if not isinstance(raw, Mapping):
            return None
        box = {key: float(raw[key]) for key in ("x", "y", "width", "height")}
        if box["width"] <= 0 or box["height"] <= 0:
            return None
        return box
    except Exception:
        return None


def compress_for_vision(image: bytes, *, max_edge: int = 640, quality: int = 82) -> tuple[bytes, str]:
    """把挑战截图压到适合模型上传的体积，返回 (字节, media_type)。

    挑战图是照片类内容，PNG 无损编码极其浪费：实测 625×587 的 PNG 有 375KB，
    等尺寸 JPEG 只需十几 KB。主图 + 网格图两张全尺寸 PNG 合计约 690KB，base64
    后接近 1MB，实测视觉端点必然在 60s 超时失败；压缩后同一张图 23s 返回且置信度
    更高（0.96 对 0.90）。

    压缩失败时返回原图与 image/png，绝不因为优化步骤失败而丢掉整轮求解。
    """
    if not image:
        return b"", "image/png"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
        width, height = canvas.size
        longest = max(width, height)
        if longest > max_edge:
            scale = max_edge / longest
            canvas = canvas.resize((max(1, round(width * scale)), max(1, round(height * scale))), Image.LANCZOS)
        buffer = io.BytesIO()
        canvas.save(buffer, format="JPEG", quality=int(quality), optimize=True)
        encoded = buffer.getvalue()
        # 压缩没有变小就没有意义（极小图可能反而变大）。
        if encoded and len(encoded) < len(image):
            return encoded, "image/jpeg"
        return image, "image/png"
    except Exception:
        return image, "image/png"


def coordinate_grid_overlay(image: bytes, *, divisions: int = 10) -> bytes:
    """在挑战图上叠加 0-1000 坐标网格与刻度，返回 PNG 字节；失败返回空。

    point/drag 挑战要求模型给出坐标，但纯截图里没有任何刻度参照，模型只能凭空
    估算归一化坐标，误差直接导致点击落空。叠加与协议同尺度（0-1000）的网格后，
    模型可以照着可见刻度读数，把"估算"变成"读数"。

    思路参考 QIN2DIM/hcaptcha-challenger 的 create_coordinate_grid（该项目用
    matplotlib + opencv 生成科学坐标系图）。这里只用已有依赖 Pillow 重写，
    不引入 numpy/opencv/matplotlib，也不复制其代码。
    """
    if not image:
        return b""
    try:
        from PIL import Image, ImageDraw

        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
        width, height = canvas.size
        if width < 32 or height < 32:
            return b""
        draw = ImageDraw.Draw(canvas, "RGBA")
        step = max(2, int(divisions))
        # 依据整体亮度选网格色，避免在亮图上画白线、暗图上画黑线而看不见。
        sample = canvas.resize((16, 16)).convert("L")
        histogram = sample.histogram()
        total = sum(histogram) or 1
        brightness = sum(level * count for level, count in enumerate(histogram)) / total
        line = (0, 0, 0, 150) if brightness > 127 else (255, 255, 255, 170)
        halo = (255, 255, 255, 190) if brightness > 127 else (0, 0, 0, 190)
        for i in range(1, step):
            ratio = i / step
            x = round(width * ratio)
            y = round(height * ratio)
            draw.line([(x, 0), (x, height)], fill=line, width=1)
            draw.line([(0, y), (width, y)], fill=line, width=1)
            label = str(round(1000 * ratio))
            # 先描边再写字，保证刻度在任何底色上都可读。
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                draw.text((x + 2 + dx, 2 + dy), label, fill=halo)
                draw.text((2 + dx, y + 2 + dy), label, fill=halo)
            draw.text((x + 2, 2), label, fill=line)
            draw.text((2, y + 2), label, fill=line)
        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        # 辅助图失败不能影响主流程，退回只发原图。
        return b""


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
        # 模型常把坐标再包一层，如 {"point": [850, 390]} 或 {"coordinates": {...}}。
        # 实测 mimo-v2.5 返回 {"start": {"point": [850, 390]}}，不解包会把一个
        # 完全正确的答案判为无效动作而丢弃。
        for key in ("point", "coordinates", "coordinate", "position", "pos"):
            if key in value:
                nested = _point(value.get(key))
                if nested is not None:
                    return nested
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
    # elements/paths 是模型实测用过的等价键名（mimo-v2.5 返回 elements）。
    raw_drags = value.get("drags", value.get("action", value.get("elements", value.get("paths", []))))
    # 与 VisionPlan.from_mapping 一致：unknown + 恰好一种动作时按动作反推类型，
    # 不再当成矛盾。模型经常类型填 unknown 但动作完全正确（实测 confidence=1）。
    if challenge_type == "unknown":
        present = [
            name
            for name, values in (("grid", raw_tiles), ("point", raw_points), ("drag", raw_drags))
            # 与下面的矛盾判定同一标准：非 list 的垃圾字段不算动作，否则会被
            # 误判成「多种动作并存」而放弃推断。
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)) and values
        ]
        if len(present) == 1 and not value.get("actions"):
            challenge_type = present[0]
            canonical["challenge_type"] = challenge_type
    # 只有「结构上可用的动作」才构成矛盾。实测模型在 drag 计划里附带了垃圾
    # tile_indices（{"source": [...], "target": [...]}），它既不是可用动作也与
    # drag 无关，却让整份正确答案被判为矛盾而丢弃。非 list 一律不算动作。
    def _usable(raw: Any) -> bool:
        return isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and bool(raw)

    has_tiles, has_points, has_drags = _usable(raw_tiles), _usable(raw_points), _usable(raw_drags)
    contradictions = {
        "grid": has_points or has_drags,
        "point": has_tiles or has_drags,
        "drag": has_tiles or has_points,
        "unknown": has_tiles or has_points or has_drags or bool(value.get("actions")),
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
            if action is None:
                # elements/paths 是实测出现过的等价键名，需与上面的 raw_drags 一致。
                action = value.get("elements", value.get("paths"))
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


def _mask(text: str) -> str:
    """按仓库统一规则脱敏后再输出。

    模型返回与服务端错误正文对排障很关键，所以整体保留；但它们可能夹带
    Authorization、api_key 等字段，必须先过 mask_secrets。脱敏器不可用时
    宁可不输出，也不冒泄露风险。
    """
    value = str(text or "")
    if not value:
        return ""
    try:
        from mask_utils import mask_secrets

        return mask_secrets(value)
    except Exception:
        return "<脱敏器不可用，已省略>"


def _render_answer(answer: Any) -> str:
    """把模型返回渲染为可读文本，尽量保留完整内容。"""
    value = _answer_mapping(answer)
    if value is None:
        return repr(answer)
    try:
        import json as _json

        return _json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return repr(value)


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
            # detached 框架上的任何 locator 调用都会阻塞到超时才失败；hCaptcha 常留下
            # 多个这样的残留框架，逐个探测会吃光求解预算。先廉价地跳过它们。
            is_detached = getattr(frame, "is_detached", None)
            if callable(is_detached):
                try:
                    if bool(is_detached()):
                        continue
                except Exception:
                    pass
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

    async def _widget_mounted(self) -> bool:
        """页面上是否已有 hCaptcha widget（iframe 元素或 data-sitekey 容器）。

        iframe 元素挂上 DOM 与「iframe 内部控件可被 Playwright 定位」之间有可观
        延迟（实测 ABR 福利站超过 10s）。只要 widget 已存在就说明验证确实存在，
        不能按「页面中未发现 hCaptcha」结束，否则会在挂载完成前提前放弃。
        """
        try:
            return bool(await _maybe_await(self.page.evaluate(_WIDGET_PRESENT_JS)))
        except Exception:
            return False

    async def _locate(self) -> tuple[bool, str, Any | None, Any | None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.presence_timeout_ms / 1000
        # widget 已挂载时把等待预算放宽到 widget_mount_timeout_ms，覆盖 iframe 内部
        # 控件的渲染延迟；两个预算取较晚者，保证不会缩短原有等待。
        mount_deadline = loop.time() + max(
            self.options.presence_timeout_ms, self.options.widget_mount_timeout_ms
        ) / 1000
        response_field_present = False
        logged_wait = False
        while True:
            present, token = await self._read_response()
            response_field_present = response_field_present or present
            checkbox_frame, challenge_frame = await self._find_frames()
            if token:
                return response_field_present, token, checkbox_frame, challenge_frame
            # 可直接求解的挑战（已有题面）随时可以返回。
            if await self._challenge_is_live(challenge_frame):
                return response_field_present, token, checkbox_frame, challenge_frame
            # 否则必须等到 checkbox 框架也可查询：hCaptcha 的空壳 challenge iframe
            # 常比 checkbox iframe 先可定位，一旦此时就返回，checkbox 为 None 会让
            # solve() 跳过复选框点击，直接判「挑战未加载」（实测该分支使整轮失败）。
            if checkbox_frame is not None:
                return response_field_present, token, checkbox_frame, challenge_frame
            if self._network_token:
                return response_field_present, self._network_token, None, None
            widget_mounted = response_field_present or await self._widget_mounted()
            effective_deadline = mount_deadline if widget_mounted else deadline
            if widget_mounted and not logged_wait:
                logged_wait = True
                self._safe_log("hCaptcha 已出现但控件尚未就绪，继续等待挂载完成")
            if loop.time() >= effective_deadline:
                return response_field_present, "", None, None
            await asyncio.sleep(self.options.poll_interval_ms / 1000)

    async def _mouse_click_locator(self, locator: Any, label: str = "元素") -> bool:
        box = await _box(locator)
        if box is None:
            self._safe_log(f"点击{label}失败：元素无可用坐标（不可见或已分离）")
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        try:
            # approach + settle 两段移动用于反检测（轨迹不能是瞬移直线）。但在
            # Camoufox humanize=True 下，单次 mouse.move 会做拟人化缓动：实测这
            # 两段耗时 17.7s + 11.8s = 29.5s，而 humanize=False 时只需 0.2s
            # （100 倍差距）。求解器每次点击都要付这笔钱，仅复选框 + 图块 + 提交
            # 就能在任何模型调用之前吃光总预算，表现为「总体求解超时、零次调用」。
            #
            # 因此给移动本身加超时：超时即放弃缓动、直接落点点击。轨迹仍非瞬移
            # （已经历部分缓动），但不会再让预算失控。
            await self._humanized_move(x - 24, y - 8, steps=5)
            await self._humanized_move(x, y, steps=7)
            await _maybe_await(self.page.mouse.click(x, y))
            self._safe_log(f"已点击{label}：({x:.0f}, {y:.0f})")
            return True
        except Exception as exc:
            self._safe_log(f"点击{label}异常：{type(exc).__name__}")
            return False

    async def _humanized_move(self, x: float, y: float, *, steps: int) -> None:
        """执行一次拟人化移动，超过 move_timeout_ms 即放弃缓动。

        放弃缓动不等于失败：后续 click 用绝对坐标，落点不受影响。
        """
        budget = self.options.move_timeout_ms / 1000
        try:
            await asyncio.wait_for(
                asyncio.ensure_future(_as_coro(self.page.mouse.move(x, y, steps=steps))),
                timeout=budget,
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._safe_log(f"拟人化移动超过 {budget:.1f}s，改用直接落点")
        except asyncio.CancelledError:
            raise
        except Exception:
            # 移动失败不致命，交给后续 click 用绝对坐标兜底。
            pass

    async def _click_checkbox(self, frame: Any) -> bool:
        locator = await _first_locator(frame, _CHECKBOX_SELECTORS)
        if locator is None:
            self._safe_log("未找到 hCaptcha 复选框元素")
            return False
        return await self._mouse_click_locator(locator, label="hCaptcha 复选框")

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
        media_type = "image/png"
        if image and self.options.compress_uploads:
            original = len(image)
            image, media_type = compress_for_vision(image)
            if len(image) < original:
                self._safe_log(
                    f"挑战图已压缩：{original / 1024:.0f}KB → {len(image) / 1024:.0f}KB"
                )
        request: dict[str, Any] = {
            "round": round_number,
            "prompt": prompt,
            "task_type": hint,
            "image": image,
            "media_type": media_type,
            "tiles": tiles,
            "action_target": action_target,
        }
        # 只有需要坐标的任务才需要网格；grid 型用图块序号，多一张图纯属干扰与开销。
        # 网格叠加在压缩后的图上，保证两张图尺度一致且请求体足够小。
        if image and hint in {"point", "drag", "unknown"} and self.options.coordinate_grid:
            grid = coordinate_grid_overlay(image)
            if grid:
                if self.options.compress_uploads:
                    grid, _grid_type = compress_for_vision(grid)
                request["grid_image"] = grid
        return request

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
        payload = dict(request)
        try:
            signature = inspect.signature(method)
            # 可选增强键：注入式客户端可能按旧签名编写，逐个摘除直到能绑定，
            # 而不是让整轮求解因为一个新参数而失败。顺序即放弃优先级。
            for optional in ("grid_image", "media_type"):
                try:
                    signature.bind(**payload)
                    break
                except TypeError:
                    if optional not in payload:
                        continue
                    payload.pop(optional, None)
                    self._safe_log(f"视觉客户端不接受 {optional}，本次调用已省略该参数")
            else:
                signature.bind(**payload)
            signature.bind(**payload)
        except (TypeError, ValueError):
            self._vision_failure = {"failure_stage": "client_signature", "error_type": "TypeError"}
            return None

        # 请求侧可观测：记录模型 id、尺寸与结构。图像字节与 API Key 永不入日志。
        image_bytes = len(payload.get("image") or b"")
        grid_bytes = len(payload.get("grid_image") or b"")
        prompt_text = str(payload.get("prompt") or "").strip()
        config = getattr(client, "config", None)
        model_id = str(getattr(config, "model", "") or "") or "<未知模型>"
        endpoint = str(getattr(config, "base_url", "") or "")
        request_timeout = getattr(config, "timeout", None)
        self._safe_log(
            f"调用视觉模型：model={model_id}"
            + (f" endpoint={endpoint}" if endpoint else "")
            + (f" timeout={request_timeout}s" if request_timeout else "")
            + f"，第 {payload.get('round')} 轮，类型 {payload.get('task_type')}，"
            f"主图 {image_bytes / 1024:.1f}KB"
            + (f"，网格图 {grid_bytes / 1024:.1f}KB" if grid_bytes else "，无网格图")
            + (f"，图块 {len(payload.get('tiles') or [])} 个" if payload.get("tiles") else "")
            + (f"，题面「{prompt_text}」" if prompt_text else "，题面为空")
        )
        started = asyncio.get_running_loop().time()
        try:
            answer = await _maybe_await(method(**payload))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 传输/服务错误转成结构化失败。异常文本可能带服务端正文，交给
            # mask_secrets 兜底脱敏后再输出，便于定位真实原因（如 HTTP 424）。
            self._vision_failure = _safe_exception_detail(exc, stage="vision_request")
            status = self._vision_failure.get("http_status")
            # 校验失败会带上模型实际返回：没有它就无法区分「远端不可用」与
            # 「模型判成 unknown 且不给动作」，两者处置完全不同。
            model_output = getattr(exc, "model_output", None)
            if model_output is not None:
                self._vision_failure["failure_stage"] = "vision_schema"
            self._safe_log(
                f"视觉模型调用失败：model={model_id} {self._vision_failure.get('error_type')}"
                + (f"（HTTP {status}）" if isinstance(status, int) else "")
                + f"，耗时 {asyncio.get_running_loop().time() - started:.1f}s"
                + f"，详情：{_mask(str(exc))}"
                + (
                    f"，模型原始返回：{_mask(_render_answer(model_output))}"
                    if model_output is not None
                    else ""
                )
            )
            return None
        raw_output = getattr(answer, "raw_output", None)
        self._safe_log(
            f"模型原始返回：{_mask(_render_answer(raw_output if raw_output is not None else answer))}"
        )
        elapsed = asyncio.get_running_loop().time() - started
        validated = _validated_answer(answer)
        if validated is None:
            self._safe_log(f"视觉模型返回无法解析的结果：model={model_id}，耗时 {elapsed:.1f}s")
            return None
        if validated.get("_invalid"):
            # schema 校验未通过：这里只记录，不提前返回。低置信度、动作越界等情形
            # 仍要交给 _apply_answer 判为 uncertain（不点击但可继续下一轮），
            # 提前返回会把它们错报成 failed 并终止整轮求解。
            self._safe_log(
                f"视觉结果未通过 schema 校验：model={model_id}，"
                f"类型 {validated.get('challenge_type')}，耗时 {elapsed:.1f}s，"
                f"内容：{_mask(_render_answer(validated))}"
            )
        kind = str(validated.get("challenge_type") or "unknown")
        counts = {
            key: len(validated.get(key) or [])
            for key in ("tile_indices", "points", "drags")
            if validated.get(key)
        }
        self._safe_log(
            f"视觉模型返回：model={model_id}，类型 {kind}，置信度 {validated.get('confidence')}，"
            f"动作 {counts or '无'}，耗时 {elapsed:.1f}s，"
            f"归一化后内容：{_mask(_render_answer(validated))}"
        )
        return validated

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
            self._safe_log("未找到可点击的图块集合")
            return False
        self._safe_log(f"准备点击图块 {indices}")
        for index in indices:
            if not await self._mouse_click_locator(locator.nth(index - 1), label=f"图块 {index}"):
                return False
        submit = await _first_locator(frame, _SUBMIT_SELECTORS)
        if submit is None:
            self._safe_log("未找到提交按钮")
            return False
        return await self._mouse_click_locator(submit, label="提交按钮")

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
            self._safe_log("point 动作无法执行：交互区域无可用坐标")
            return False
        for point in points:
            assert point is not None
            x = box["x"] + box["width"] * point[0] / 1000
            y = box["y"] + box["height"] * point[1] / 1000
            try:
                await _maybe_await(self.page.mouse.move(x, y, steps=7))
                await _maybe_await(self.page.mouse.click(x, y))
            except Exception as exc:
                self._safe_log(f"point 点击异常：{type(exc).__name__}")
                return False
            self._safe_log(
                f"已按刻度点击：({point[0]:.0f}, {point[1]:.0f}) → 页面 ({x:.0f}, {y:.0f})"
            )
        submit = await _first_locator(frame, _SUBMIT_SELECTORS)
        if submit is not None:
            await self._mouse_click_locator(submit, label="提交按钮")
        else:
            self._safe_log("point 动作已完成，未发现提交按钮（可能自动提交）")
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
            self._safe_log("drag 动作无法执行：交互区域无可用坐标")
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
            self._safe_log(f"已拖拽：({sx:.0f}, {sy:.0f}) → ({ex:.0f}, {ey:.0f})")
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
                    if response_field_present or self._network_passed or await self._widget_mounted():
                        return await self._result(
                            "timeout",
                            "hCaptcha 控件已出现，但其 iframe 内部始终无法定位（挂载超时）",
                            failure_stage="widget_mount",
                        )
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

                last_challenge_type = ""
                for round_number in range(1, self.options.max_rounds + 1):
                    try:
                        async with asyncio.timeout(self.options.round_timeout_ms / 1000):
                            # 第 2 轮起先确认挑战仍在呈现：上一轮点击后 hCaptcha 可能
                            # 已收起挑战（等待令牌签发）或换帧未完成，此时截图是空白图，
                            # 发出去只会得到「图中没有目标」的空结果，白费一次调用。
                            if round_number > 1 and not await self._challenge_is_live(challenge_frame):
                                self._safe_log("挑战已不在呈现状态，等待令牌或新一帧题面")
                                token, next_frame, passed = await self._wait_for_progress()
                                if token or passed:
                                    return await self._result(
                                        "success",
                                        "hCaptcha 验证成功",
                                        token=token,
                                        rounds=round_number - 1,
                                        challenge_type=last_challenge_type,
                                    )
                                refreshed = await self._wait_for_live_challenge(
                                    next_frame or challenge_frame
                                )
                                if refreshed is None:
                                    return await self._result(
                                        "timeout",
                                        "hCaptcha 上一轮动作后未出现新题面，也未签发令牌",
                                        rounds=round_number - 1,
                                        challenge_type=last_challenge_type,
                                        failure_stage="challenge_dismissed",
                                    )
                                challenge_frame = refreshed
                            request = await self._capture_round(challenge_frame, round_number)
                            action_target = request.pop("action_target", None)
                            if not request.get("image"):
                                return await self._result(
                                    "failed",
                                    "hCaptcha 挑战截图为空，未向模型发起调用",
                                    rounds=round_number,
                                    failure_stage="empty_capture",
                                )
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
                            last_challenge_type = challenge_type
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
