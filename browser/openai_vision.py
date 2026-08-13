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
        max_actions = _config_value(secret_config, "max_actions", "maxActions")
        if max_actions in (None, ""):
            max_actions = _option(options, "max_actions")
        return cls(
            api_key=api_key or "",
            base_url=base_url or "",
            model=model or "",
            timeout=60 if timeout in (None, "") else timeout,
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
        if isinstance(raw_tiles, list):
            # 合法数组一律严格校验，与 points/drags 同一标准。
            for index in raw_tiles:
                if isinstance(index, bool) or not isinstance(index, int) or index < 1:
                    raise VisionClientError("tile_indices must contain 1-based positive integers")
                tile_indices.append(index)
        elif _relevant("grid"):
            raise VisionClientError("tile_indices must be an array")

        raw_points = value.get("points", [])
        points: list[dict[str, int | float]] = []
        if isinstance(raw_points, list):
            # 合法数组一律严格校验：越界坐标必须报错，不能静默丢成「无动作」。
            points = [_point(point, "points") for point in raw_points]
        elif _relevant("point"):
            raise VisionClientError("points must be an array")

        # elements/paths 是模型实测用过的等价键名，与 hcaptcha._validated_answer 保持一致。
        raw_drags = value.get("drags", value.get("elements", value.get("paths", [])))
        drags: list[dict[str, dict[str, int | float]]] = []
        if isinstance(raw_drags, list):
            drags = [_drag(drag) for drag in raw_drags]
        elif _relevant("drag"):
            raise VisionClientError("drags must be an array")

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

    async def analyze(
        self,
        image: bytes | bytearray | memoryview | str,
        *,
        prompt: str | None = None,
        media_type: str = "image/png",
        grid_image: bytes | bytearray | memoryview | str | None = None,
    ) -> VisionPlan:
        """Analyze an image and return a validated action plan.

        grid_image 为可选的坐标网格辅助图（与主图同尺寸、叠加 0-1000 刻度）。
        提供时会作为第二张图一并发送，模型可照刻度读数而非凭空估算坐标。
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
        payload = {
            "model": self.config.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Return only one JSON object describing the visual challenge. "
                        "Use challenge_type grid, point, drag, or unknown; confidence from 0 to 1; "
                        "1-based tile_indices; points with x/y; and drags with start/end points. "
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
        media_type: str = "image/png",
        **_ignored: Any,
    ) -> VisionPlan:
        """Adapt an hCaptcha round request to the generic vision endpoint."""

        details = [prompt.strip()] if prompt.strip() else []
        if task_type:
            details.append(f"Challenge type hint: {task_type}.")
        if isinstance(tiles, list) and tiles:
            details.append(f"The displayed grid has {len(tiles)} numbered tiles.")
        if round is not None:
            details.append(f"This is challenge round {round}.")
        return await self.analyze(
            image,
            prompt=" ".join(details) or None,
            grid_image=grid_image,
            media_type=media_type or "image/png",
        )

    async def _request(self, payload: dict[str, Any]) -> Any:
        try:
            return await self._send(payload)
        except Exception as exc:
            status = _status_of(exc)
            if status == 400 and "response_format" in payload:
                fallback = dict(payload)
                fallback.pop("response_format", None)
                try:
                    return await self._send(fallback)
                except Exception as retry_exc:
                    raise self._safe_transport_error(retry_exc) from None
            raise self._safe_transport_error(exc) from None

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


def _point(value: Any, label: str) -> dict[str, int | float]:
    # 允许 [x, y] 与再包一层的 {"point": [x, y]}：实测 mimo-v2.5 返回
    # {"start": {"point": [850, 390]}}，不解包会把正确答案判为无效。
    if isinstance(value, Mapping):
        for key in ("point", "coordinates", "coordinate", "position", "pos"):
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
        start = value.get("start", value.get("from"))
        end = value.get("end", value.get("to"))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise VisionClientError("drags entries must contain start and end points")
    return {"start": _point(start, "drags.start"), "end": _point(end, "drags.end")}


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
