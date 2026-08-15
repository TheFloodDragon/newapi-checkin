#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hCaptcha 浏览器交互核心。

本模块只编排浏览器中正常呈现的 hCaptcha 控件：读取页面签发的响应、使用真实
鼠标操作复选框，并在需要时把挑战截图交给可注入的视觉客户端。模型密钥、截图
字节和模型原始回复不会写入日志。
"""

from __future__ import annotations

import asyncio
import hashlib
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
_SKIP_BUTTON_MARKERS = ("skip", "跳过", "略过")
_TEMPORAL_PROMPT_MARKERS = (
    "grows",
    "grow",
    "jumps",
    "jump",
    "highest",
    "moves",
    "moving",
    "changes",
    "change",
    "rotates",
    "rotate",
    "spins",
    "spin",
    "appears",
    "disappears",
    "变大",
    "生长",
    "跳得最高",
    "移动",
    "变化",
    "旋转",
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
    # 视觉请求最多尝试次数，以及从单轮预算中预留给退避/解析/动作的时间。
    # 每次 HTTP 超时 = (round_timeout_ms - vision_retry_reserve_ms) / attempts。
    vision_max_attempts: int = 3
    vision_retry_reserve_ms: int = 4_000
    presence_timeout_ms: int = 8_000
    # 为 point/drag 挑战附带一张叠加了 0-1000 坐标网格的辅助图，让模型照刻度读数
    # 而不是凭空估算坐标。纯本地 Pillow 绘制，失败会自动退回只发原图。
    coordinate_grid: bool = True
    # 上传前把截图压成 JPEG。全尺寸 PNG（主图+网格约 690KB）实测会让视觉端点在 60s
    # 超时失败；压缩后 23s 返回且置信度更高。
    compress_uploads: bool = True
    vision_max_edge: int = 640
    vision_jpeg_quality: int = 82
    # 时间型 point 题（如 grows / jumps highest）必须观察多帧。默认只取 1 帧；
    # 单站可开启连续采样并附带 3×2 序列图，不影响其它站点与静态题。
    temporal_frames: int = 1
    temporal_interval_ms: int = 350
    temporal_sheet_max_edge: int = 800
    # 模型返回后等待动画重新经过最后采样帧的相位，匹配后立即点击。
    temporal_phase_wait_ms: int = 5_000
    # widget 元素已在页面上、但 iframe 内部控件尚未可查询时的额外等待预算。
    # 实测 ABR 福利站从 domcontentloaded 到 checkbox frame 可定位需 10s 以上，
    # 只用 presence_timeout_ms 会在挂载完成前就判定「未发现 hCaptcha」。
    widget_mount_timeout_ms: int = 25_000
    post_action_wait_ms: int = 5_000
    poll_interval_ms: int = 200
    # 单次拟人化 mouse.move 的上限。Camoufox humanize=True 下实测一次移动可达
    # 17s，两段就是 29.5s；不设上限会让点击开销吃光整轮预算。
    move_timeout_ms: int = 3_000
    # 是否在 click 前执行拟人化 mouse.move。浏览器已关闭 humanize 的站点可关闭，
    # 避免 move 取消后底层请求仍占驱动队列，使后续 click 卡满整轮预算。
    move_before_click: bool = True
    click_timeout_ms: int = 5_000
    # 拖拽必须保持 mouse.down，不能像点击一样超时后直接落点。实测 humanize=True：
    # steps=1 约 3s、steps=2 约 4s、steps=5 虽通常 8s 但真实运行会随机超过 15s、
    # steps=15 约 26s。因此拖拽使用 1/2 步真实鼠标轨迹，并给每段 8s 硬上限。
    drag_move_timeout_ms: int = 8_000
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
            vision_max_attempts=max(1, int(self.vision_max_attempts)),
            vision_retry_reserve_ms=max(0, int(self.vision_retry_reserve_ms)),
            presence_timeout_ms=max(0, int(self.presence_timeout_ms)),
            coordinate_grid=bool(self.coordinate_grid),
            compress_uploads=bool(self.compress_uploads),
            vision_max_edge=max(128, min(1600, int(self.vision_max_edge))),
            vision_jpeg_quality=max(40, min(95, int(self.vision_jpeg_quality))),
            temporal_frames=max(1, min(8, int(self.temporal_frames))),
            temporal_interval_ms=max(50, min(2_000, int(self.temporal_interval_ms))),
            temporal_sheet_max_edge=max(256, min(1_600, int(self.temporal_sheet_max_edge))),
            temporal_phase_wait_ms=max(500, min(15_000, int(self.temporal_phase_wait_ms))),
            widget_mount_timeout_ms=max(0, int(self.widget_mount_timeout_ms)),
            post_action_wait_ms=max(0, int(self.post_action_wait_ms)),
            poll_interval_ms=max(10, int(self.poll_interval_ms)),
            move_timeout_ms=max(100, int(self.move_timeout_ms)),
            move_before_click=bool(self.move_before_click),
            click_timeout_ms=max(100, int(self.click_timeout_ms)),
            drag_move_timeout_ms=max(100, int(self.drag_move_timeout_ms)),
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


async def _attribute(locator: Any, name: str) -> str:
    """有界读取元素属性；locator 已替换/分离时返回空串。"""
    if locator is None:
        return ""
    method = getattr(locator, "get_attribute", None)
    if not callable(method):
        return ""
    try:
        return str(await _probe(method(name), "") or "").strip()
    except Exception:
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


def _task_type_from_prompt(prompt: str, *, has_tiles: bool) -> str:
    """从可见题面推断基础任务类型，网络元数据缺失时使用。"""
    if has_tiles:
        return "grid"
    text = str(prompt or "").strip().casefold()
    # 当前站点实测题面稳定使用 Please drag... / Please click...。显式类型不仅
    # 改善模型理解，也让 action_target 选择实际交互图片而不是含标题的整块容器，
    # 避免坐标基准偏移。
    if "drag" in text:
        return "drag"
    if "click" in text:
        return "point"
    # 单图选择题实测使用 "Find all animals based on the number provided"，DOM 没有
    # 独立 task-image tile，仍需返回多个 point 坐标。
    if text.startswith("find all ") or "based on the number provided" in text:
        return "point"
    # 新版题面会用命令式 "Move ONE animal to ..."，没有 drag 这个词；
    # 但 "click on the animal that moves" 仍应是 point，因此只匹配句首命令。
    if text.startswith("move ") or text.startswith("please move "):
        return "drag"
    return "unknown"


def _is_temporal_prompt(prompt: str) -> bool:
    """题面是否要求比较时间变化，而非从单帧即可判断。"""
    text = str(prompt or "").strip().casefold()
    return bool(text and any(marker in text for marker in _TEMPORAL_PROMPT_MARKERS))


def _image_fingerprint(image: bytes) -> str:
    """生成抗轻微压缩差异的 dHash；解码失败时退回 SHA-256。"""
    if not image:
        return ""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as source:
            gray = source.convert("L").resize((9, 8), Image.LANCZOS)
        flattened = getattr(gray, "get_flattened_data", None)
        pixels = list(flattened() if callable(flattened) else gray.getdata())
        bits = 0
        for row in range(8):
            offset = row * 9
            for column in range(8):
                bits = (bits << 1) | int(pixels[offset + column] > pixels[offset + column + 1])
        return f"dhash:{bits:016x}"
    except Exception:
        return "sha256:" + hashlib.sha256(image).hexdigest()


def _same_image_fingerprint(left_hash: str, right_hash: str, *, max_distance: int = 8) -> bool:
    """比较两个感知指纹，容忍截图/压缩造成的轻微像素变化。"""
    if not left_hash or not right_hash:
        return False
    if left_hash.startswith("dhash:") and right_hash.startswith("dhash:"):
        left_bits = int(left_hash.split(":", 1)[1], 16)
        right_bits = int(right_hash.split(":", 1)[1], 16)
        return (left_bits ^ right_bits).bit_count() <= max(0, int(max_distance))
    return left_hash == right_hash


def _same_challenge_image(left: bytes, right: bytes, *, max_distance: int = 8) -> bool:
    """判断模型请求前后是否仍是同一题图。"""
    return _same_image_fingerprint(
        _image_fingerprint(left), _image_fingerprint(right), max_distance=max_distance
    )


def _image_is_nearly_blank(image: bytes, *, min_stddev: float = 6.0) -> bool:
    """判断截图是否几乎没有内容（hCaptcha 换帧过渡期的空壳画面）。

    实测同一题面的正常截图约 271KB，而换帧过渡帧只有 5KB 且几乎是纯色。把这种图
    发给模型只会得到 confidence=0、空 points，白费一次请求与一次 schema 重试。

    用灰度标准差判定而非字节数：字节数受 JPEG 质量与分辨率影响，不可靠；纯色/近纯色
    画面的标准差必然很低，而任何真实题面都有明显的明暗结构。

    无法解码时返回 False —— 宁可照旧发出请求，也不要因判定失败而漏掉真实题面。
    """
    if not image:
        return False
    try:
        from PIL import Image, ImageStat

        with Image.open(io.BytesIO(image)) as source:
            grayscale = source.convert("L")
            stat = ImageStat.Stat(grayscale)
            stddev = float(stat.stddev[0]) if stat.stddev else 0.0
        return stddev < float(min_stddev)
    except Exception:
        return False


def _image_mean_difference(left: bytes, right: bytes, *, size: tuple[int, int] = (48, 48)) -> float | None:
    """返回两张截图归一化后的 RGB 平均差，供动画相位匹配使用。"""
    if not left or not right:
        return None
    try:
        from PIL import Image

        with Image.open(io.BytesIO(left)) as left_image, Image.open(io.BytesIO(right)) as right_image:
            left_resized = left_image.convert("RGB").resize(size)
            right_resized = right_image.convert("RGB").resize(size)
            left_flattened = getattr(left_resized, "get_flattened_data", None)
            right_flattened = getattr(right_resized, "get_flattened_data", None)
            left_pixels = list(
                left_flattened() if callable(left_flattened) else left_resized.getdata()
            )
            right_pixels = list(
                right_flattened() if callable(right_flattened) else right_resized.getdata()
            )
        if len(left_pixels) != len(right_pixels):
            return None
        return sum(
            abs(left_channel - right_channel)
            for left_pixel, right_pixel in zip(left_pixels, right_pixels)
            for left_channel, right_channel in zip(left_pixel, right_pixel)
        ) / (len(left_pixels) * 3 * 255)
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


def detect_move_sources(image: bytes) -> list[tuple[float, float]]:
    """定位 drag 题里全部可拖动源，返回 0-1000 归一化坐标列表；失败返回空。

    hCaptcha 的拖拽题把整幅题面画在单个 canvas 上，DOM 里没有源元素节点（已实测：
    challenge iframe 内只有一个 aria-label="Image-based CAPTCHA challenge" 的
    canvas），因此源只能从像素里找。

    每个可拖动元素的顶部都压着一个近黑圆角胶囊，内含白色十字箭头图标与 "Move"
    字样。直接找深色胶囊并不可靠：多源题（"Move ONE animal to the matching
    silhouette"）的胶囊会与同样深色的左侧面板连成一片，实测三个胶囊只能检出一个。
    改为找"上下都被深色夹住的亮字团"，既排除大面积白卡片与青色题面横幅上的白字，
    也不受胶囊与深色面板连通的影响；实测三源题稳定检出 3 个、单源题检出 1 个。

    这是渐进增强：任何一步不确定就返回空列表，把判断交回视觉模型，绝不猜。
    """
    if not image:
        return []
    try:
        import cv2
        import numpy as np
    except Exception:
        return []
    try:
        frame = cv2.imdecode(np.frombuffer(image, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            return []
        height, width = frame.shape[:2]
        if width < 40 or height < 40:
            return []
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        value = hsv[:, :, 2].astype(np.int16)
        saturation = hsv[:, :, 1].astype(np.int16)
        bright = ((value > 165) & (saturation < 80)).astype(np.uint8) * 255
        dark = ((value < 95) & (saturation < 130)).astype(np.uint8) * 255

        # 尺寸阈值按 500px 宽的挑战图标定，按实际宽度等比缩放以适配缩略图。
        scale = width / 500.0
        reach = max(3, int(round(8 * scale)))
        span = reach * 2 + 1
        kernel_above = np.zeros((span, 1), np.uint8)
        kernel_above[: reach + 1] = 1
        kernel_below = np.zeros((span, 1), np.uint8)
        kernel_below[reach:] = 1
        sandwiched = cv2.bitwise_and(
            bright,
            cv2.bitwise_and(cv2.dilate(dark, kernel_above), cv2.dilate(dark, kernel_below)),
        )
        merged = cv2.morphologyEx(
            sandwiched, cv2.MORPH_CLOSE, np.ones((3, max(3, int(round(13 * scale)))), np.uint8)
        )
        contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        labels: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, label_width, label_height = cv2.boundingRect(contour)
            if not (18 * scale <= label_width <= 90 * scale):
                continue
            if not (6 * scale <= label_height <= 22 * scale):
                continue
            if not 1.8 <= label_width / max(1, label_height) <= 8.0:
                continue
            labels.append((x, y, label_width, label_height))
        if not labels:
            return []
        # 同一题里每个 Move 胶囊的尺寸完全一致，据此剔除偶发误检（实测某轮多出
        # 27×7 与 35×6 两个杂块）。取尺寸相同、数量最多的一组。
        groups: dict[tuple[int, int], list[tuple[int, int, int, int]]] = {}
        for item in labels:
            groups.setdefault((item[2], item[3]), []).append(item)
        chosen = max(groups.values(), key=lambda group: (len(group), group[0][2]))
        sources: list[tuple[float, float]] = []
        for x, y, label_width, label_height in sorted(chosen, key=lambda item: (item[1], item[0])):
            center_x = x + label_width / 2
            # 胶囊压在元素顶边上；实测按字团宽度的 0.8 倍下移即落在元素主体内，
            # 对白色动物卡片与半透明形状方块都成立。
            center_y = y + label_height + 0.8 * label_width
            if not (0 <= center_x <= width and 0 <= center_y <= height):
                continue
            sources.append((center_x / width * 1000, center_y / height * 1000))
        return sources
    except Exception:
        return []


def detect_move_source(image: bytes) -> tuple[float, float] | None:
    """只在能确定唯一可拖动源时返回它，否则返回 None。"""
    sources = detect_move_sources(image)
    return sources[0] if len(sources) == 1 else None


def drag_detail_sheet(
    image: bytes,
    source_points: Sequence[tuple[float, float]],
    *,
    max_edge: int = 900,
    quality: int = 82,
) -> bytes:
    """放大 drag 题的源侧和目标场景，并标注原图 0-1000 坐标刻度。

    视觉端点使用 400px 主图时，单个多面体或动物剪影通常只有约 50px，内部棱线和
    轮廓会在 JPEG 缩放后丢失。辅助图将源侧与相反侧场景分别裁出、放大后并排展示；
    场景刻度仍按第一张原图换算，模型不得返回辅助拼图自身的像素坐标。

    源侧无法确定（没有检测点或检测点横跨两侧）时返回空，保持原流程不变。
    """
    if not image or not source_points:
        return b""
    try:
        from PIL import Image, ImageDraw

        with Image.open(io.BytesIO(image)) as source:
            canvas = source.convert("RGB")
        width, height = canvas.size
        if width < 80 or height < 80:
            return b""

        normalized = [
            (float(x), float(y))
            for x, y in source_points
            if math.isfinite(float(x))
            and math.isfinite(float(y))
            and 0 <= float(x) <= 1000
            and 0 <= float(y) <= 1000
        ]
        if not normalized:
            return b""
        mean_x = sum(point[0] for point in normalized) / len(normalized)
        # 中线附近无法可靠区分源侧和场景侧；不生成可能误导模型的辅助图。
        if 400 <= mean_x <= 600:
            return b""
        source_left = mean_x < 500
        content_top = max(0, min(height - 1, round(height * 0.23)))
        source_split = round(width * (0.34 if source_left else 0.66))
        if source_left:
            source_box = (0, content_top, source_split, height)
            scene_box = (round(width * 0.28), content_top, width, height)
        else:
            source_box = (source_split, content_top, width, height)
            scene_box = (0, content_top, round(width * 0.72), height)

        # 单源题只需展示源对象附近，进一步放大内部棱线；多源动物题保留整列卡片。
        if len(normalized) == 1:
            _nx, ny = normalized[0]
            source_y = ny / 1000 * height
            y0 = max(content_top, round(source_y - height * 0.13))
            y1 = min(height, round(source_y + height * 0.22))
            if y1 - y0 >= 40:
                source_box = (source_box[0], y0, source_box[2], y1)

        source_panel = canvas.crop(source_box)
        scene_panel = canvas.crop(scene_box)
        scene_draw = ImageDraw.Draw(scene_panel, "RGBA")
        scene_width, scene_height = scene_panel.size
        sx0, sy0, sx1, sy1 = scene_box

        # 低透明网格只负责给目标中心读数；每 200 刻度加标签，避免遮住细小线框。
        for tick in range(0, 1001, 100):
            original_x = tick / 1000 * width
            if sx0 <= original_x <= sx1:
                x = round(original_x - sx0)
                scene_draw.line((x, 0, x, scene_height), fill=(255, 255, 255, 55), width=1)
                if tick % 200 == 0:
                    scene_draw.rectangle((x + 2, 2, x + 34, 16), fill=(0, 0, 0, 150))
                    scene_draw.text((x + 4, 3), str(tick), fill=(255, 255, 255, 255))
            original_y = tick / 1000 * height
            if sy0 <= original_y <= sy1:
                y = round(original_y - sy0)
                scene_draw.line((0, y, scene_width, y), fill=(255, 255, 255, 55), width=1)
                if tick % 200 == 0:
                    scene_draw.rectangle((2, y + 2, 34, y + 16), fill=(0, 0, 0, 150))
                    scene_draw.text((4, y + 3), str(tick), fill=(255, 255, 255, 255))

        source_draw = ImageDraw.Draw(source_panel, "RGBA")
        px0, py0, _px1, _py1 = source_box
        for nx, ny in normalized:
            x = nx / 1000 * width - px0
            y = ny / 1000 * height - py0
            if 0 <= x <= source_panel.width and 0 <= y <= source_panel.height:
                source_draw.ellipse((x - 8, y - 8, x + 8, y + 8), outline=(255, 40, 40, 255), width=3)

        panel_height = 500

        def _resize_to_height(panel: Any) -> Any:
            scale = panel_height / max(1, panel.height)
            return panel.resize(
                (max(1, round(panel.width * scale)), panel_height),
                Image.LANCZOS,
            )

        source_panel = _resize_to_height(source_panel)
        scene_panel = _resize_to_height(scene_panel)
        gap = 10
        title_height = 30
        sheet = Image.new(
            "RGB",
            (source_panel.width + gap + scene_panel.width, title_height + panel_height),
            (24, 24, 24),
        )
        sheet.paste(source_panel, (0, title_height))
        sheet.paste(scene_panel, (source_panel.width + gap, title_height))
        draw = ImageDraw.Draw(sheet)
        draw.text((8, 8), "SOURCE (red ring = exact drag start)", fill=(255, 255, 255))
        draw.text(
            (source_panel.width + gap + 8, 8),
            "DESTINATION SCENE (ticks = original 0-1000 coordinates)",
            fill=(255, 255, 255),
        )
        max_edge = max(400, min(1_200, int(max_edge)))
        if max(sheet.size) > max_edge:
            scale = max_edge / max(sheet.size)
            sheet = sheet.resize(
                (max(1, round(sheet.width * scale)), max(1, round(sheet.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        sheet.save(buffer, format="JPEG", quality=max(50, min(95, int(quality))), optimize=True)
        return buffer.getvalue()
    except Exception:
        return b""


def temporal_contact_sheet(
    images: Sequence[bytes], *, columns: int = 3, max_edge: int = 800, quality: int = 72
) -> bytes:
    """把同一题面的连续帧排列成时间序列图；失败或不足两帧返回空。"""
    if len(images) < 2:
        return b""
    try:
        from PIL import Image, ImageDraw

        panels: list[Any] = []
        for raw in images:
            if not raw:
                continue
            with Image.open(io.BytesIO(raw)) as source:
                panels.append(source.convert("RGB"))
        if len(panels) < 2:
            return b""
        width, height = panels[0].size
        panels = [
            panel if panel.size == (width, height) else panel.resize((width, height), Image.LANCZOS)
            for panel in panels
        ]
        columns = max(1, min(int(columns), len(panels)))
        rows = math.ceil(len(panels) / columns)
        sheet = Image.new("RGB", (width * columns, height * rows), "black")
        draw = ImageDraw.Draw(sheet)
        for index, panel in enumerate(panels):
            x = (index % columns) * width
            y = (index // columns) * height
            sheet.paste(panel, (x, y))
            label = f"t{index}"
            draw.rectangle((x + 4, y + 4, x + 32, y + 22), fill=(0, 0, 0))
            draw.text((x + 8, y + 6), label, fill=(255, 255, 255))
        longest = max(sheet.size)
        max_edge = max(256, min(1_600, int(max_edge)))
        if longest > max_edge:
            scale = max_edge / longest
            sheet = sheet.resize(
                (max(1, round(sheet.width * scale)), max(1, round(sheet.height * scale))),
                Image.LANCZOS,
            )
        buffer = io.BytesIO()
        sheet.save(buffer, format="JPEG", quality=max(40, min(95, int(quality))), optimize=True)
        return buffer.getvalue()
    except Exception:
        return b""


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
        # 完全正确的答案判为无效动作而丢弃。另实测 {"center": [800, 350]}。
        for key in (
            "point",
            "coordinates",
            "coordinate",
            "position",
            "pos",
            "center",
            "centre",
            "center_point",
        ):
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
    raw_points = value.get("points")
    if raw_points is None:
        raw_points = value.get("point")
    if raw_points is None and challenge_type == "point":
        raw_points = value.get("target")
    if raw_points is None:
        raw_points = []
    # elements/paths 是模型实测用过的等价键名；另有顶层
    # source_point/target_point，统一包装成单次 drag。
    raw_drags = value.get(
        "drags", value.get("drag", value.get("action", value.get("elements", value.get("paths"))))
    )
    if isinstance(raw_drags, Mapping):
        raw_drags = [raw_drags]
    if raw_drags is None and ("source_point" in value or "target_point" in value):
        raw_drags = [{"start": value.get("source_point"), "end": value.get("target_point")}]
    if raw_drags is None and ("drag_start" in value or "drag_end" in value):
        raw_drags = [{"start": value.get("drag_start"), "end": value.get("drag_end")}]
    if raw_drags is None and challenge_type in {"drag", "unknown"} and (
        "source" in value or "target" in value
    ):
        raw_drags = [{"start": value.get("source"), "end": value.get("target")}]
    if (
        raw_drags is None
        and challenge_type in {"drag", "unknown"}
        and ("start" in value or "from" in value)
        and ("end" in value or "to" in value)
    ):
        raw_drags = [value]
    if raw_drags is None:
        raw_drags = []
    # 只有「结构上可用的动作」才构成矛盾。非 list 的垃圾字段不算动作。
    def _usable(raw: Any) -> bool:
        return isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) and bool(raw)

    # 单图网格题会把行列位置附在 tile_indices=[[row,column], ...]，同时给出可点击
    # points。嵌套行列不是 1-based tile id；仅在存在合法 point/drag 动作时视作辅助
    # 字段忽略。没有其它动作时仍保留，后续严格判无效，不能把错误 grid 答案放过。
    if _usable(raw_tiles) and not all(
        isinstance(index, int) and not isinstance(index, bool) and index >= 1
        for index in raw_tiles
    ):
        if challenge_type in {"point", "drag"} or (
            challenge_type == "unknown" and (_usable(raw_points) or _usable(raw_drags))
        ):
            raw_tiles = []

    # 实测 drag 计划会附带 points=[end,start]，只是 drag 端点的重复副本。仅在两边
    # 坐标集合完全一一对应时安全丢弃；有任何额外/不同 point 仍按真实冲突拒绝。
    if challenge_type in {"drag", "unknown"} and _usable(raw_points) and _usable(raw_drags):
        point_coords: list[tuple[float, float]] = []
        drag_coords: list[tuple[float, float]] = []
        for raw_point in raw_points:
            normalized = _point(raw_point)
            if normalized is None:
                break
            point_coords.append(normalized)
        else:
            for raw_drag in raw_drags:
                if not isinstance(raw_drag, Mapping):
                    break
                start = _point(
                    raw_drag.get(
                        "start",
                        raw_drag.get(
                            "from", raw_drag.get("start_point", raw_drag.get("source"))
                        ),
                    )
                )
                end = _point(
                    raw_drag.get(
                        "end", raw_drag.get("to", raw_drag.get("end_point", raw_drag.get("target")))
                    )
                )
                if start is None or end is None:
                    break
                drag_coords.extend((start, end))
            else:
                if sorted(point_coords) == sorted(drag_coords):
                    raw_points = []

    # 与 VisionPlan.from_mapping 一致：unknown + 恰好一种动作时按动作反推类型。
    if challenge_type == "unknown":
        present = [
            name
            for name, values in (("grid", raw_tiles), ("point", raw_points), ("drag", raw_drags))
            if _usable(values)
        ]
        if len(present) == 1 and not value.get("actions"):
            challenge_type = present[0]
            canonical["challenge_type"] = challenge_type

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
        points = value.get("points")
        if points is None:
            points = value.get("actions")
        if points is None:
            points = value.get("point", value.get("target", []))
        canonical["points"] = [points] if isinstance(points, Mapping) else points
    elif challenge_type == "drag":
        drags = value.get("drags")
        if drags is None:
            action = value.get("drag", value.get("action"))
            if action is None:
                # elements/paths 是实测出现过的等价键名，需与上面的 raw_drags 一致。
                action = value.get("elements", value.get("paths"))
            if action is None and ("source_point" in value or "target_point" in value):
                action = {"start": value.get("source_point"), "end": value.get("target_point")}
            if action is None and ("drag_start" in value or "drag_end" in value):
                action = {"start": value.get("drag_start"), "end": value.get("drag_end")}
            if action is None and challenge_type in {"drag", "unknown"} and (
                "source" in value or "target" in value
            ):
                action = {"start": value.get("source"), "end": value.get("target")}
            if action is None and ("start" in value or "from" in value):
                action = value
            drags = [action] if isinstance(action, Mapping) else action or []
        normalized_drags: list[dict[str, Any]] = []
        if isinstance(drags, Sequence) and not isinstance(drags, (str, bytes)):
            for drag in drags:
                if not isinstance(drag, Mapping):
                    canonical["drags"] = drags
                    break
                # 与 VisionPlan.from_mapping 保持一致：实测模型会用
                # start_point / end_point / source / target / drag_start 等别名。
                start = _point(
                    drag.get(
                        "start",
                        drag.get(
                            "from",
                            drag.get(
                                "start_point", drag.get("source", drag.get("drag_start"))
                            ),
                        ),
                    )
                )
                end = _point(
                    drag.get(
                        "end",
                        drag.get(
                            "to", drag.get("end_point", drag.get("target", drag.get("drag_end")))
                        ),
                    )
                )
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
        # 当前轮的截止时刻（事件循环时间）。schema 重试必须先确认剩余预算，
        # 否则一次重试就会把整轮预算耗尽并把可继续的求解变成轮超时。
        self._round_deadline: float | None = None
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

    async def _wait_for_live_challenge(
        self, frame: Any, *, timeout_ms: int | None = None
    ) -> Any | None:
        """等待挑战真正呈现题面，返回可求解的 frame。

        首次点击复选框后的题面挂载可能明显慢于轮次动作后的换题：调用方可传入
        widget_mount_timeout_ms；后续轮次不传则继续使用 post_action_wait_ms。
        """
        loop = asyncio.get_running_loop()
        wait_ms = self.options.post_action_wait_ms if timeout_ms is None else max(0, timeout_ms)
        deadline = loop.time() + wait_ms / 1000
        candidate = frame
        try:
            # 逻辑 deadline 只能在两次循环间检查；一次多 iframe 扫描本身也可能很慢。
            # 方法级 timeout 确保整个等待不会突破子预算。
            async with asyncio.timeout(max(0.1, wait_ms / 1000 + 2.0)):
                while True:
                    _checkbox, current = await self._find_frames()
                    if current is not None:
                        candidate = current
                    if await self._challenge_is_live(candidate):
                        return candidate
                    if loop.time() >= deadline:
                        return candidate if await self._challenge_is_live(candidate) else None
                    await asyncio.sleep(self.options.poll_interval_ms / 1000)
        except TimeoutError:
            return None

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

    def _box_intersects_viewport(self, box: Mapping[str, float]) -> bool:
        """元素坐标是否至少与当前视口相交。"""
        values = [box.get(key) for key in ("x", "y", "width", "height")]
        if any(value is None or not math.isfinite(float(value)) for value in values):
            return False
        x, y, width, height = (float(value) for value in values)
        # 完全位于视口左侧/上方的节点通常是 hCaptcha 保留的离屏旧挑战。
        if x + width <= 0 or y + height <= 0:
            return False
        viewport = getattr(self.page, "viewport_size", None)
        if isinstance(viewport, Mapping):
            vw, vh = viewport.get("width"), viewport.get("height")
            if isinstance(vw, (int, float)) and isinstance(vh, (int, float)):
                if x >= float(vw) or y >= float(vh):
                    return False
        return True

    async def _first_submit_locator(self, frame: Any, *, timeout_ms: int = 1_500) -> Any | None:
        """寻找当前视口中的真实提交按钮，排除 Skip 与离屏旧节点。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_ms) / 1000
        logged_skip = False
        logged_offscreen = False
        while True:
            for selector in _SUBMIT_SELECTORS:
                try:
                    locator = frame.locator(selector)
                    count = await _count(locator)
                except Exception:
                    continue
                for index in range(count):
                    item = locator.nth(index)
                    if not await _is_visible(item):
                        continue
                    text = (await _text(item)).strip().casefold()
                    if text and any(marker in text for marker in _SKIP_BUTTON_MARKERS):
                        if not logged_skip:
                            self._safe_log(f"忽略 hCaptcha 跳过按钮：{text}")
                            logged_skip = True
                        continue
                    box = await _box(item)
                    if box is None or not self._box_intersects_viewport(box):
                        if not logged_offscreen:
                            self._safe_log("忽略离屏或坐标异常的 hCaptcha 提交候选")
                            logged_offscreen = True
                        continue
                    return item
            if loop.time() >= deadline:
                return None
            await asyncio.sleep(self.options.poll_interval_ms / 1000)

    async def _locator_click(
        self,
        locator: Any,
        *,
        label: str,
        position: Mapping[str, float] | None = None,
    ) -> bool:
        """优先使用 Playwright locator 发送可信且有界的元素点击。"""
        click = getattr(locator, "click", None)
        if not callable(click):
            return False
        kwargs: dict[str, Any] = {
            "force": True,
            "timeout": self.options.click_timeout_ms,
        }
        if position is not None:
            kwargs["position"] = dict(position)
        try:
            signature = inspect.signature(click)
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if not accepts_kwargs:
                kwargs = {
                    key: item
                    for key, item in kwargs.items()
                    if key in signature.parameters
                }
        except (TypeError, ValueError):
            pass
        budget = self.options.click_timeout_ms / 1000
        try:
            await asyncio.wait_for(
                asyncio.ensure_future(_as_coro(click(**kwargs))),
                timeout=budget + 0.5,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            self._safe_log(f"元素级点击{label}超过 {budget:.1f}s")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._safe_log(f"元素级点击{label}异常：{type(exc).__name__}")
            return False

    async def _mouse_click_at(self, x: float, y: float, *, label: str) -> bool:
        """执行有界真实鼠标点击，避免驱动请求卡满整轮预算。"""
        budget = self.options.click_timeout_ms / 1000
        try:
            await asyncio.wait_for(
                asyncio.ensure_future(_as_coro(self.page.mouse.click(x, y))),
                timeout=budget,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            self._safe_log(f"点击{label}超过 {budget:.1f}s，放弃本次动作")
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._safe_log(f"点击{label}异常：{type(exc).__name__}")
            return False

    async def _mouse_click_locator(self, locator: Any, label: str = "元素") -> bool:
        box = await _box(locator)
        if box is None:
            self._safe_log(f"点击{label}失败：元素无可用坐标（不可见或已分离）")
            return False
        if not self._box_intersects_viewport(box):
            self._safe_log(f"点击{label}失败：元素位于视口外或坐标异常")
            return False
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        # 浏览器已关闭 humanize 的站点优先使用元素级可信点击，避免 page.mouse
        # 在 Camoufox 中被取消的 move 请求占住驱动队列。
        if not self.options.move_before_click:
            if await self._locator_click(locator, label=label):
                self._safe_log(f"已点击{label}：({x:.0f}, {y:.0f})")
                return True
        else:
            # humanize=True 的站点继续保留 approach + settle 轨迹，但每段有上限。
            await self._humanized_move(x - 24, y - 8, steps=5)
            await self._humanized_move(x, y, steps=7)
        if not await self._mouse_click_at(x, y, label=label):
            return False
        self._safe_log(f"已点击{label}：({x:.0f}, {y:.0f})")
        return True

    async def _humanized_move(
        self, x: float, y: float, *, steps: int, timeout_ms: int | None = None
    ) -> bool:
        """执行一次有界拟人化移动，返回是否到达目标。

        普通点击可在 False 后用带绝对坐标的 click 兜底；拖拽则必须在 mouse.down 前
        确认到达起点、在 mouse.up 前确认到达终点，因此调用方会检查返回值。
        """
        budget = max(100, timeout_ms or self.options.move_timeout_ms) / 1000
        try:
            await asyncio.wait_for(
                asyncio.ensure_future(_as_coro(self.page.mouse.move(x, y, steps=steps))),
                timeout=budget,
            )
            return True
        except (TimeoutError, asyncio.TimeoutError):
            self._safe_log(f"拟人化移动超过 {budget:.1f}s，改用直接落点或放弃本次动作")
            return False
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

    async def _checkbox_checked(self, frame: Any) -> bool | None:
        """返回复选框明确的 aria-checked 状态；无法确认时返回 None。"""
        locator = await _first_locator(frame, _CHECKBOX_SELECTORS)
        aria_checked = (await _attribute(locator, "aria-checked")).casefold()
        if aria_checked == "true":
            return True
        if aria_checked == "false":
            return False
        return None

    async def _click_checkbox(self, frame: Any) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.widget_mount_timeout_ms / 1000
        try:
            async with asyncio.timeout(max(1.0, self.options.widget_mount_timeout_ms / 1000)):
                while True:
                    fresh_frame, _challenge = await self._find_frames()
                    candidate = fresh_frame or frame
                    locator = await _first_locator(candidate, _CHECKBOX_SELECTORS)
                    if locator is not None:
                        if self.options.move_before_click:
                            clicked = await self._mouse_click_locator(
                                locator, label="hCaptcha 复选框"
                            )
                            if not clicked:
                                clicked = await self._locator_click(
                                    locator, label="hCaptcha 复选框"
                                )
                        else:
                            # 直接模式只用 fresh locator 的可信 click，绝不把失败的
                            # page.mouse.move/click 请求排队到浏览器驱动中。
                            clicked = await self._locator_click(
                                locator, label="hCaptcha 复选框"
                            )
                        if clicked:
                            return True
                    if loop.time() >= deadline:
                        break
                    await asyncio.sleep(self.options.poll_interval_ms / 1000)
        except TimeoutError:
            pass
        self._safe_log("在挂载预算内未能点击 hCaptcha 复选框")
        return False

    async def _wait_for_progress(self) -> tuple[str, Any | None, bool]:
        loop = asyncio.get_running_loop()
        wait_ms = self.options.post_action_wait_ms
        deadline = loop.time() + wait_ms / 1000
        last_challenge = None
        try:
            async with asyncio.timeout(max(0.1, wait_ms / 1000 + 2.0)):
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
        except TimeoutError:
            return "", last_challenge, False

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

        hint = self._network_task_type or _task_type_from_prompt(prompt, has_tiles=bool(tiles))
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

        # 题面指纹必须基于「未经我们装饰的画面」。grid 题的红色序号标签是本地注入的
        # 辅助层，只给模型看；复核截图时不会再注入，若拿带标签的图做指纹，dHash 必然
        # 与复核图不同，模型每轮的正确答案都会被误判为「题面已更新」而丢弃 ——
        # 实测 grid 题因此连续 4 轮白跑直到用尽最大轮数。
        clean_image = image
        if labels_added and action_target is not None:
            reference = await _screenshot_locator(self.page, action_target)
            if reference:
                clean_image = reference

        self._diagnostic_target = challenge or action_target
        # drag 题的可拖动源在像素里可稳定定位（近黑 "Move" 胶囊标签），而实测模型
        # 经常把背景装饰或目标本身当成起点。在压缩前用原始截图检测，精度最高。
        drag_source_points = detect_move_sources(image) if hint == "drag" else []
        if drag_source_points:
            listed = "、".join(f"({x:.0f}, {y:.0f})" for x, y in drag_source_points)
            self._safe_log(
                f"已在挑战图中定位 {len(drag_source_points)} 个可拖动源（Move 标签）：{listed}"
            )
        drag_detail_image = (
            drag_detail_sheet(
                image,
                drag_source_points,
                max_edge=max(640, min(800, round(self.options.vision_max_edge * 1.6))),
                quality=max(70, min(82, self.options.vision_jpeg_quality)),
            )
            if hint == "drag" and drag_source_points
            else b""
        )
        if drag_detail_image:
            self._safe_log(f"已生成 drag 源/场景细节图：{len(drag_detail_image) / 1024:.1f}KB")
        media_type = "image/png"
        if image and self.options.compress_uploads:
            original = len(image)
            image, media_type = compress_for_vision(
                image,
                max_edge=self.options.vision_max_edge,
                quality=self.options.vision_jpeg_quality,
            )
            if len(image) < original:
                self._safe_log(
                    f"挑战图已压缩：{original / 1024:.0f}KB → {len(image) / 1024:.0f}KB"
                )
        if clean_image is not image and clean_image and self.options.compress_uploads:
            # 指纹图必须与复核路径同样压缩，否则编码差异本身就会拉开 dHash 距离。
            clean_image, _clean_media_type = compress_for_vision(
                clean_image,
                max_edge=self.options.vision_max_edge,
                quality=self.options.vision_jpeg_quality,
            )
        request: dict[str, Any] = {
            "round": round_number,
            "prompt": prompt,
            "task_type": hint,
            "image": image,
            "media_type": media_type,
            "tiles": tiles,
            "action_target": action_target,
            # 仅供模型返回后的上下文校验；调用视觉客户端前会 pop，不会进入请求体。
            "challenge_fingerprint": _image_fingerprint(clean_image or image),
            "drag_source_points": drag_source_points,
        }
        if drag_source_points:
            # 单独字段传给视觉客户端；prompt 必须保持页面原文，否则题面比对会误判。
            request["source_hint"] = list(drag_source_points)
        if drag_detail_image:
            request["drag_detail_image"] = drag_detail_image
            request["drag_detail_media_type"] = "image/jpeg"
        if (
            image
            and action_target is not None
            and hint == "point"
            and self.options.temporal_frames > 1
            and _is_temporal_prompt(prompt)
        ):
            temporal_frames = [image]
            last_media_type = media_type
            expected_prompt = " ".join(prompt.casefold().split())
            for _index in range(1, self.options.temporal_frames):
                await asyncio.sleep(self.options.temporal_interval_ms / 1000)
                current_prompt = await _text(await _first_locator(frame, _PROMPT_SELECTORS))
                if expected_prompt and " ".join(current_prompt.casefold().split()) != expected_prompt:
                    break
                current_image = await _screenshot_locator(self.page, action_target)
                if not current_image:
                    break
                current_media_type = "image/png"
                if self.options.compress_uploads:
                    current_image, current_media_type = compress_for_vision(
                        current_image,
                        max_edge=self.options.vision_max_edge,
                        quality=self.options.vision_jpeg_quality,
                    )
                temporal_frames.append(current_image)
                last_media_type = current_media_type
            temporal_image = temporal_contact_sheet(
                temporal_frames,
                columns=3,
                max_edge=self.options.temporal_sheet_max_edge,
                quality=self.options.vision_jpeg_quality,
            )
            if temporal_image:
                # 模型坐标改以最后采样帧 tN 为准；模型返回后等待动画再次经过该
                # 感知指纹相位，再立即点击，避免 12-15s 推理期间对象移动导致坐标失效。
                final_image = temporal_frames[-1]
                final_fingerprint = _image_fingerprint(final_image)
                request["image"] = final_image
                request["media_type"] = last_media_type
                request["challenge_fingerprint"] = final_fingerprint
                # 仅供模型返回后的相位对齐；调用视觉客户端前会 pop，不进入请求体。
                request["temporal_phase_image"] = final_image
                request["temporal_image"] = temporal_image
                request["temporal_media_type"] = "image/jpeg"
                self._safe_log(
                    f"时间型挑战已采样 {len(temporal_frames)} 帧，"
                    f"序列图 {len(temporal_image) / 1024:.1f}KB，坐标基准为末帧"
                )

        # 只有需要坐标的任务才需要网格；时间型题若切换为末帧主图，网格也必须基于
        # 同一末帧重新生成，保证坐标系统一致。
        request_image = request.get("image") or b""
        # drag 细节图已经包含映射到主图的刻度；再上传一张全幅网格图只增加延迟，
        # 实测会把 55s 视觉预算吃完。若细节图生成失败，仍回退到全幅网格。
        need_grid = not (hint == "drag" and request.get("drag_detail_image"))
        if request_image and hint in {"point", "drag", "unknown"} and self.options.coordinate_grid and need_grid:
            grid = coordinate_grid_overlay(request_image)
            if grid:
                if self.options.compress_uploads:
                    grid, _grid_type = compress_for_vision(
                        grid,
                        max_edge=self.options.vision_max_edge,
                        quality=self.options.vision_jpeg_quality,
                    )
                request["grid_image"] = grid
        return request

    async def _refresh_action_context(
        self,
        frame: Any,
        *,
        task_type: str,
        prompt: str,
        original_fingerprint: str,
    ) -> tuple[Any | None, Any | None, bool]:
        """模型返回后刷新 iframe/交互目标，并确认仍是请求时的同一题。"""
        _checkbox, current = await self._find_frames()
        current = current or frame
        if not await self._challenge_is_live(current):
            return current, None, False

        current_prompt = await _text(await _first_locator(current, _PROMPT_SELECTORS))
        expected_prompt = " ".join(str(prompt or "").casefold().split())
        actual_prompt = " ".join(str(current_prompt or "").casefold().split())
        if expected_prompt and actual_prompt and expected_prompt != actual_prompt:
            return current, None, False

        challenge = await _first_locator(current, _SCREENSHOT_SELECTORS)
        action_target = challenge
        if task_type in {"point", "drag"}:
            interaction = await _first_locator(current, _INTERACTION_SELECTORS)
            if interaction is not None:
                action_target = interaction
        current_image = b""
        if action_target is not None:
            current_image = await _screenshot_locator(self.page, action_target)
        if not current_image:
            body = await _first_locator(current, ("body",))
            if body is not None:
                current_image = await _screenshot_locator(self.page, body)
                action_target = body if current_image else None
        if current_image and self.options.compress_uploads:
            current_image, _media_type = compress_for_vision(
                current_image,
                max_edge=self.options.vision_max_edge,
                quality=self.options.vision_jpeg_quality,
            )

        return current, action_target, _same_image_fingerprint(
            original_fingerprint, _image_fingerprint(current_image)
        )

    async def _align_temporal_phase(
        self,
        frame: Any,
        *,
        task_type: str,
        prompt: str,
        target_image: bytes,
    ) -> tuple[Any | None, Any | None, bool]:
        """等待动画重新经过模型坐标对应的末帧相位。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.options.temporal_phase_wait_ms / 1000
        expected_prompt = " ".join(str(prompt or "").casefold().split())
        current = frame
        best_difference: float | None = None
        while True:
            _checkbox, refreshed = await self._find_frames()
            current = refreshed or current
            if not await self._challenge_is_live(current):
                return current, None, False
            current_prompt = await _text(await _first_locator(current, _PROMPT_SELECTORS))
            actual_prompt = " ".join(str(current_prompt or "").casefold().split())
            if expected_prompt and actual_prompt and expected_prompt != actual_prompt:
                return current, None, False

            challenge = await _first_locator(current, _SCREENSHOT_SELECTORS)
            action_target = challenge
            if task_type in {"point", "drag"}:
                interaction = await _first_locator(current, _INTERACTION_SELECTORS)
                if interaction is not None:
                    action_target = interaction
            current_image = (
                await _screenshot_locator(self.page, action_target)
                if action_target is not None
                else b""
            )
            if current_image and self.options.compress_uploads:
                current_image, _media_type = compress_for_vision(
                    current_image,
                    max_edge=self.options.vision_max_edge,
                    quality=self.options.vision_jpeg_quality,
                )
            difference = _image_mean_difference(target_image, current_image)
            if difference is not None:
                best_difference = (
                    difference
                    if best_difference is None
                    else min(best_difference, difference)
                )
                if difference <= 0.006:
                    self._safe_log(
                        f"时间型挑战已回到末帧相位（RGB 差 {difference:.4f}），立即执行动作"
                    )
                    return current, action_target, True
            if loop.time() >= deadline:
                if best_difference is not None:
                    self._safe_log(
                        f"时间型挑战在 {self.options.temporal_phase_wait_ms / 1000:.1f}s 内"
                        f"未回到末帧相位（最小 RGB 差 {best_difference:.4f}）"
                    )
                return current, action_target, False
            await asyncio.sleep(min(0.15, self.options.poll_interval_ms / 1000))

    def _vision_attempt_timeout_ms(self) -> int:
        """把单轮预算公平分给各次请求，保留退避、解析与动作时间。"""
        attempts = max(1, self.options.vision_max_attempts)
        usable = max(100, self.options.round_timeout_ms - self.options.vision_retry_reserve_ms)
        return max(100, usable // attempts)

    def _remaining_round_budget(self) -> float | None:
        """当前轮剩余秒数；未设置轮截止时刻时返回 None（不限制）。"""
        deadline = self._round_deadline
        if deadline is None:
            return None
        try:
            now = asyncio.get_running_loop().time()
        except RuntimeError:
            return None
        return max(0.0, deadline - now)

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
                    # 这是「每次尝试」的安全上限，不是整轮上限。若让首次请求独占
                    # 整轮预算，瞬时断连后的退避重试永远没有执行机会。
                    "timeout_cap_ms": self._vision_attempt_timeout_ms(),
                    "max_actions": 16,
                }
            )
            client.max_attempts = self.options.vision_max_attempts
        except Exception:
            return None
        self.vision_client = client
        return client

    async def _ask_vision(
        self, request: Mapping[str, Any], *, _schema_retry: bool = False
    ) -> Mapping[str, Any] | None:
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
            parameters = signature.parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
            if not accepts_kwargs:
                optional_enhancements = {
                    "grid_image",
                    "media_type",
                    "temporal_image",
                    "temporal_media_type",
                    "drag_detail_image",
                    "drag_detail_media_type",
                    "source_hint",
                }
                for key in list(payload):
                    if key in parameters:
                        continue
                    if key not in optional_enhancements:
                        # 非增强参数不应被静默吞掉；交给 bind 给出签名错误。
                        continue
                    payload.pop(key, None)
                    self._safe_log(f"视觉客户端不接受 {key}，本次调用已省略该参数")
            signature.bind(**payload)
        except (TypeError, ValueError):
            self._vision_failure = {"failure_stage": "client_signature", "error_type": "TypeError"}
            return None

        # 请求侧可观测：记录模型 id、尺寸与结构。图像字节与 API Key 永不入日志。
        image_bytes = len(payload.get("image") or b"")
        grid_bytes = len(payload.get("grid_image") or b"")
        temporal_bytes = len(payload.get("temporal_image") or b"")
        drag_detail_bytes = len(payload.get("drag_detail_image") or b"")
        prompt_text = str(payload.get("prompt") or "").strip()
        config = getattr(client, "config", None)
        model_id = str(getattr(config, "model", "") or "") or "<未知模型>"
        endpoint = str(getattr(config, "base_url", "") or "")
        request_timeout = getattr(config, "timeout", None)
        max_attempts = getattr(client, "max_attempts", None)
        self._safe_log(
            f"调用视觉模型：model={model_id}"
            + (f" endpoint={endpoint}" if endpoint else "")
            + (f" timeout={request_timeout}s" if request_timeout else "")
            + (f" attempts={max_attempts}" if max_attempts else "")
            + f"，第 {payload.get('round')} 轮，类型 {payload.get('task_type')}，"
            f"主图 {image_bytes / 1024:.1f}KB"
            + (f"，网格图 {grid_bytes / 1024:.1f}KB" if grid_bytes else "，无网格图")
            + (f"，序列图 {temporal_bytes / 1024:.1f}KB" if temporal_bytes else "")
            + (f"，drag 细节图 {drag_detail_bytes / 1024:.1f}KB" if drag_detail_bytes else "")
            + (f"，图块 {len(payload.get('tiles') or [])} 个" if payload.get("tiles") else "")
            + (
                f"，已附带 {len(payload['source_hint'])} 个检测源提示"
                if payload.get("source_hint")
                else ""
            )
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
            if model_output is not None and not _schema_retry:
                remaining = self._remaining_round_budget()
                needed = self._vision_attempt_timeout_ms() / 1000
                if remaining is not None and remaining < needed:
                    self._safe_log(
                        f"视觉 schema 校验失败，但本轮仅剩 {remaining:.1f}s（需 {needed:.1f}s），"
                        "不再重试，交由下一轮处理"
                    )
                    return None
                retry_payload = dict(payload)
                retry_prompt = str(retry_payload.get("prompt") or "").strip()
                # 追加的约束必须匹配当前题型：实测 point 题被追加了 drag 专用说明
                # （"source must be the movable object marked Move"），既无意义又会
                # 干扰模型，重试后仍给出错误坐标。
                retry_task_type = str(retry_payload.get("task_type") or "").strip().lower()
                if retry_task_type == "drag":
                    constraint = (
                        "Return exactly one usable drag action. Source must be the movable object "
                        "marked Move, target must be its matching destination, and source/target must "
                        "be different coordinates given as plain numeric x and y fields; "
                        "do not wrap coordinates in extra objects and do not return a zero-distance drag."
                    )
                elif retry_task_type == "grid":
                    constraint = (
                        "Return the tile_indices array with 1-based integer indices of every matching "
                        "tile. Do not return an empty array unless no tile matches."
                    )
                elif retry_task_type == "point":
                    constraint = (
                        "Return the points array with one entry per target, each having plain numeric "
                        "x and y fields in the 0-1000 space. If the prompt asks for two targets, return "
                        "exactly two points. Do not return an empty array or zero confidence; look again "
                        "and give your best estimate."
                    )
                else:
                    constraint = (
                        "Return exactly one usable action with plain numeric coordinates in the 0-1000 "
                        "space, and set challenge_type to grid, point, or drag accordingly."
                    )
                retry_payload["prompt"] = (
                    f"{retry_prompt} The previous JSON answer was rejected as invalid. "
                    f"Reinspect the image. {constraint}"
                ).strip()
                self._safe_log(
                    f"视觉 schema 校验失败，按 {retry_task_type or 'unknown'} 题型追加约束后重试一次"
                )
                return await self._ask_vision(retry_payload, _schema_retry=True)
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
        submit = await self._first_submit_locator(frame)
        if submit is None:
            # 某些 hCaptcha 题型在最后一个图块点击后自动提交；若只存在 Skip 或
            # 离屏旧按钮，交给后续 token/新题面等待逻辑判断，绝不主动跳题。
            self._safe_log("未发现可操作的提交按钮，等待自动提交或题面更新")
            return True
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
            clicked = False
            if not self.options.move_before_click and target is not None:
                clicked = await self._locator_click(
                    target,
                    label="point 目标",
                    position={
                        "x": box["width"] * point[0] / 1000,
                        "y": box["height"] * point[1] / 1000,
                    },
                )
            else:
                if self.options.move_before_click:
                    await self._humanized_move(x, y, steps=3)
                clicked = await self._mouse_click_at(x, y, label="point 目标")
            if not clicked:
                return False
            self._safe_log(
                f"已按刻度点击：({point[0]:.0f}, {point[1]:.0f}) → 页面 ({x:.0f}, {y:.0f})"
            )
        submit = await self._first_submit_locator(frame)
        if submit is not None:
            await self._mouse_click_locator(submit, label="提交按钮")
        else:
            self._safe_log("point 动作已完成，未发现可操作提交按钮（等待自动提交）")
        return True

    async def _apply_drag(
        self,
        frame: Any,
        answer: Mapping[str, Any],
        target: Any = None,
        source_points: Sequence[tuple[float, float]] | None = None,
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
        candidates = [point for point in (source_points or ()) if point is not None]
        if candidates and len(parsed) == 1:
            adjusted = self._align_drag_with_sources(parsed[0], candidates)
            if adjusted is None:
                return False
            parsed = [adjusted]
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
            reached_start = await self._humanized_move(
                sx,
                sy,
                steps=1,
                timeout_ms=self.options.drag_move_timeout_ms,
            )
            if not reached_start:
                self._safe_log("拖拽失败：未在预算内到达起点")
                return False
            await _maybe_await(self.page.mouse.down())
            reached_end = False
            try:
                reached_end = await self._humanized_move(
                    ex,
                    ey,
                    steps=2,
                    timeout_ms=self.options.drag_move_timeout_ms,
                )
            finally:
                await _maybe_await(self.page.mouse.up())
            if not reached_end:
                self._safe_log("拖拽失败：未在预算内到达终点")
                return False
            self._safe_log(f"已拖拽：({sx:.0f}, {sy:.0f}) → ({ex:.0f}, {ey:.0f})")

        # 真实 drag 题初始按钮是 Skip；放下对象后同一节点会切换为 Verify。
        # 旧实现拖完即返回，导致答案从未提交：下一轮截图里对象已经移动且仍带 Move，
        # 求解器便把它误当成新源继续拖，最终耗尽轮数。这里复用统一按钮筛选，等待
        # Verify 出现并点击；自动提交型题目没有按钮时仍交给后续 token/题面等待处理。
        submit = await self._first_submit_locator(frame)
        if submit is not None:
            return await self._mouse_click_locator(submit, label="提交按钮")
        self._safe_log("drag 动作已完成，未发现可操作提交按钮（等待自动提交）")
        return True

    def _align_drag_with_sources(
        self,
        drag: tuple[tuple[float, float], tuple[float, float]],
        candidates: Sequence[tuple[float, float]],
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """用像素检测出的可拖动源校正模型给出的拖拽端点。

        实测两类错误：模型把源与目标写反（起点落在 Move 元素上、终点在场景里的
        反向组合），以及起点只是"大致靠近"可拖动元素而偏出命中区。这里先按距离
        判断方向，再把起点吸附到最近的检测源。

        多源题（"Move ONE animal to the matching silhouette"）里配对本身是题目的
        核心，必须保留模型选的那个源，只做吸附；若模型起点远离所有候选，则只有在
        唯一源时才敢强制覆盖，多源时保留原样而不猜。
        """
        start, end = drag

        def _nearest(point: tuple[float, float]) -> tuple[float, tuple[float, float]]:
            best = min(
                candidates,
                key=lambda candidate: math.hypot(point[0] - candidate[0], point[1] - candidate[1]),
            )
            return math.hypot(point[0] - best[0], point[1] - best[1]), best

        start_distance, start_source = _nearest(start)
        end_distance, end_source = _nearest(end)
        if end_distance < start_distance:
            self._safe_log(
                "模型起终点方向与检测源矛盾，已按检测源交换起终点："
                f"起点 ({end[0]:.0f}, {end[1]:.0f}) → 终点 ({start[0]:.0f}, {start[1]:.0f})"
            )
            start, end = end, start
            start_distance, start_source = end_distance, end_source

        snap_radius = 150.0
        if start_distance <= snap_radius:
            if start_source != start:
                self._safe_log(
                    f"拖拽起点已吸附到检测源：模型 ({start[0]:.0f}, {start[1]:.0f}) → "
                    f"检测 ({start_source[0]:.0f}, {start_source[1]:.0f})"
                )
            start = start_source
        elif len(candidates) == 1:
            self._safe_log(
                f"模型起点远离唯一检测源，已改用检测源：模型 ({start[0]:.0f}, {start[1]:.0f}) → "
                f"检测 ({candidates[0][0]:.0f}, {candidates[0][1]:.0f})"
            )
            start = candidates[0]
        else:
            self._safe_log(
                f"模型起点 ({start[0]:.0f}, {start[1]:.0f}) 不靠近任何检测源，"
                "多源题无法判定配对，保留模型原始起点"
            )

        if math.hypot(end[0] - start[0], end[1] - start[1]) < 40:
            self._safe_log("校正后起终点距离过近，无法确定目标，本轮判为不确定")
            return None
        return start, end

    async def _apply_answer(
        self,
        frame: Any,
        answer: Mapping[str, Any],
        tile_count: int,
        action_target: Any = None,
        drag_source_points: Sequence[tuple[float, float]] | None = None,
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
                if await self._apply_drag(
                    frame, answer, target=action_target, source_points=drag_source_points
                )
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
                    # 首次题面加载复用 widget 挂载预算；实测同一站点偶尔超过 24s
                    # 才出现题面，若只给 post_action_wait_ms=12s 会误报「挑战未加载」。
                    challenge_frame = await self._wait_for_live_challenge(
                        progressed_frame or challenge_frame,
                        timeout_ms=self.options.widget_mount_timeout_ms,
                    )

                    # 首次点击可能因 iframe 在定位与点击之间被替换而静默失效：坐标点击
                    # 日志显示成功，但 aria-checked 仍为 false、题面持续空壳。长挂载窗口
                    # 后只重试一次，避免无限点击或误把正常慢加载当失败。
                    if not await self._challenge_is_live(challenge_frame):
                        retry_checkbox, retry_challenge = await self._find_frames()
                        if retry_checkbox is not None:
                            self._safe_log("首次点击后未出现题面，重新定位并重试 hCaptcha 复选框")
                            if await self._click_checkbox(retry_checkbox):
                                token, progressed_frame, passed = await self._wait_for_progress()
                                if token or passed:
                                    return await self._result(
                                        "success", "hCaptcha 已自动通过", token=token
                                    )
                                challenge_frame = await self._wait_for_live_challenge(
                                    progressed_frame or retry_challenge or challenge_frame,
                                    timeout_ms=self.options.widget_mount_timeout_ms,
                                )

                if not await self._challenge_is_live(challenge_frame):
                    return await self._result("failed", "hCaptcha 挑战未加载")
                if await self._default_vision_client() is None:
                    return await self._result("not_configured", "hCaptcha 视觉客户端未配置")

                last_challenge_type = ""
                post_action_checkbox_retries = 0
                # 只有「真正把动作作用到题面」才算一轮。恢复题面、题面被换掉而丢弃
                # 坐标这类空转不产生任何提交，若也扣轮次，就会在没答过几道题的情况下
                # 报「已达到最大求解轮数」——实测 5 轮里 3 轮是空转，只提交了 2 次。
                # 用 while 配合独立的 attempts 计数，并对空转设独立上限防止死循环。
                round_number = 0
                idle_rounds = 0
                max_idle_rounds = max(3, self.options.max_rounds)
                while round_number < self.options.max_rounds:
                    try:
                        self._round_deadline = (
                            asyncio.get_running_loop().time()
                            + self.options.round_timeout_ms / 1000
                        )
                        async with asyncio.timeout(self.options.round_timeout_ms / 1000):
                            # 每次循环都确认挑战仍在呈现：上一轮点击/模型等待期间 hCaptcha
                            # 可能已收起或换帧。不能用 round_number>0 作门槛，因为坐标被
                            # 丢弃时会回退轮次到 0，但手里的 frame 仍可能已经失效。
                            if not await self._challenge_is_live(challenge_frame):
                                self._safe_log("挑战已不在呈现状态，等待令牌或新一帧题面")
                                token, next_frame, passed = await self._wait_for_progress()
                                if token or passed:
                                    return await self._result(
                                        "success",
                                        "hCaptcha 验证成功",
                                        token=token,
                                        rounds=round_number,
                                        challenge_type=last_challenge_type,
                                    )

                                retry_checkbox, current_challenge = await self._find_frames()
                                current_challenge = current_challenge or next_frame or challenge_frame
                                checkbox_checked = (
                                    await self._checkbox_checked(retry_checkbox)
                                    if retry_checkbox is not None
                                    else None
                                )
                                if (
                                    checkbox_checked is False
                                    and post_action_checkbox_retries < 1
                                    and retry_checkbox is not None
                                ):
                                    self._safe_log(
                                        "挑战已重置为未勾选状态，重新点击一次 hCaptcha 复选框"
                                    )
                                    post_action_checkbox_retries += 1
                                    if await self._click_checkbox(retry_checkbox):
                                        token, progressed_frame, passed = await self._wait_for_progress()
                                        if token or passed:
                                            return await self._result(
                                                "success",
                                                "hCaptcha 验证成功",
                                                token=token,
                                                rounds=round_number,
                                                challenge_type=last_challenge_type,
                                            )
                                        current_challenge = (
                                            progressed_frame or current_challenge
                                        )

                                refreshed = await self._wait_for_live_challenge(
                                    current_challenge,
                                    timeout_ms=self.options.widget_mount_timeout_ms,
                                )
                                if refreshed is None:
                                    return await self._result(
                                        "timeout",
                                        "hCaptcha 上一轮动作后未出现新题面，也未签发令牌",
                                        rounds=round_number,
                                        challenge_type=last_challenge_type,
                                        failure_stage="challenge_dismissed",
                                    )
                                challenge_frame = refreshed
                                idle_rounds += 1
                                if idle_rounds > max_idle_rounds:
                                    return await self._result(
                                        "timeout",
                                        "hCaptcha 题面反复重置，未能进入可作答状态",
                                        rounds=round_number,
                                        challenge_type=last_challenge_type,
                                        failure_stage="challenge_dismissed",
                                    )
                                self._safe_log("新题面已恢复，重新开始本轮求解")
                                # 恢复题面不算一次作答：不推进 round_number。
                                continue
                            # 本轮确实进入求解（已拿到可作答题面），此时即计数：
                            # 超时/失败的 rounds 必须反映真实尝试过的轮数。空转分支
                            # 在上面就 continue 了，不会走到这里，因此不计入。
                            round_number += 1
                            request = await self._capture_round(challenge_frame, round_number)
                            action_target = request.pop("action_target", None)
                            challenge_fingerprint = str(request.pop("challenge_fingerprint", "") or "")
                            temporal_phase_image = request.pop("temporal_phase_image", b"")
                            drag_source_points = request.pop("drag_source_points", None) or []
                            if not request.get("image"):
                                return await self._result(
                                    "failed",
                                    "hCaptcha 挑战截图为空，未向模型发起调用",
                                    rounds=round_number,
                                    failure_stage="empty_capture",
                                )
                            self._vision_failure = {}
                            # 换帧过渡期的截图是近乎纯色的空壳（实测 271KB 的正常题面
                            # 对比只有 5KB 的过渡帧），模型只会回 confidence=0、空
                            # points，白费一次请求和一次 schema 重试。用像素方差判定
                            # 「几乎没有内容」，此时等新题面而不是发请求。
                            if _image_is_nearly_blank(request.get("image") or b""):
                                self._safe_log("挑战截图接近空白（疑为换帧过渡），等待新题面后重试")
                                # 没有发出请求也没有作答，回退本轮计数。
                                round_number -= 1
                                idle_rounds += 1
                                if idle_rounds > max_idle_rounds:
                                    return await self._result(
                                        "timeout",
                                        "hCaptcha 截图持续为空白过渡帧，无法作答",
                                        rounds=round_number,
                                        challenge_type=last_challenge_type,
                                        failure_stage="blank_capture",
                                    )
                                refreshed = await self._wait_for_live_challenge(
                                    challenge_frame,
                                    timeout_ms=self.options.widget_mount_timeout_ms,
                                )
                                if refreshed is not None:
                                    challenge_frame = refreshed
                                continue
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

                            # 模型请求可能历经多次重试（实测 114s），期间 hCaptcha 会
                            # 替换 iframe。先检查令牌，再重新定位当前 frame/交互目标；
                            # 只有题面与感知图像仍匹配才执行旧坐标，避免点击 detached
                            # 节点，或把上一题坐标作用到已经换出的新题。
                            current_token = (await self._read_response())[1] or self._network_token
                            if current_token or self._network_passed:
                                return await self._result(
                                    "success",
                                    "hCaptcha 验证成功",
                                    token=current_token,
                                    rounds=round_number,
                                    challenge_type=challenge_type,
                                )
                            request_task_type = str(
                                request.get("task_type") or challenge_type
                            )
                            if temporal_phase_image:
                                refreshed_frame, refreshed_target, same_challenge = (
                                    await self._align_temporal_phase(
                                        challenge_frame,
                                        task_type=request_task_type,
                                        prompt=str(request.get("prompt") or ""),
                                        target_image=bytes(temporal_phase_image),
                                    )
                                )
                            else:
                                refreshed_frame, refreshed_target, same_challenge = (
                                    await self._refresh_action_context(
                                        challenge_frame,
                                        task_type=request_task_type,
                                        prompt=str(request.get("prompt") or ""),
                                        original_fingerprint=challenge_fingerprint,
                                    )
                                )
                            if not same_challenge:
                                self._safe_log(
                                    "时间型挑战未匹配到末帧相位，丢弃旧坐标并重新求解"
                                    if temporal_phase_image
                                    else "模型返回时题面已更新，丢弃旧坐标并重新求解"
                                )
                                if refreshed_frame is not None:
                                    challenge_frame = refreshed_frame
                                # 坐标被丢弃、未提交任何动作，同样不计入作答轮次。
                                round_number -= 1
                                idle_rounds += 1
                                if idle_rounds > max_idle_rounds:
                                    return await self._result(
                                        "timeout",
                                        "hCaptcha 题面持续变化，模型坐标始终无法落到同一题",
                                        rounds=round_number,
                                        challenge_type=last_challenge_type,
                                        failure_stage="challenge_changed",
                                    )
                                continue
                            challenge_frame = refreshed_frame or challenge_frame
                            action_target = refreshed_target
                            # 已回到稳定题面并准备执行动作，连续空转链在此结束。
                            idle_rounds = 0

                            self._safe_log(
                                f"hCaptcha 第 {round_number}/{self.options.max_rounds} 轮："
                                f"模型判定 {challenge_type}，开始执行受约束动作"
                            )
                            applied = await self._apply_answer(
                                challenge_frame,
                                answer,
                                len(request["tiles"]),
                                action_target=action_target,
                                drag_source_points=drag_source_points,
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
                        # 极小预算可能在截图/探测阶段、round_number 自增前就超时；
                        # 但这仍是第 1 轮尝试，结构化结果不能错误报告 rounds=0。
                        return await self._result(
                            "timeout", "hCaptcha 单轮求解超时", rounds=max(1, round_number)
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
