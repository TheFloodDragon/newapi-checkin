#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small, dependency-free OpenAI-compatible vision client.

The module deliberately contains no provider-specific SDK code.  It builds a
``chat/completions`` request and delegates the blocking HTTP operation to the
repository's standard-library transport in :mod:`providers.base`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import providers.base as provider_base

_DEFAULT_BASE_URL = ""
_DEFAULT_MODEL = ""
_LOCAL_CONFIG_PATH = Path(__file__).resolve().parent.parent / "HCAPTCHA_VISION_CONFIG.json"
_ALLOWED_CHALLENGE_TYPES = frozenset({"grid", "point", "drag", "unknown"})
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)

Transport = Callable[..., Any]


class VisionClientError(RuntimeError):
    """A configuration, transport, response, or validation error."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        model_output: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        # 校验失败时携带模型实际返回，便于调用方完整记录（不含密钥）。
        self.model_output = model_output


@dataclass(frozen=True, slots=True)
class VisionClientConfig:
    """Configuration for an OpenAI-compatible vision endpoint.

    ``api_key`` is intentionally excluded from ``repr``.  Prefer
    :meth:`from_options`, which only reads credentials from the environment.
    """

    api_key: str = field(repr=False)
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL
    timeout: float = 60.0
    max_actions: int = 16

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise VisionClientError("OpenAI API key is not configured")
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise VisionClientError("OpenAI base URL is not configured")
        if not isinstance(self.model, str) or not self.model.strip():
            raise VisionClientError("OpenAI model is not configured")
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or self.timeout <= 0
        ):
            raise VisionClientError("Vision request timeout must be a positive number")
        if isinstance(self.max_actions, bool) or not isinstance(self.max_actions, int) or self.max_actions <= 0:
            raise VisionClientError("Vision max_actions must be a positive integer")

    @classmethod
    def from_options(
        cls,
        options: Mapping[str, Any] | object | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> VisionClientConfig:
        """Resolve configuration without ever accepting an option-supplied key.

        ``HCAPTCHA_VISION_CONFIG`` is a JSON Secret and has highest precedence.
        It accepts snake_case, camelCase, and the legacy environment-style names.
        Missing values fall back to non-empty options, dedicated ``HCAPTCHA_*``
        variables, and finally generic ``OPENAI_*`` variables.  An
        ``options.api_key`` value is always ignored.
        """

        env = os.environ if environ is None else environ
        # 根目录本地文件用于桌面/本机运行，已由 .gitignore 排除。按用户要求它的
        # 优先级最高；CI/无文件环境继续使用 HCAPTCHA_VISION_CONFIG Secret。
        # 显式传 environ 的调用用于测试/隔离配置，不能被开发机本地文件污染；
        # 正常运行不传 environ，此时根目录文件才作为最高优先级来源。
        local_config = _read_local_config(_LOCAL_CONFIG_PATH) if environ is None else {}
        env_config = _read_json_config(env.get("HCAPTCHA_VISION_CONFIG"))
        secret_config = {**env_config, **local_config}
        api_key = _first_nonempty(
            _config_value(
                secret_config,
                "api_key",
                "apiKey",
                "HCAPTCHA_OPENAI_API_KEY",
                "OPENAI_API_KEY",
            ),
            env.get("HCAPTCHA_OPENAI_API_KEY"),
            env.get("OPENAI_API_KEY"),
        )
        base_url = _first_nonempty(
            _config_value(
                secret_config,
                "base_url",
                "baseUrl",
                "HCAPTCHA_OPENAI_BASE_URL",
                "OPENAI_BASE_URL",
            ),
            _option(options, "base_url"),
            env.get("HCAPTCHA_OPENAI_BASE_URL"),
            env.get("OPENAI_BASE_URL"),
        )
        model = _first_nonempty(
            _config_value(
                secret_config,
                "model",
                "HCAPTCHA_OPENAI_MODEL",
                "OPENAI_MODEL",
            ),
            _option(options, "model"),
            env.get("HCAPTCHA_OPENAI_MODEL"),
            env.get("OPENAI_MODEL"),
        )
        timeout = _config_value(secret_config, "timeout")
        timeout_ms = _config_value(secret_config, "timeout_ms", "timeoutMs")
        if timeout in (None, ""):
            timeout = _option(options, "timeout")
        if timeout in (None, "") and timeout_ms in (None, ""):
            timeout_ms = _option(options, "timeout_ms")
        if timeout in (None, "") and timeout_ms not in (None, ""):
            if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
                raise VisionClientError("Vision request timeout_ms must be a positive number")
            timeout = max(0.1, float(timeout_ms) / 1000)
        resolved_timeout = 60 if timeout in (None, "") else timeout
        # timeout_cap_ms 是调用方给出的安全上限，不参与普通配置优先级：本地文件 /
        # Secret 仍决定常规超时，但浏览器求解器可确保单次阻塞请求不超过当前轮预算。
        # 实测根配置 timeout_ms=240000 覆盖了求解器 75s 轮预算；协程取消后 _send
        # 又会等待 to_thread 自然结束，导致单任务实际挂满 900s。
        timeout_cap_ms = _option(options, "timeout_cap_ms")
        if timeout_cap_ms not in (None, ""):
            if (
                isinstance(timeout_cap_ms, bool)
                or not isinstance(timeout_cap_ms, (int, float))
                or not math.isfinite(float(timeout_cap_ms))
                or float(timeout_cap_ms) <= 0
            ):
                raise VisionClientError("Vision timeout_cap_ms must be a positive number")
            resolved_timeout = min(float(resolved_timeout), max(0.1, float(timeout_cap_ms) / 1000))

        max_actions = _config_value(secret_config, "max_actions", "maxActions")
        if max_actions in (None, ""):
            max_actions = _option(options, "max_actions")
        return cls(
            api_key=api_key or "",
            base_url=base_url or "",
            model=model or "",
            timeout=resolved_timeout,
            max_actions=16 if max_actions in (None, "") else max_actions,
        )

    from_env = from_options


@dataclass(slots=True)
class VisionPlan:
    """Validated, normalized actions returned by the vision model."""

    challenge_type: str
    confidence: float
    tile_indices: list[int] = field(default_factory=list)
    points: list[dict[str, int | float]] = field(default_factory=list)
    drags: list[dict[str, dict[str, int | float]]] = field(default_factory=list)
    # 模型未经规范化的原始返回，仅用于日志与排障；不参与任何动作判定。
    raw_output: Mapping[str, Any] | None = field(default=None, repr=False)

    def model_dump(self) -> dict[str, Any]:
        """Return the action shape consumed by the browser hCaptcha solver."""

        result: dict[str, Any] = {
            "challenge_type": self.challenge_type,
            "type": self.challenge_type,
            "confidence": self.confidence,
            "tile_indices": list(self.tile_indices),
            "points": [dict(point) for point in self.points],
            "drags": [
                {"start": dict(drag["start"]), "end": dict(drag["end"])}
                for drag in self.drags
            ],
        }
        if self.challenge_type == "grid":
            result["actions"] = list(self.tile_indices)
        elif self.challenge_type == "point":
            result["actions"] = [dict(point) for point in self.points]
        elif self.challenge_type == "drag" and self.drags:
            result["action"] = dict(result["drags"][0])
        return result

    def dict(self) -> dict[str, Any]:
        """Compatibility alias for consumers using a Pydantic-style API."""

        return self.model_dump()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, max_actions: int = 16) -> VisionPlan:
        challenge_type = value.get("challenge_type")
        if challenge_type not in _ALLOWED_CHALLENGE_TYPES:
            allowed = ", ".join(sorted(_ALLOWED_CHALLENGE_TYPES))
            raise VisionClientError(f"Invalid challenge_type; expected one of: {allowed}")

        confidence = _number(value.get("confidence"), "confidence")
        if not 0 <= confidence <= 1:
            raise VisionClientError("confidence must be between 0 and 1")

        # 与本次挑战类型无关、且结构本身就不是动作数组的字段，不得否决一个有效计划：
        # 实测模型在 drag 计划里附带了垃圾 tile_indices（{"source": [...],
        # "target": [...]}），旧实现因此报 "tile_indices must be an array"
        # 丢掉了正确的 drags。
        #
        # 但「模型确实填了合法数组、只是值越界」必须照旧严格报错——那是真实的坐标
        # 错误，静默丢弃会让越界答案变成「没有动作」，掩盖问题。因此只对「非数组」
        # 的无关字段宽容。
        def _relevant(name: str) -> bool:
            return challenge_type in ("unknown", name)

        raw_tiles = value.get("tile_indices", [])
        tile_indices: list[int] = []
        tile_error = ""
        if isinstance(raw_tiles, list):
            for index in raw_tiles:
                if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                    tile_error = "tile_indices must contain 1-based positive integers"
                    tile_indices = []
                    break
                tile_indices.append(index)
        elif _relevant("grid"):
            tile_error = "tile_indices must be an array"

        raw_points = value.get("points")
        if raw_points is None:
            raw_points = value.get("point")
        if raw_points is None and challenge_type == "point":
            # 2026-08-14 真实端点返回 target:{x,y} 表示 point 目标。
            # 仅在明确 point 类型下采用，避免与 drag 的 target 语义混淆。
            raw_points = value.get("target")
        if raw_points is None:
            raw_points = []
        # 2026-08-14 真实端点返回 point:{x,y}（单数对象）；语义无歧义，统一包装
        # 成 points 数组。列表仍按原规则严格校验，越界坐标绝不静默丢弃。
        if isinstance(raw_points, Mapping):
            raw_points = [raw_points]
        points: list[dict[str, int | float]] = []
        if isinstance(raw_points, list):
            points = [_point(point, "points") for point in raw_points]
        elif _relevant("point"):
            raise VisionClientError("points must be an array or point object")

        # elements/paths 是模型实测用过的等价键名；另有顶层
        # source_point/target_point（2026-08-14 真实返回），统一包装成单次 drag。
        raw_drags = value.get(
            "drags", value.get("drag", value.get("elements", value.get("paths")))
        )
        if isinstance(raw_drags, Mapping):
            raw_drags = [raw_drags]
        if raw_drags is None and ("source_point" in value or "target_point" in value):
            raw_drags = [
                {"start": value.get("source_point"), "end": value.get("target_point")}
            ]
        if raw_drags is None and ("drag_start" in value or "drag_end" in value):
            # 2026-08-15 实测别名：顶层 drag_start / drag_end。
            raw_drags = [{"start": value.get("drag_start"), "end": value.get("drag_end")}]
        if raw_drags is None and challenge_type in {"drag", "unknown"} and (
            "source" in value or "target" in value
        ):
            # 2026-08-15 真实视觉端点返回了顶层 source/target；仅对明确的
            # drag/unknown 计划采用，避免覆盖 point 题的 target:{x,y} 语义。
            raw_drags = [{"start": value.get("source"), "end": value.get("target")}]
        if (
            raw_drags is None
            and challenge_type in {"drag", "unknown"}
            and ("start" in value or "from" in value)
            and ("end" in value or "to" in value)
        ):
            # 2026-08-15 实测：模型把 start/end 直接放在顶层而不是 drags 数组里。
            raw_drags = [value]
        if raw_drags is None:
            raw_drags = []
        drags: list[dict[str, dict[str, int | float]]] = []
        if isinstance(raw_drags, list):
            drags = [_drag(drag) for drag in raw_drags]
        elif _relevant("drag"):
            raise VisionClientError("drags must be an array")

        # 实测 drag 返回会同时附带 points=[end,start]，只是起终点的重复副本。
        # 只有两边坐标集合完全一一对应时才忽略；任何额外/不同 point 仍作为矛盾拒绝。
        if challenge_type in {"drag", "unknown"} and points and drags:
            point_coords = sorted((float(point["x"]), float(point["y"])) for point in points)
            drag_coords = sorted(
                (float(point["x"]), float(point["y"]))
                for drag in drags
                for point in (drag["start"], drag["end"])
            )
            if point_coords == drag_coords:
                points = []

        action_count = len(tile_indices) + len(points) + len(drags)
        if action_count > max_actions:
            raise VisionClientError(f"Vision plan has {action_count} actions; maximum is {max_actions}")
        expected_nonempty = {
            "grid": bool(tile_indices),
            "point": bool(points),
            "drag": bool(drags),
            "unknown": action_count == 0,
        }
        # 模型常把类型填成 unknown 却给出明确且唯一的动作（实测 mimo-v2.5 对
        # "click the TWO crosses" 返回 challenge_type=unknown + 两个正确 points，
        # confidence=1）。此时动作本身已无歧义，按动作反推类型即可，直接拒绝等于
        # 丢掉一个正确答案。仅在「恰好一种动作非空」时推断，多种并存仍视为矛盾。
        if challenge_type == "unknown" and action_count:
            present = [
                name
                for name, values in (("grid", tile_indices), ("point", points), ("drag", drags))
                if values
            ]
            if len(present) == 1:
                challenge_type = present[0]
        if tile_error and challenge_type not in {"point", "drag"}:
            raise VisionClientError(tile_error)
        if not expected_nonempty.get(challenge_type, bool(action_count)):
            raise VisionClientError(f"{challenge_type} plan has no matching actions")
        if challenge_type != "grid" and tile_indices:
            raise VisionClientError("tile_indices are only valid for grid plans")
        if challenge_type != "point" and points:
            raise VisionClientError("points are only valid for point plans")
        if challenge_type != "drag" and drags:
            raise VisionClientError("drags are only valid for drag plans")

        return cls(
            challenge_type=challenge_type,
            confidence=confidence,
            tile_indices=tile_indices,
            points=points,
            drags=drags,
        )


class OpenAIVisionClient:
    """Async OpenAI-compatible vision client with an injectable transport."""

    def __init__(
        self,
        config: VisionClientConfig | None = None,
        *,
        options: Mapping[str, Any] | object | None = None,
        model: str | None = None,
        base_url: str | None = None,
        transport: Transport | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        if config is not None and any(value is not None for value in (options, model, base_url)):
            raise VisionClientError("Pass either config or client options, not both")
        if config is None:
            resolved_options = dict(options) if isinstance(options, Mapping) else options
            if model is not None or base_url is not None:
                values = {
                    "model": model if model is not None else _option(options, "model"),
                    "base_url": base_url if base_url is not None else _option(options, "base_url"),
                    "timeout": _option(options, "timeout"),
                    "max_actions": _option(options, "max_actions"),
                }
                resolved_options = values
            config = VisionClientConfig.from_options(resolved_options, environ=environ)
        self.config = config
        self._transport = transport or provider_base.http_request
        # 瞬时网关故障（424/429/5xx）的最大尝试次数。视觉分析是纯读操作，重放安全。
        self.max_attempts = 3

    async def analyze(
        self,
        image: bytes | bytearray | memoryview | str,
        *,
        prompt: str | None = None,
        media_type: str = "image/png",
        grid_image: bytes | bytearray | memoryview | str | None = None,
        temporal_image: bytes | bytearray | memoryview | str | None = None,
        temporal_media_type: str = "image/jpeg",
        drag_detail_image: bytes | bytearray | memoryview | str | None = None,
        drag_detail_media_type: str = "image/jpeg",
    ) -> VisionPlan:
        """Analyze an image and return a validated action plan.

        grid_image 为可选的坐标网格辅助图（与主图同尺寸、叠加 0-1000 刻度）。
        drag_detail_image 为 drag 题的源侧/场景放大图，刻度仍映射到主图坐标。
        temporal_image 为同一题面的连续帧序列图，用于 grows / jumps 等时间型题。
        坐标始终以首张主图的 0-1000 空间为准，不使用任何辅助拼图整体坐标。
        """

        image_url = _image_url(image, media_type)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": prompt or "Determine the correct actions for this challenge."},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
        if grid_image:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "The second image is the same challenge with a 0-1000 coordinate grid "
                        "overlaid. Read the axis ticks to report exact coordinates."
                    ),
                }
            )
            content.append({"type": "image_url", "image_url": {"url": _image_url(grid_image, media_type)}})
        if drag_detail_image:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "An additional image enlarges the movable SOURCE side and the DESTINATION "
                        "SCENE for this drag challenge. Compare the source outline and internal edges "
                        "with every candidate before choosing the matching destination. The red ring "
                        "marks an exact source point. Tick labels in the destination panel are the "
                        "original first-image 0-1000 coordinates. Report start/end in that original "
                        "coordinate space, never in the detail-sheet pixel space, and place the end at "
                        "the geometric center of the matching outline or gap."
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _image_url(
                            drag_detail_image,
                            drag_detail_media_type or "image/jpeg",
                        )
                    },
                }
            )
        if temporal_image:
            content.append(
                {
                    "type": "text",
                    "text": (
                        "An additional image is a temporal contact sheet of the same scene, "
                        "ordered left-to-right and top-to-bottom as t0, t1, ... tN. Compare panels "
                        "to identify what grows, moves, changes, or jumps highest. The first main "
                        "image is the same scene at the final tN panel. Report the target location "
                        "using the 0-1000 coordinates of that main/final image, never the full "
                        "contact-sheet coordinates."
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _image_url(temporal_image, temporal_media_type or "image/jpeg")
                    },
                }
            )
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only one JSON object describing the visual challenge. "
                        "Use challenge_type grid, point, drag, or unknown; confidence from 0 to 1; "
                        "1-based tile_indices; points with x/y; and drags with start/end points. "
                        "For a drag, start is the movable object and end is its matching destination; "
                        "start and end must be different coordinates. If you use top-level source/target "
                        "aliases, source is the movable object and target is the destination. "
                        "All coordinates are normalized integers or numbers from 0 through 1000."
                    ),
                },
                {"role": "user", "content": content},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        response = await self._request(payload)
        content = _response_content(response)
        parsed = parse_json_object(content)
        try:
            plan = VisionPlan.from_mapping(parsed, max_actions=self.config.max_actions)
        except VisionClientError as exc:
            # 校验失败与传输失败必须可区分：把模型实际返回附在异常上，调用方才能
            # 记录「模型判成什么、缺什么动作」，而不是笼统的「未返回结构化结果」。
            exc.model_output = parsed
            raise
        # 成功路径同样保留原文：规范化会丢掉模型的附加字段（如解释性 note），
        # 而这些字段在判断「模型为何这样答」时往往是唯一线索。
        plan.raw_output = parsed
        return plan

    async def plan(
        self,
        image: bytes | bytearray | memoryview | str,
        *,
        prompt: str | None = None,
        media_type: str = "image/png",
    ) -> VisionPlan:
        """Alias for :meth:`analyze`."""

        return await self.analyze(image, prompt=prompt, media_type=media_type)

    async def solve_hcaptcha(
        self,
        *,
        image: bytes | bytearray | memoryview | str,
        prompt: str = "",
        task_type: str = "unknown",
        tiles: Any = None,
        round: int | None = None,
        grid_image: bytes | bytearray | memoryview | str | None = None,
        temporal_image: bytes | bytearray | memoryview | str | None = None,
        temporal_media_type: str = "image/jpeg",
        drag_detail_image: bytes | bytearray | memoryview | str | None = None,
        drag_detail_media_type: str = "image/jpeg",
        media_type: str = "image/png",
        source_hint: Any = None,
        **_ignored: Any,
    ) -> VisionPlan:
        """Adapt an hCaptcha round request to the generic vision endpoint."""

        details = [prompt.strip()] if prompt.strip() else []
        if task_type:
            details.append(f"Challenge type hint: {task_type}.")
        if task_type == "drag":
            # 真实 hCaptcha drag 题的可拖动源会标注 "Move"；目标是场景中与其
            # 外观/语义匹配的对象或缺口。旧提示未说明这一规则，模型常把背景装饰、
            # 目标本身或角色底座当成起点，连续答错导致挑战不断加题。
            details.append(
                "For drag challenges, the source is the movable object explicitly marked 'Move'. "
                "Drag the center of that source to the center of the matching object or gap in the scene. "
                "Do not choose background decorations or the target object as the source."
            )
        hint_points = _hint_points(source_hint)
        if len(hint_points) == 1:
            # 源已由本地像素检测确定，模型只需判断目标；实测模型自行找源的错误率高。
            details.append(
                f"The movable source object is already located at x={hint_points[0][0]:.0f}, "
                f"y={hint_points[0][1]:.0f} in the 0-1000 coordinate space. Use exactly that point as "
                "the drag start, and spend your analysis on the destination: report it as the drag "
                "end, which must be a clearly different location."
            )
        elif hint_points:
            listed = "; ".join(f"({point[0]:.0f}, {point[1]:.0f})" for point in hint_points)
            details.append(
                "The movable source objects have already been located at these 0-1000 coordinates: "
                f"{listed}. Exactly one of them belongs with one destination in the scene. Choose the "
                "matching pair, use that source coordinate verbatim as the drag start, and report its "
                "destination as the drag end."
            )
        if hint_points:
            mean_source_x = sum(point[0] for point in hint_points) / len(hint_points)
            if mean_source_x < 400:
                details.append(
                    "All movable sources are in the left source column. The destination scene is on "
                    "the opposite right side, so drag end.x must be at least 280. Never return a point "
                    "inside a source card."
                )
            elif mean_source_x > 600:
                details.append(
                    "All movable sources are in the right source column. The destination scene is on "
                    "the opposite left side, so drag end.x must be at most 720. Never return a point "
                    "inside a source card."
                )
        if isinstance(tiles, list) and tiles:
            details.append(f"The displayed grid has {len(tiles)} numbered tiles.")
        if round is not None:
            details.append(f"This is challenge round {round}.")
        return await self.analyze(
            image,
            prompt=" ".join(details) or None,
            grid_image=grid_image,
            temporal_image=temporal_image,
            temporal_media_type=temporal_media_type or "image/jpeg",
            drag_detail_image=drag_detail_image,
            drag_detail_media_type=drag_detail_media_type or "image/jpeg",
            media_type=media_type or "image/png",
        )

    # 网关侧的瞬时故障码。424 是本仓库实测最常见的一种（上游账号短暂不可用，
    # 报 "Upstream service temporarily unavailable"），紧接着重试往往就成功；
    # 429/5xx 同属瞬时。视觉分析是纯读操作，重放没有副作用。
    _TRANSIENT_STATUSES = frozenset({424, 425, 429, 500, 502, 503, 504})

    async def _request(self, payload: dict[str, Any]) -> Any:
        attempts = max(1, int(self.max_attempts))
        last: BaseException | None = None
        for attempt in range(attempts):
            try:
                return await self._send(payload)
            except Exception as exc:
                last = exc
                status = _status_of(exc)
                if status == 400 and "response_format" in payload:
                    fallback = dict(payload)
                    fallback.pop("response_format", None)
                    try:
                        return await self._send(fallback)
                    except Exception as retry_exc:
                        raise self._safe_transport_error(retry_exc) from None
                # 底层 http_request 会把连接中断、读取超时等无 HTTP 状态的网络错误
                # 标为 transient=True。旧逻辑只看 status，导致
                # "Remote end closed connection without response"（status=None）
                # 单次即放弃；解析/schema 异常没有 transient 标记，仍不会误重试。
                retryable = status in self._TRANSIENT_STATUSES or bool(
                    getattr(exc, "transient", False)
                )
                if not retryable or attempt >= attempts - 1:
                    raise self._safe_transport_error(exc) from None
                # 退避 + 抖动：上游刚被打满时立刻重试只会再撞一次。
                delay = min(4.0, 0.6 * (2**attempt))
                await asyncio.sleep(delay + random.uniform(0, delay * 0.25))
        raise self._safe_transport_error(last or RuntimeError("vision request failed"))

    async def _send(self, payload: dict[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        task = asyncio.create_task(
            asyncio.to_thread(
                self._transport,
                _chat_completions_url(self.config.base_url),
                method="POST",
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                body=body,
                timeout=self.config.timeout,
                max_attempts=1,
            )
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # asyncio.to_thread 无法中断已启动的阻塞线程。等待单次请求按自身超时结束，
            # 保证本协程返回后不会留下后台请求继续运行或产生延迟计费。
            try:
                await task
            except Exception:
                pass
            raise

    def _safe_transport_error(self, exc: BaseException) -> VisionClientError:
        """把传输异常转成不含密钥的 VisionClientError，但保留服务端诊断信息。

        服务端正文常是唯一能说明原因的线索（如「Upstream access forbidden」与
        「Service temporarily unavailable」对应完全不同的处置）。之前一律丢弃，
        排障时只能看到「request failed」。这里附带正文摘要，调用方输出前会再过
        一次 mask_secrets；请求头与 api_key 从不出现在异常里。
        """
        status = _status_of(exc)
        suffix = f" (HTTP {status})" if status is not None else ""
        detail = ""
        payload = getattr(exc, "payload", None)
        if payload is not None:
            try:
                rendered = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            except Exception:
                rendered = str(payload)
            rendered = " ".join(str(rendered).split())
            if rendered:
                detail = f": {rendered[:400]}"
        if not detail:
            message = " ".join(str(getattr(exc, "message", "") or "").split())
            if message:
                detail = f": {message[:400]}"
        # 服务端可能把请求里的 key 原样回显在错误正文里，必须在构造异常时就脱敏：
        # 异常文本会流向日志、结果 detail 与用户界面，不能依赖每个调用方各自处理。
        if detail:
            try:
                from mask_utils import mask_secrets

                detail = mask_secrets(detail)
            except Exception:
                detail = ""
            if self.config.api_key and self.config.api_key in detail:
                detail = detail.replace(self.config.api_key, "***")
        return VisionClientError(f"OpenAI vision request failed{suffix}{detail}", status=status)


# A concise public name, while retaining the explicit provider name for callers.
VisionClient = OpenAIVisionClient


def create_client(
    *,
    model: str | None = None,
    base_url: str | None = None,
    transport: Transport | None = None,
    environ: Mapping[str, str] | None = None,
) -> OpenAIVisionClient:
    """Create a client using option/environment precedence."""

    return OpenAIVisionClient(
        model=model,
        base_url=base_url,
        transport=transport,
        environ=environ,
    )


def parse_json_object(content: str | Mapping[str, Any]) -> dict[str, Any]:
    """Parse direct JSON, fenced JSON, or the first balanced JSON object."""

    if isinstance(content, Mapping):
        return dict(content)
    if not isinstance(content, str):
        raise VisionClientError("Vision response content must be text or an object")

    stripped = content.strip()
    direct = _load_object(stripped)
    if direct is not None:
        return direct

    for match in _FENCE_RE.finditer(stripped):
        fenced = _load_object(match.group(1).strip())
        if fenced is not None:
            return fenced

    balanced = _first_balanced_object(stripped)
    if balanced is not None:
        parsed = _load_object(balanced)
        if parsed is not None:
            return parsed
    raise VisionClientError("Vision response did not contain a valid JSON object")


def _read_local_config(path: Path) -> dict[str, Any]:
    """读取根目录本地视觉配置；错误信息不包含密钥或文件正文。"""
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise VisionClientError(f"Local hCaptcha vision config could not be read: {type(exc).__name__}") from exc
    return _read_json_config(raw)


def _read_json_config(raw: Any) -> dict[str, Any]:
    """Parse the single JSON Secret without exposing its value in errors."""

    if raw in (None, ""):
        return {}
    if not isinstance(raw, str):
        raise VisionClientError("HCAPTCHA_VISION_CONFIG must be a JSON object")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise VisionClientError("HCAPTCHA_VISION_CONFIG must be valid JSON") from exc
    if not isinstance(value, dict):
        raise VisionClientError("HCAPTCHA_VISION_CONFIG must be a JSON object")
    return value


def _config_value(config: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        value = config.get(name)
        if value not in (None, ""):
            return value
    return None


def _first_nonempty(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _option(options: Mapping[str, Any] | object | None, name: str) -> Any:
    if options is None:
        return None
    if isinstance(options, Mapping):
        return options.get(name)
    return getattr(options, name, None)


def _number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise VisionClientError(f"{label} must be a finite number")
    return value


def _hint_point(value: Any) -> tuple[float, float] | None:
    """把调用方给出的源坐标提示收敛成 0-1000 内的 (x, y)；无效则忽略。

    提示只影响 prompt 文案，不参与动作校验，因此这里静默忽略脏数据而不抛错。
    """
    if isinstance(value, Mapping):
        raw = (value.get("x"), value.get("y"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        raw = (value[0], value[1])
    else:
        return None
    coords: list[float] = []
    for item in raw:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if not math.isfinite(number) or not 0 <= number <= 1000:
            return None
        coords.append(number)
    return coords[0], coords[1]


def _hint_points(value: Any) -> list[tuple[float, float]]:
    """接受单个坐标或坐标序列，返回全部有效的 0-1000 提示点。"""
    single = _hint_point(value)
    if single is not None:
        return [single]
    if isinstance(value, (list, tuple)):
        points = [_hint_point(item) for item in value]
        return [point for point in points if point is not None]
    return []


def _point(value: Any, label: str) -> dict[str, int | float]:
    # 允许 [x, y] 与再包一层的 {"point": [x, y]}：实测 mimo-v2.5 返回
    # {"start": {"point": [850, 390]}}，不解包会把正确答案判为无效。
    # 2026-08-15 又实测到 {"source": {"center": [800, 350]}}，同属包装层。
    if isinstance(value, Mapping):
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
                return _point(value.get(key), label)
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        value = {"x": value[0], "y": value[1]}
    if not isinstance(value, Mapping):
        raise VisionClientError(f"{label} entries must be objects with x and y")
    x = _number(value.get("x"), f"{label}.x")
    y = _number(value.get("y"), f"{label}.y")
    if not 0 <= x <= 1000 or not 0 <= y <= 1000:
        raise VisionClientError(f"{label} coordinates must be between 0 and 1000")
    return {"x": x, "y": y}


def _drag(value: Any) -> dict[str, dict[str, int | float]]:
    if isinstance(value, Mapping):
        # 实测 mimo-v2.5 会返回 start_point / end_point；它们与 start / end
        # 语义完全相同，不能让一个置信度 1 的有效拖拽答案因键名差异被丢弃。
        start = value.get(
            "start",
            value.get(
                "from", value.get("start_point", value.get("source", value.get("drag_start")))
            ),
        )
        end = value.get(
            "end",
            value.get("to", value.get("end_point", value.get("target", value.get("drag_end")))),
        )
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise VisionClientError("drags entries must contain start and end points")
    normalized_start = _point(start, "drags.start")
    normalized_end = _point(end, "drags.end")
    if normalized_start == normalized_end:
        raise VisionClientError("drag start and end points must differ")
    return {"start": normalized_start, "end": normalized_end}


def _image_url(image: bytes | bytearray | memoryview | str, media_type: str) -> str:
    if isinstance(image, str):
        if not image.strip():
            raise VisionClientError("Vision image must not be empty")
        if image.startswith(("data:", "http://", "https://")):
            return image
        encoded = image.strip()
    elif isinstance(image, (bytes, bytearray, memoryview)):
        if not image:
            raise VisionClientError("Vision image must not be empty")
        encoded = base64.b64encode(bytes(image)).decode("ascii")
    else:
        raise VisionClientError("Vision image must be bytes, base64 text, or a URL")
    if not isinstance(media_type, str) or not media_type.startswith("image/"):
        raise VisionClientError("media_type must be an image MIME type")
    return f"data:{media_type};base64,{encoded}"


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def _status_of(exc: BaseException) -> int | None:
    status = getattr(exc, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _response_content(response: Any) -> str | Mapping[str, Any]:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise VisionClientError("OpenAI vision response is missing message content") from exc
    if isinstance(content, list):
        text_parts = [part.get("text", "") for part in content if isinstance(part, Mapping) and part.get("type") == "text"]
        content = "".join(part for part in text_parts if isinstance(part, str))
    if not isinstance(content, (str, Mapping)):
        raise VisionClientError("OpenAI vision message content has an unsupported shape")
    return content


def _load_object(candidate: str) -> dict[str, Any] | None:
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _first_balanced_object(text: str) -> str | None:
    start = -1
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if start < 0:
            if char == "{":
                start = index
                depth = 1
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


__all__ = [
    "OpenAIVisionClient",
    "VisionClient",
    "VisionClientConfig",
    "VisionClientError",
    "VisionPlan",
    "create_client",
    "parse_json_object",
]
