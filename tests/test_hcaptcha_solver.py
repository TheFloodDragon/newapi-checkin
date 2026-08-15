# -*- coding: utf-8 -*-
"""hCaptcha 核心离线测试：仅使用 Fake Page/Frame/Locator/Mouse。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from browser.hcaptcha import (
    HCaptchaOptions,
    HCaptchaSolveResult,
    HCaptchaSolver,
    _image_fingerprint,
    _is_temporal_prompt,
    _task_type_from_prompt,
    _validated_answer,
    compress_for_vision,
    coordinate_grid_overlay,
    detect_move_source,
    detect_move_sources,
    drag_detail_sheet,
    temporal_contact_sheet,
    solve,
)
from browser.openai_vision import VisionClientError


class FakeLocator:
    def __init__(
        self,
        items: list[FakeLocator] | None = None,
        *,
        text: str = "",
        box: dict[str, float] | None = None,
        image: bytes = b"png",
    ) -> None:
        self.items = items
        self.text = text
        self.box = box or {"x": 10, "y": 20, "width": 100, "height": 80}
        self.image = image
        self.screenshots = 0
        self.visible = True

    @property
    def first(self) -> FakeLocator:
        return self.nth(0)

    def nth(self, index: int) -> FakeLocator:
        if self.items is None:
            return self
        return self.items[index]

    async def count(self) -> int:
        return len(self.items) if self.items is not None else 1

    async def inner_text(self) -> str:
        return self.text

    async def text_content(self) -> str:
        return self.text

    async def bounding_box(self) -> dict[str, float]:
        return dict(self.box)

    async def is_visible(self) -> bool:
        return self.visible

    async def screenshot(self, **_kwargs: Any) -> bytes:
        self.screenshots += 1
        return self.image


class EmptyLocator(FakeLocator):
    def __init__(self) -> None:
        super().__init__(items=[])


class FakeFrame:
    def __init__(
        self,
        url: str,
        selectors: dict[str, FakeLocator] | None = None,
        children: list[FakeFrame] | None = None,
    ) -> None:
        self.url = url
        self.selectors = selectors or {}
        self.child_frames = children or []
        self.evaluations: list[tuple[str, Any]] = []

    def locator(self, selector: str) -> FakeLocator:
        return self.selectors.get(selector, EmptyLocator())

    async def evaluate(self, script: str, arg: Any = None) -> Any:
        self.evaluations.append((script, arg))
        return len(self.selectors.get(".task-image", EmptyLocator()).items or [])



class FakeMouse:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    async def move(self, x: float, y: float, *, steps: int = 1) -> None:
        self.events.append(("move", x, y, steps))

    async def click(self, x: float, y: float) -> None:
        self.events.append(("click", x, y))

    async def down(self) -> None:
        self.events.append(("down",))

    async def up(self) -> None:
        self.events.append(("up",))


class FakePage:
    def __init__(
        self,
        main_frame: FakeFrame,
        responses: list[tuple[bool, str]] | None = None,
        *,
        widget_present: bool = False,
    ) -> None:
        self.main_frame = main_frame
        self.frames = [main_frame]
        self.mouse = FakeMouse()
        self.responses = list(responses or [(False, "")])
        self.listeners: dict[str, list[Callable[..., Any]]] = {}
        self.page_screenshots: list[dict[str, Any]] = []
        self.widget_present = widget_present

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.page_screenshots.append(kwargs)
        # 返回真实 PNG：坐标网格叠加会解码这份字节，假数据会被正确地判为不可用。
        return _png(200, 160, (250, 250, 250))

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable[..., Any]) -> None:
        self.listeners[event].remove(callback)

    async def evaluate(self, script: str) -> Any:
        # widget 存在探测与 response 读取共用 evaluate，按脚本内容区分。
        if "data-sitekey" in script:
            return self.widget_present
        if len(self.responses) > 1:
            present, token = self.responses.pop(0)
        else:
            present, token = self.responses[0]
        return {"present": present, "token": token}

    def locator(self, _selector: str) -> EmptyLocator:
        return EmptyLocator()


class FakeVision:
    def __init__(self, answers: list[dict[str, Any]]) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    async def solve_hcaptcha(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return self.answers.pop(0)


class SlowResponse:
    url = "https://hcaptcha.com/getcaptcha"

    async def json(self) -> dict[str, Any]:
        await asyncio.sleep(60)
        return {}


class FakeNetworkResponse:
    def __init__(self, payload: dict[str, Any], url: str = "https://hcaptcha.com/checkcaptcha/abc") -> None:
        self.url = url
        self.payload = payload

    async def json(self) -> dict[str, Any]:
        return dict(self.payload)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def challenge_frame(*, tiles: int = 0) -> FakeFrame:
    selectors: dict[str, FakeLocator] = {
        ".prompt-text": FakeLocator(text="请选择所有公交车"),
        ".challenge-container": FakeLocator(box={"x": 100, "y": 200, "width": 400, "height": 300}),
        "button.button-submit": FakeLocator(box={"x": 520, "y": 450, "width": 60, "height": 30}),
    }
    if tiles:
        selectors[".task-image"] = FakeLocator(
            items=[
                FakeLocator(box={"x": 100 + index * 50, "y": 200, "width": 40, "height": 40})
                for index in range(tiles)
            ]
        )
    return FakeFrame("https://newassets.hcaptcha.com/captcha/v1/challenge", selectors)


def options(**values: Any) -> HCaptchaOptions:
    defaults = {
        "presence_timeout_ms": 0,
        "post_action_wait_ms": 0,
        "poll_interval_ms": 10,
        "total_timeout_ms": 1000,
        "round_timeout_ms": 500,
    }
    defaults.update(values)
    return HCaptchaOptions(**defaults)


def test_result_repr_redacts_token() -> None:
    result = HCaptchaSolveResult("success", "ok", token="secret-token")

    assert result.ok
    assert "secret-token" not in repr(result)
    assert "<redacted>" in repr(result)


def test_constructor_registers_response_listener_and_aclose_cancels_tasks() -> None:
    async def scenario() -> None:
        page = FakePage(FakeFrame("https://site.invalid"))
        solver = HCaptchaSolver(page, options=options())
        listener = page.listeners["response"][0]
        listener(SlowResponse())
        task = next(iter(solver._response_tasks))

        await solver.aclose()

        assert page.listeners["response"] == []
        assert task.cancelled()
        assert solver._response_tasks == set()

    run(scenario())


def test_trigger_runs_after_listener_registration() -> None:
    page = FakePage(FakeFrame("https://site.invalid"), responses=[(False, "")])
    observations: list[bool] = []

    async def trigger() -> None:
        observations.append(bool(page.listeners.get("response")))

    result = run(solve(page, trigger=trigger, options=options()))

    assert observations == [True]
    assert result.status == "not_present"
    assert page.listeners["response"] == []


def test_reads_existing_main_page_response_without_vision() -> None:
    page = FakePage(FakeFrame("https://site.invalid"), responses=[(True, "signed-token")])
    vision = FakeVision([{"type": "grid", "confidence": 1, "actions": [1]}])

    result = run(solve(page, options=options(), vision_client=vision))

    assert result.status == "success"
    assert result.token == "signed-token"
    assert vision.calls == []


def test_finds_hcaptcha_frame_recursively_up_to_four_levels() -> None:
    checkbox = FakeLocator(box={"x": 20, "y": 30, "width": 30, "height": 30})
    target = FakeFrame("https://newassets.hcaptcha.com/checkbox", {"#checkbox": checkbox})
    root = FakeFrame("https://site.invalid", children=[FakeFrame("about:blank", children=[target])])
    page = FakePage(root, responses=[(True, ""), (True, "auto-token")])
    vision = FakeVision([])

    result = run(solve(page, options=options(post_action_wait_ms=20), vision_client=vision))

    assert result.status == "success"
    assert result.token == "auto-token"
    assert any(event[0] == "click" for event in page.mouse.events)
    assert vision.calls == []


def test_checkbox_frame_is_not_misclassified_as_challenge() -> None:
    checkbox = FakeLocator(box={"x": 20, "y": 30, "width": 30, "height": 30})
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/?frame=checkbox",
        {"#checkbox": checkbox},
    )
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, "auto-token")],
    )

    result = run(solve(page, options=options(post_action_wait_ms=20)))

    assert result.status == "success"
    assert result.token == "auto-token"
    assert any(event[0] == "click" for event in page.mouse.events)


def test_network_pass_response_returns_generated_token() -> None:
    async def scenario() -> HCaptchaSolveResult:
        page = FakePage(FakeFrame("https://site.invalid"), responses=[(False, "")])
        solver = HCaptchaSolver(page, options=options(presence_timeout_ms=20))
        listener = page.listeners["response"][0]
        listener(FakeNetworkResponse({"pass": True, "generated_pass_UUID": "network-token"}))
        await asyncio.sleep(0)
        try:
            return await solver.solve()
        finally:
            await solver.aclose()

    result = run(scenario())

    assert result.status == "success"
    assert result.token == "network-token"
    assert "network-token" not in repr(result)


def test_network_pass_without_token_does_not_report_success() -> None:
    async def scenario() -> HCaptchaSolveResult:
        page = FakePage(FakeFrame("https://site.invalid"), responses=[(True, "")])
        solver = HCaptchaSolver(page, options=options(presence_timeout_ms=20))
        page.listeners["response"][0](FakeNetworkResponse({"pass": True}))
        await asyncio.sleep(0)
        try:
            return await solver.solve()
        finally:
            await solver.aclose()

    result = run(scenario())

    assert result.status == "timeout"
    assert result.token == ""


def test_empty_response_field_is_not_misreported_as_no_hcaptcha() -> None:
    page = FakePage(FakeFrame("https://site.invalid"), responses=[(True, "")])

    result = run(solve(page, options=options(presence_timeout_ms=20)))

    assert result.status == "timeout"


def test_hidden_first_checkbox_does_not_hide_visible_widget() -> None:
    hidden = FakeLocator(box={"x": 1, "y": 1, "width": 20, "height": 20})
    hidden.visible = False
    visible = FakeLocator(box={"x": 40, "y": 50, "width": 30, "height": 30})
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/?frame=checkbox",
        {"#checkbox": FakeLocator(items=[hidden, visible])},
    )
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, "auto-token")],
    )

    result = run(solve(page, options=options(post_action_wait_ms=20)))

    assert result.status == "success"
    assert ("click", 55.0, 65.0) in page.mouse.events


def test_grid_collects_numbered_tiles_clicks_and_submits() -> None:
    frame = challenge_frame(tiles=4)
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "grid-token")],
    )
    vision = FakeVision([{"type": "grid", "confidence": 0.95, "actions": [1, 3]}])

    result = run(solve(page, options=options(post_action_wait_ms=20), vision_client=vision))

    assert result.status == "success"
    assert result.rounds == 1
    assert result.challenge_type == "grid"
    assert [tile["index"] for tile in vision.calls[0]["tiles"]] == [1, 2, 3, 4]
    assert len(frame.evaluations) == 2
    assert "data-nf-hcaptcha-index" in frame.evaluations[1][0]
    clicks = [event for event in page.mouse.events if event[0] == "click"]
    assert len(clicks) == 3


def test_point_maps_zero_to_thousand_coordinates() -> None:
    frame = challenge_frame()
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "point-token")],
    )
    vision = FakeVision([{"type": "point", "confidence": 0.9, "points": [{"x": 250, "y": 500}]}])

    result = run(solve(page, options=options(post_action_wait_ms=20), vision_client=vision))

    assert result.status == "success"
    assert ("click", 200.0, 350.0) in page.mouse.events


def test_drag_uses_move_down_move_up() -> None:
    frame = challenge_frame()
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "drag-token")],
    )
    vision = FakeVision(
        [{"type": "drag", "confidence": 0.99, "start": [0, 0], "end": [1000, 1000]}]
    )

    result = run(solve(page, options=options(post_action_wait_ms=20), vision_client=vision))

    assert result.status == "success"
    assert ("down",) in page.mouse.events
    assert ("up",) in page.mouse.events
    assert ("move", 100.0, 200.0, 1) in page.mouse.events
    assert ("move", 500.0, 500.0, 2) in page.mouse.events
    assert page.mouse.events.index(("down",)) < page.mouse.events.index(("up",))


def test_drag_clicks_verify_that_replaces_skip_after_drop() -> None:
    """真实 drag 题放下对象后按钮会由 Skip 变为 Verify，必须提交答案。"""
    submit = FakeLocator(
        text="Skip",
        box={"x": 520, "y": 450, "width": 60, "height": 30},
    )
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="Please drag the element to the place where it fits"),
            ".challenge-container": FakeLocator(
                box={"x": 100, "y": 200, "width": 400, "height": 300}
            ),
            "button.button-submit": submit,
        },
    )

    class VerifyAfterDropMouse(FakeMouse):
        async def up(self) -> None:
            await super().up()
            submit.text = "Verify"

    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    page.mouse = VerifyAfterDropMouse()
    solver = HCaptchaSolver(
        page,
        options=options(move_before_click=False, poll_interval_ms=10),
    )
    answer = {
        "challenge_type": "drag",
        "confidence": 1,
        "drags": [{"start": {"x": 0, "y": 0}, "end": {"x": 1000, "y": 1000}}],
    }

    applied = run(solver._apply_drag(frame, answer))
    run(solver.aclose())

    assert applied is True
    assert submit.text == "Verify"
    assert ("click", 550.0, 465.0) in page.mouse.events
    assert page.mouse.events.index(("up",)) < page.mouse.events.index(("click", 550.0, 465.0))


def test_drag_end_timeout_always_releases_mouse() -> None:
    """按下后的终段移动超时也必须 mouse.up，不能污染后续浏览器操作。"""

    class StallingEndMouse(FakeMouse):
        async def move(self, x: float, y: float, *, steps: int = 1) -> None:
            if steps == 2:
                await asyncio.sleep(30)
                return
            self.events.append(("move", x, y, steps))

    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    page.mouse = StallingEndMouse()
    solver = HCaptchaSolver(
        page,
        options=options(drag_move_timeout_ms=100),
    )
    answer = {
        "challenge_type": "drag",
        "confidence": 1,
        "drags": [{"start": {"x": 0, "y": 0}, "end": {"x": 1000, "y": 1000}}],
    }

    applied = run(solver._apply_drag(frame, answer))
    run(solver.aclose())

    assert applied is False
    assert ("down",) in page.mouse.events
    assert ("up",) in page.mouse.events
    assert page.mouse.events.index(("down",)) < page.mouse.events.index(("up",))


def test_low_confidence_empty_and_out_of_bounds_actions_do_not_click() -> None:
    answers = [
        {"type": "grid", "confidence": 0.2, "actions": [1]},
        {"type": "grid", "confidence": 0.99, "actions": []},
        {"type": "grid", "confidence": True, "actions": [1]},
        {"type": "grid", "confidence": 0.99, "actions": [True]},
        {"type": "grid", "confidence": 0.99, "actions": [1.9]},
        {
            "type": "grid",
            "confidence": 0.99,
            "actions": [1],
            "points": [{"x": 100, "y": 100}],
        },
        {"type": "point", "confidence": 0.99, "points": [{"x": 1001, "y": 50}]},
    ]
    for answer in answers:
        frame = challenge_frame(tiles=2)
        page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
        result = run(solve(page, options=options(), vision_client=FakeVision([answer])))

        assert result.status == "uncertain"
        assert not any(event[0] in {"click", "down"} for event in page.mouse.events)


def test_unknown_task_type_is_unsupported() -> None:
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])

    result = run(
        solve(
            page,
            options=options(),
            vision_client=FakeVision([{"type": "audio", "confidence": 1, "actions": [1]}]),
        )
    )

    assert result.status == "unsupported"


def test_missing_vision_configuration_reports_not_configured(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr("browser.openai_vision._LOCAL_CONFIG_PATH", tmp_path / "missing.json")
    for name in ("HCAPTCHA_VISION_CONFIG", "HCAPTCHA_OPENAI_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    frame = challenge_frame(tiles=2)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])

    result = run(solve(page, options=options()))

    assert result.status == "not_configured"


def test_json_secret_enables_default_vision_client(monkeypatch: Any, tmp_path: Any) -> None:
    monkeypatch.setattr("browser.openai_vision._LOCAL_CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.delenv("HCAPTCHA_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv(
        "HCAPTCHA_VISION_CONFIG",
        '{"api_key":"json-key","base_url":"https://vision.example/v1","model":"vision"}',
    )
    solver = HCaptchaSolver(FakePage(FakeFrame("https://site.invalid")), options=options())

    client = run(solver._default_vision_client())

    assert client is not None
    assert client.config.api_key == "json-key"
    assert client.config.base_url == "https://vision.example/v1"
    assert client.config.model == "vision"
    run(solver.aclose())


def test_round_budget_is_split_across_vision_attempts(monkeypatch: Any, tmp_path: Any) -> None:
    """一次请求不得独占整轮，否则瞬时断连后的重试永远没有执行机会。"""
    monkeypatch.setattr("browser.openai_vision._LOCAL_CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setenv(
        "HCAPTCHA_VISION_CONFIG",
        '{"api_key":"k","base_url":"https://v.example/v1",'
        '"model":"m","timeout_ms":240000}',
    )
    solver = HCaptchaSolver(
        FakePage(FakeFrame("https://site.invalid")),
        options=options(
            round_timeout_ms=125_000,
            vision_max_attempts=3,
            vision_retry_reserve_ms=4_000,
        ),
    )

    client = run(solver._default_vision_client())

    assert solver._vision_attempt_timeout_ms() == 40_333
    assert client is not None
    assert abs(client.config.timeout - 40.333) < 0.001
    assert client.max_attempts == 3
    run(solver.aclose())


def test_max_rounds_is_finite() -> None:
    frame = challenge_frame(tiles=2)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    vision = FakeVision(
        [
            {"type": "grid", "confidence": 1, "actions": [1]},
            {"type": "grid", "confidence": 1, "actions": [2]},
        ]
    )

    result = run(solve(page, options=options(max_rounds=2), vision_client=vision))

    assert result.status == "failed"
    assert result.rounds == 2
    assert len(vision.calls) == 2


def test_round_timeout_and_total_timeout_are_structured() -> None:
    class SlowVision:
        async def solve_hcaptcha(self, **_request: Any) -> dict[str, Any]:
            await asyncio.sleep(1)
            return {}

    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    round_result = run(
        solve(page, options=options(round_timeout_ms=1, total_timeout_ms=100), vision_client=SlowVision())
    )
    total_result = run(
        solve(page, options=options(round_timeout_ms=100, total_timeout_ms=1), vision_client=SlowVision())
    )

    assert round_result.status == "timeout"
    assert round_result.rounds == 1
    assert total_result.status == "timeout"


class LateMountFrame(FakeFrame):
    """iframe 元素已在页面上，但内部控件延迟若干次轮询后才可定位。"""

    def __init__(self, url: str, selectors: dict[str, FakeLocator], *, ready_after: int) -> None:
        super().__init__(url, {})
        self._real_selectors = selectors
        self._ready_after = ready_after
        self.probes = 0

    def locator(self, selector: str) -> FakeLocator:
        self.probes += 1
        if self.probes <= self._ready_after:
            return EmptyLocator()
        return self._real_selectors.get(selector, EmptyLocator())


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_prompt_task_type_inference_uses_visible_instruction() -> None:
    assert _task_type_from_prompt("Please drag the element to the gap", has_tiles=False) == "drag"
    assert _task_type_from_prompt("Please click on the animal", has_tiles=False) == "point"
    assert _task_type_from_prompt("Move ONE animal to the matching silhouette", has_tiles=False) == "drag"
    assert (
        _task_type_from_prompt(
            "Find all animals based on the number provided", has_tiles=False
        )
        == "point"
    )
    assert _task_type_from_prompt("Please click all buses", has_tiles=True) == "grid"
    assert _task_type_from_prompt("Solve this challenge", has_tiles=False) == "unknown"


def test_temporal_prompt_detection_and_contact_sheet() -> None:
    from io import BytesIO

    from PIL import Image

    assert _is_temporal_prompt("Please click on the shape that grows")
    assert _is_temporal_prompt("Please click on the animal who jumps the highest")
    assert not _is_temporal_prompt("Please click on the two crosses")

    frames = [_png(200, 160, (20 + index * 20, 40, 80)) for index in range(6)]
    sheet = temporal_contact_sheet(frames, columns=3, max_edge=800, quality=72)

    assert sheet
    with Image.open(BytesIO(sheet)) as image:
        assert image.format == "JPEG"
        assert max(image.size) <= 800
        assert image.width > image.height


def test_temporal_point_round_attaches_sequence_image_only_when_needed() -> None:
    class SequencePage(FakePage):
        def __init__(self, main_frame: FakeFrame, images: list[bytes]) -> None:
            super().__init__(main_frame)
            self.images = list(images)

        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return self.images.pop(0) if len(self.images) > 1 else self.images[0]

    temporal_frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="Please click on the shape that grows"),
            ".challenge-container": FakeLocator(),
        },
    )
    temporal_images = [
        _png(200, 160, (30, 40, 50)),
        _png(200, 160, (80, 90, 100)),
        _png(200, 160, (140, 150, 160)),
    ]
    temporal_page = SequencePage(
        FakeFrame("https://site.invalid", children=[temporal_frame]), temporal_images
    )
    temporal_solver = HCaptchaSolver(
        temporal_page,
        options=options(
            temporal_frames=3,
            temporal_interval_ms=10,
            temporal_sheet_max_edge=600,
            compress_uploads=False,
        ),
    )
    temporal_request = run(temporal_solver._capture_round(temporal_frame, 1))
    run(temporal_solver.aclose())

    static_frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="Please click on the two crosses"),
            ".challenge-container": FakeLocator(),
        },
    )
    static_page = FakePage(FakeFrame("https://site.invalid", children=[static_frame]))
    static_solver = HCaptchaSolver(
        static_page,
        options=options(temporal_frames=3, temporal_interval_ms=10),
    )
    static_request = run(static_solver._capture_round(static_frame, 1))
    run(static_solver.aclose())

    assert temporal_request.get("temporal_image")
    assert temporal_request["temporal_media_type"] == "image/jpeg"
    assert temporal_request["image"] == temporal_images[-1]
    assert temporal_request["temporal_phase_image"] == temporal_images[-1]
    assert len(temporal_page.page_screenshots) == 3
    assert "temporal_image" not in static_request
    assert len(static_page.page_screenshots) == 1


def _striped_png(width: int, height: int, stripes: int) -> bytes:
    """竖条纹 PNG：dHash 比较横向相邻像素，条纹数不同则指纹必然不同。"""
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(image)
    band = max(1, width // max(1, stripes))
    for index in range(stripes):
        if index % 2:
            draw.rectangle((index * band, 0, (index + 1) * band, height), fill=(20, 20, 20))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_grid_fingerprint_ignores_locally_injected_tile_labels() -> None:
    """指纹必须基于无标签画面，否则模型答案会被误判为「题面已更新」而丢弃。"""

    class LabelAwareFrame(FakeFrame):
        def __init__(self, url: str, selectors: dict[str, FakeLocator]) -> None:
            super().__init__(url, selectors)
            self.labels_on = False

        async def evaluate(self, script: str, arg: Any = None) -> Any:
            self.evaluations.append((script, arg))
            # 移除脚本只出现连字符属性与 badge.remove()，新增脚本才带驼峰 dataset 名。
            if "remove()" in script:
                self.labels_on = False
                return None
            if "nfHcaptchaIndex" in script:
                self.labels_on = True
                return len(self.selectors.get(".task-image", EmptyLocator()).items or [])
            return len(self.selectors.get(".task-image", EmptyLocator()).items or [])

    class LabelAwarePage(FakePage):
        def __init__(self, main_frame: FakeFrame, challenge: LabelAwareFrame) -> None:
            super().__init__(main_frame)
            self.challenge = challenge

        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            # 标签开启时画面明显不同，模拟真实注入的红色序号徽章。
            if self.challenge.labels_on:
                return _striped_png(200, 160, 8)
            return _striped_png(200, 160, 2)

    tiles = [FakeLocator(box={"x": 100 + index * 40, "y": 200, "width": 36, "height": 36}) for index in range(9)]
    challenge = LabelAwareFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="Click on all objects smaller than the example"),
            ".challenge-container": FakeLocator(box={"x": 100, "y": 200, "width": 400, "height": 300}),
            ".task-image": FakeLocator(items=tiles),
        },
    )
    page = LabelAwarePage(FakeFrame("https://site.invalid", children=[challenge]), challenge)
    solver = HCaptchaSolver(page, options=options(compress_uploads=False))

    request = run(solver._capture_round(challenge, 1))
    fingerprint = str(request["challenge_fingerprint"])
    _frame, _target, same = run(
        solver._refresh_action_context(
            challenge,
            task_type="grid",
            prompt="Click on all objects smaller than the example",
            original_fingerprint=fingerprint,
        )
    )
    run(solver.aclose())

    assert len(request["tiles"]) == 9
    # 两种画面的指纹必须确实不同，否则本测试无法证明修复生效。
    assert _image_fingerprint(_striped_png(200, 160, 8)) != _image_fingerprint(
        _striped_png(200, 160, 2)
    )
    # 发给模型的图仍带标签，用于指纹的参考图不带标签，因此复核判定为同一题。
    assert request["image"] == _striped_png(200, 160, 8)
    assert fingerprint == _image_fingerprint(_striped_png(200, 160, 2))
    assert same is True


def _drag_scene(
    badges: tuple[tuple[int, int], ...] = ((394, 119),), size: tuple[int, int] = (500, 470)
) -> bytes:
    """合成 drag 题图：中亮度背景 + 近黑 Move 胶囊（内含白字块）。"""
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, (150, 180, 200))
    draw = ImageDraw.Draw(image)
    for x, y in badges:
        draw.rectangle((x, y, x + 59, y + 19), fill=(30, 35, 45))
        for offset in (10, 24, 38):
            draw.rectangle((x + offset, y + 7, x + offset + 5, y + 12), fill=(255, 255, 255))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_detect_move_source_finds_single_badge_and_refuses_ambiguity() -> None:
    """Move 胶囊唯一时给出源中心；缺失、多候选或无法定位时必须放弃而不是猜。"""
    detected = detect_move_source(_drag_scene())

    assert detected is not None
    # 合成胶囊宽 60（白字团合并约 44）、中心约 x=421，中心点下移到源方块主体，
    # 归一化到 0-1000 后 x=842、y≈339。
    assert round(detected[0]) == 842
    assert abs(detected[1] - 339) <= 2

    # 等比缩放后归一化坐标必须保持一致（真实链路会先压缩再上传）。
    compressed, _media = compress_for_vision(_drag_scene(), max_edge=250, quality=90)
    scaled = detect_move_source(compressed)
    assert scaled is not None
    assert abs(scaled[0] - detected[0]) <= 12
    assert abs(scaled[1] - detected[1]) <= 12

    assert detect_move_source(_png(500, 470, (150, 180, 200))) is None
    assert detect_move_source(b"") is None
    assert detect_move_source(b"not-an-image") is None
    # 多候选时唯一源检测必须拒绝，交由多源检测与视觉模型判断。
    assert detect_move_source(_drag_scene(badges=((394, 119), (60, 119)))) is None


def test_detect_move_sources_finds_all_badges() -> None:
    """多源题必须检出全部可拖动元素，且尺寸一致的误检块要被剔除。"""
    sources = detect_move_sources(_drag_scene(badges=((60, 119), (394, 119))))

    assert len(sources) == 2
    x_values = sorted(round(point[0]) for point in sources)
    assert x_values == [174, 842]
    assert all(abs(point[1] - 339) <= 2 for point in sources)


def test_drag_detail_sheet_enlarges_both_sides_and_keeps_original_coordinate_ticks() -> None:
    from io import BytesIO

    from PIL import Image

    detail = drag_detail_sheet(_drag_scene(), [(848.0, 358.0)], max_edge=900, quality=82)

    assert detail
    with Image.open(BytesIO(detail)) as decoded:
        assert decoded.format == "JPEG"
        assert max(decoded.size) <= 900
        assert decoded.width > decoded.height
    assert drag_detail_sheet(_drag_scene(), []) == b""
    assert drag_detail_sheet(_drag_scene(), [(500.0, 400.0)]) == b""


def test_drag_round_attaches_high_resolution_detail_image() -> None:
    class DragScenePage(FakePage):
        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return _drag_scene()

    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="Please drag the element to the place where it fits"),
            ".challenge-container": FakeLocator(
                box={"x": 100, "y": 200, "width": 500, "height": 470}
            ),
        },
    )
    page = DragScenePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options(compress_uploads=False))
    solver._network_task_type = "drag"

    request = run(solver._capture_round(frame, 1))
    run(solver.aclose())

    assert request["source_hint"]
    assert request["drag_detail_image"]
    assert request["drag_detail_media_type"] == "image/jpeg"
    assert "grid_image" not in request
    assert request["image"] == _drag_scene()


def test_drag_start_uses_detected_source_and_fixes_reversed_direction() -> None:
    """检测到的源覆盖模型起点；模型写反方向时按距离自动纠正。"""
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options())
    answer = {
        "challenge_type": "drag",
        "confidence": 1,
        "drags": [{"start": {"x": 100, "y": 100}, "end": {"x": 900, "y": 900}}],
    }

    applied = run(solver._apply_drag(frame, answer, source_points=[(900.0, 900.0)]))
    run(solver.aclose())

    assert applied is True
    # box=(100,200,400,300)：源 (900,900) → (460,470)，目标 (100,100) → (140,230)。
    assert ("move", 460.0, 470.0, 1) in page.mouse.events
    assert ("move", 140.0, 230.0, 2) in page.mouse.events


def test_drag_refuses_when_both_model_points_hug_detected_source() -> None:
    """两端都紧贴检测源时无法判断目标，必须判为不确定而不是原地拖拽。"""
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options())
    answer = {
        "challenge_type": "drag",
        "confidence": 1,
        "drags": [{"start": {"x": 510, "y": 500}, "end": {"x": 500, "y": 520}}],
    }

    applied = run(solver._apply_drag(frame, answer, source_points=[(500.0, 500.0)]))
    run(solver.aclose())

    assert applied is False
    assert ("down",) not in page.mouse.events


def test_coordinate_grid_overlay_keeps_size_and_adds_marks() -> None:
    """网格图必须与原图同尺寸，且真的改变了像素（画上了刻度）。"""
    from io import BytesIO

    from PIL import Image

    base = _png(200, 160, (240, 240, 240))
    grid = coordinate_grid_overlay(base)

    assert grid
    with Image.open(BytesIO(grid)) as image:
        assert image.size == (200, 160)
    assert grid != base


def test_coordinate_grid_overlay_rejects_unusable_input() -> None:
    assert coordinate_grid_overlay(b"") == b""
    assert coordinate_grid_overlay(b"not-an-image") == b""
    # 过小的图没有可读刻度空间，不生成辅助图。
    assert coordinate_grid_overlay(_png(10, 10, (0, 0, 0))) == b""


def test_grid_ticks_match_the_click_mapping_scale() -> None:
    """网格刻度必须与 _apply_point 的换算同一尺度，否则模型读数会点偏。

    网格画在 action_target 的截图上，刻度 n 表示该图宽/高的 n/1000。
    _apply_point 用 box + box_size * coord / 1000 换算，两者必须一致。
    """
    box = {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0}
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="click the target"),
            ".challenge-view": FakeLocator(box=box),
            ".challenge-container": FakeLocator(box=box),
            "button.button-submit": FakeLocator(box={"x": 520, "y": 560, "width": 60, "height": 30}),
        },
    )
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "tick-token")],
    )
    # 模型读到刻度 500/500（正中心）时，点击应落在 action_target 的几何中心。
    vision = FakeVision([{"type": "point", "confidence": 0.95, "points": [{"x": 500, "y": 500}]}])
    solver = HCaptchaSolver(page, options=options(post_action_wait_ms=20), vision_client=vision)
    solver._network_task_type = "point"

    result = run(solver.solve())
    run(solver.aclose())

    assert result.status == "success"
    clicks = [event for event in page.mouse.events if event[0] == "click"]
    assert clicks, "应产生真实点击"
    expected_x = box["x"] + box["width"] * 0.5
    expected_y = box["y"] + box["height"] * 0.5
    assert (round(clicks[0][1]), round(clicks[0][2])) == (round(expected_x), round(expected_y))


def test_coordinate_grid_adapts_to_dark_and_light_backgrounds() -> None:
    """亮底与暗底必须生成不同网格（自适应对比），否则刻度可能不可见。"""
    light = coordinate_grid_overlay(_png(200, 160, (245, 245, 245)))
    dark = coordinate_grid_overlay(_png(200, 160, (12, 12, 12)))

    assert light and dark
    assert light != dark


def test_point_round_attaches_grid_image_and_grid_round_does_not() -> None:
    point_frame = challenge_frame()
    point_page = FakePage(FakeFrame("https://site.invalid", children=[point_frame]))
    point_solver = HCaptchaSolver(point_page, options=options(), vision_client=FakeVision([]))
    point_solver._network_task_type = "point"
    point_request = run(point_solver._capture_round(point_frame, 1))
    run(point_solver.aclose())

    grid_frame = challenge_frame(tiles=4)
    grid_page = FakePage(FakeFrame("https://site.invalid", children=[grid_frame]))
    grid_solver = HCaptchaSolver(grid_page, options=options(), vision_client=FakeVision([]))
    grid_request = run(grid_solver._capture_round(grid_frame, 1))
    run(grid_solver.aclose())

    assert "grid_image" not in grid_request, "grid 型用图块序号，不需要坐标网格"
    assert point_request.get("grid_image"), "point 型需要坐标网格辅助定位"


def test_coordinate_grid_can_be_disabled_by_option() -> None:
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options(coordinate_grid=False), vision_client=FakeVision([]))
    solver._network_task_type = "point"

    request = run(solver._capture_round(frame, 1))
    run(solver.aclose())

    assert "grid_image" not in request


def test_client_without_grid_support_still_receives_request() -> None:
    """客户端不接受 grid_image 时应退回只发主图，而不是整轮失败。"""

    class NarrowVision:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def solve_hcaptcha(
            self, *, image: Any, prompt: str = "", task_type: str = "unknown",
            tiles: Any = None, round: int | None = None,
        ) -> dict[str, Any]:
            self.calls.append({"task_type": task_type})
            return {"challenge_type": "point", "confidence": 0.9, "points": [{"x": 100, "y": 200}]}

    vision = NarrowVision()
    frame = challenge_frame()
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "narrow-token")],
    )
    solver = HCaptchaSolver(page, options=options(post_action_wait_ms=20), vision_client=vision)
    solver._network_task_type = "point"

    result = run(solver.solve())
    run(solver.aclose())

    assert vision.calls, "不支持网格图不应导致请求被丢弃"
    assert result.status == "success"


def test_widget_present_but_iframe_not_ready_keeps_waiting() -> None:
    """控件已出现、iframe 内部未就绪时必须继续等待，不能判为未发现 hCaptcha。"""
    late = LateMountFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": FakeLocator(box={"x": 20, "y": 30, "width": 28, "height": 28})},
        ready_after=3,
    )
    page = FakePage(
        FakeFrame("https://site.invalid", children=[late]),
        responses=[(True, ""), (True, ""), (True, "late-token")],
        widget_present=True,
    )
    logs: list[str] = []

    result = run(
        solve(
            page,
            options=options(presence_timeout_ms=0, widget_mount_timeout_ms=5_000, post_action_wait_ms=20),
            vision_client=FakeVision([]),
            log=logs.append,
        )
    )

    assert result.status == "success"
    assert result.token == "late-token"
    assert any("尚未就绪" in line for line in logs)


def test_submit_selector_ignores_skip_and_offscreen_candidates() -> None:
    """hCaptcha 的 .button-submit 可能实际是 Skip，离屏旧挑战也会保持 visible。"""
    logs: list[str] = []
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            "button.button-submit": FakeLocator(
                text="Skip", box={"x": 500, "y": 600, "width": 60, "height": 30}
            ),
            "button[type='submit']": FakeLocator(
                text="Submit", box={"x": 400, "y": -9458, "width": 60, "height": 30}
            ),
        },
    )
    solver = HCaptchaSolver(
        FakePage(FakeFrame("https://site.invalid")), options=options(), log=logs.append
    )

    submit = run(solver._first_submit_locator(frame, timeout_ms=0))
    run(solver.aclose())

    assert submit is None
    assert any("跳过按钮" in line for line in logs)
    assert any("离屏" in line for line in logs)


def test_submit_selector_keeps_real_verify_button_clickable() -> None:
    verify = FakeLocator(text="Verify", box={"x": 520, "y": 450, "width": 60, "height": 30})
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {"button.button-submit": verify},
    )
    page = FakePage(FakeFrame("https://site.invalid"))
    page.viewport_size = {"width": 1280, "height": 720}
    solver = HCaptchaSolver(page, options=options())

    submit = run(solver._first_submit_locator(frame, timeout_ms=0))
    clicked = run(solver._mouse_click_locator(submit, label="提交按钮"))
    run(solver.aclose())

    assert submit is verify
    assert clicked is True
    assert ("click", 550.0, 465.0) in page.mouse.events


def test_direct_click_mode_skips_pre_move() -> None:
    class ClickableLocator(FakeLocator):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.clicks: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.clicks.append(kwargs)

    frame = challenge_frame(tiles=1)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options(move_before_click=False))
    target = ClickableLocator(box={"x": 10, "y": 20, "width": 40, "height": 40})

    clicked = run(solver._mouse_click_locator(target, label="测试元素"))
    run(solver.aclose())

    assert clicked is True
    assert target.clicks == [{"force": True, "timeout": 5_000}]
    assert page.mouse.events == []


def test_direct_point_uses_locator_relative_position() -> None:
    class ClickableLocator(FakeLocator):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.clicks: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.clicks.append(kwargs)

    target = ClickableLocator(box={"x": 100, "y": 200, "width": 400, "height": 300})
    frame = FakeFrame("https://newassets.hcaptcha.com/captcha/v1/challenge")
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options(move_before_click=False, poll_interval_ms=10))

    applied = run(
        solver._apply_point(
            frame,
            {"points": [{"x": 250, "y": 500}]},
            target=target,
        )
    )
    run(solver.aclose())

    assert applied is True
    assert target.clicks == [
        {
            "force": True,
            "timeout": 5_000,
            "position": {"x": 100.0, "y": 150.0},
        }
    ]
    assert page.mouse.events == []


def test_slow_click_is_bounded() -> None:
    import time as _time

    class SlowClickMouse(FakeMouse):
        async def click(self, x: float, y: float) -> None:
            await asyncio.sleep(30)
            self.events.append(("click", x, y))

    page = FakePage(FakeFrame("https://site.invalid"))
    page.mouse = SlowClickMouse()
    solver = HCaptchaSolver(
        page,
        options=options(move_before_click=False, click_timeout_ms=100),
    )

    began = _time.monotonic()
    clicked = run(solver._mouse_click_at(30, 40, label="测试元素"))
    elapsed = _time.monotonic() - began
    run(solver.aclose())

    assert clicked is False
    assert elapsed < 2
    assert page.mouse.events == []


def test_slow_humanized_move_cannot_stall_the_click() -> None:
    """拟人化移动必须有上限：否则点击开销会吃光预算，表现为「总体超时、零次调用」。

    实测 Camoufox humanize=True 下单次 mouse.move 耗时 17.7s，两段共 29.5s，
    而 humanize=False 仅 0.2s。这里用慢速 mouse 复现并确认落点点击仍然发生。
    """
    import time as _time

    class SlowMouse(FakeMouse):
        async def move(self, x: float, y: float, *, steps: int = 1) -> None:
            await asyncio.sleep(30)
            self.events.append(("move", x, y, steps))

    frame = challenge_frame(tiles=1)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    page.mouse = SlowMouse()
    solver = HCaptchaSolver(page, options=options(move_timeout_ms=200))
    target = FakeLocator(box={"x": 10, "y": 20, "width": 40, "height": 40})

    began = _time.monotonic()
    clicked = run(solver._mouse_click_locator(target, label="测试元素"))
    elapsed = _time.monotonic() - began
    run(solver.aclose())

    assert clicked is True, "移动超时不应导致点击失败"
    assert elapsed < 5, f"移动未受上限约束，耗时 {elapsed:.1f}s"
    # 落点点击必须真实发生，且坐标为元素中心
    clicks = [e for e in page.mouse.events if e[0] == "click"]
    assert clicks == [("click", 30.0, 40.0)]


def test_irrelevant_junk_field_does_not_invalidate_a_valid_plan() -> None:
    """实测第 2 轮：drag 答案正确，但模型附带了与 drag 无关的垃圾 tile_indices
    （{"source": [...], "target": [...]}），旧实现因此丢掉整份正确答案。
    非 list 的字段不是可用动作，不该构成矛盾。
    """
    real_output = {
        "challenge_type": "drag",
        "confidence": 0.95,
        "drags": [{"end": {"x": 431, "y": 830}, "start": {"x": 753, "y": 475}}],
        "tile_indices": {"source": [8, 5], "target": [4, 8]},
    }

    normalized = _validated_answer(real_output)

    assert normalized is not None
    assert not normalized.get("_invalid")
    assert normalized["challenge_type"] == "drag"
    assert normalized["drags"] == [
        {"start": {"x": 753.0, "y": 475.0}, "end": {"x": 431.0, "y": 830.0}}
    ]


def test_junk_field_does_not_block_type_inference_but_real_conflict_still_does() -> None:
    inferred = _validated_answer(
        {
            "challenge_type": "unknown",
            "confidence": 1,
            "points": [{"x": 10, "y": 20}],
            "tile_indices": {"junk": 1},
        }
    )
    assert inferred is not None
    assert not inferred.get("_invalid")
    assert inferred["challenge_type"] == "point"

    # 两种都是真实可用动作时仍属矛盾，不能瞎猜。
    conflict = _validated_answer(
        {
            "challenge_type": "unknown",
            "confidence": 1,
            "points": [{"x": 1, "y": 2}],
            "tile_indices": [3],
        }
    )
    assert conflict is not None
    assert conflict.get("_invalid") is True


def test_singular_point_alias_is_accepted_by_solver_normalizer() -> None:
    normalized = _validated_answer(
        {"challenge_type": "point", "confidence": 1, "point": {"x": 317, "y": 444}}
    )

    assert normalized is not None
    assert normalized["challenge_type"] == "point"
    assert normalized["points"] == [{"x": 317.0, "y": 444.0}]

    target = _validated_answer(
        {"challenge_type": "point", "confidence": 0.95, "target": {"x": 753, "y": 623}}
    )
    assert target is not None
    assert target["points"] == [{"x": 753.0, "y": 623.0}]


def test_nested_row_column_hints_are_ignored_only_with_valid_point_actions() -> None:
    normalized = _validated_answer(
        {
            "challenge_type": "unknown",
            "confidence": 0.95,
            "points": [{"x": 337, "y": 531}, {"x": 632, "y": 853}],
            "tile_indices": [[2, 1], [4, 2]],
        }
    )
    assert normalized is not None
    assert normalized["challenge_type"] == "point"
    assert normalized["tile_indices"] == []
    assert normalized["points"] == [
        {"x": 337.0, "y": 531.0},
        {"x": 632.0, "y": 853.0},
    ]

    invalid = _validated_answer(
        {
            "challenge_type": "unknown",
            "confidence": 0.95,
            "tile_indices": [[2, 1], [4, 2]],
        }
    )
    assert invalid is not None and invalid.get("_invalid") is True


def test_drag_endpoint_points_are_ignored_only_when_exact_duplicates() -> None:
    normalized = _validated_answer(
        {
            "challenge_type": "drag",
            "confidence": 1,
            "drags": [{"start": [860, 360], "end": [190, 650]}],
            "points": [[190, 650], [860, 360]],
        }
    )
    assert normalized is not None
    assert not normalized.get("_invalid")
    assert normalized["points"] == []
    assert normalized["drags"] == [
        {"start": {"x": 860.0, "y": 360.0}, "end": {"x": 190.0, "y": 650.0}}
    ]

    conflict = _validated_answer(
        {
            "challenge_type": "drag",
            "confidence": 1,
            "drags": [{"start": [860, 360], "end": [190, 650]}],
            "points": [[999, 999]],
        }
    )
    assert conflict is not None and conflict.get("_invalid") is True


def test_singular_drag_alias_with_nested_points_is_accepted() -> None:
    normalized = _validated_answer(
        {
            "challenge_type": "drag",
            "confidence": 1,
            "drag": {
                "start": {"point": [915, 600]},
                "end": {"point": [135, 420]},
            },
        }
    )

    assert normalized is not None
    assert normalized["drags"] == [
        {"start": {"x": 915.0, "y": 600.0}, "end": {"x": 135.0, "y": 420.0}}
    ]


def test_top_level_source_target_point_aliases_are_accepted() -> None:
    normalized = _validated_answer(
        {
            "challenge_type": "drag",
            "confidence": 0.95,
            "source_point": {"x": 570, "y": 800},
            "target_point": {"x": 320, "y": 450},
        }
    )

    assert normalized is not None
    assert normalized["challenge_type"] == "drag"
    assert normalized["drags"] == [
        {"start": {"x": 570.0, "y": 800.0}, "end": {"x": 320.0, "y": 450.0}}
    ]


def test_real_drag_output_with_elements_and_nested_point_is_accepted() -> None:
    """实测 mimo-v2.5 的 drag 返回：elements 键 + start/end 内再包一层 point。

    模型答案本身完全正确（confidence 0.95、起终点都有），旧解析既不认 elements
    这个键名，也不解包嵌套 point，于是把一个可用答案判为无效动作丢弃，
    日志里表现为 "drag plan has no matching actions"。
    """
    real_output = {
        "challenge_type": "drag",
        "confidence": 0.95,
        "elements": [{"end": {"point": [550, 350]}, "start": {"point": [850, 390]}}],
    }

    normalized = _validated_answer(real_output)

    assert normalized is not None
    assert not normalized.get("_invalid")
    assert normalized["challenge_type"] == "drag"
    assert normalized["drags"] == [
        {"start": {"x": 850.0, "y": 390.0}, "end": {"x": 550.0, "y": 350.0}}
    ]


def test_real_drag_output_with_start_point_aliases_is_accepted() -> None:
    """2026-08-14 实测返回 start_point/end_point，必须归一化成 start/end。"""
    real_output = {
        "challenge_type": "drag",
        "confidence": 1,
        "drags": [
            {"start_point": {"x": 870, "y": 596}, "end_point": {"x": 430, "y": 381}}
        ],
    }

    normalized = _validated_answer(real_output)

    assert normalized is not None
    assert not normalized.get("_invalid")
    assert normalized["drags"] == [
        {"start": {"x": 870.0, "y": 596.0}, "end": {"x": 430.0, "y": 381.0}}
    ]


def test_nested_point_wrappers_are_unwrapped_but_bounds_still_enforced() -> None:
    from browser.hcaptcha import _point

    assert _point({"point": [850, 390]}) == (850.0, 390.0)
    assert _point({"coordinates": {"x": 10, "y": 20}}) == (10.0, 20.0)
    # 原有形态不受影响
    assert _point({"x": 100, "y": 200}) == (100.0, 200.0)
    assert _point([10, 20]) == (10.0, 20.0)
    # 越界与垃圾输入仍必须拒绝
    assert _point({"point": [1200, 10]}) is None
    assert _point({"point": "nope"}) is None


def test_unknown_type_with_unambiguous_actions_is_inferred_not_rejected() -> None:
    """模型类型填 unknown 但动作明确时应按动作反推，而不是丢掉正确答案。

    实测 mimo-v2.5 对「click the TWO crosses」返回 challenge_type=unknown 且
    points 完全正确、confidence=1，旧校验直接拒绝，白扔一个可用结果。
    """
    real_output = {
        "challenge_type": "unknown",
        "confidence": 1,
        "points": [{"x": 181, "y": 761}, {"x": 710, "y": 761}],
        "tile_indices": [],
        "drags": [],
    }

    normalized = _validated_answer(real_output)

    assert normalized is not None
    assert normalized["challenge_type"] == "point"
    assert normalized["points"] == [{"x": 181, "y": 761}, {"x": 710, "y": 761}]


def test_unknown_type_with_conflicting_actions_is_still_rejected() -> None:
    """多种动作并存仍是矛盾，不能瞎猜。"""
    normalized = _validated_answer(
        {
            "challenge_type": "unknown",
            "confidence": 1,
            "points": [{"x": 1, "y": 2}],
            "tile_indices": [3],
            "drags": [],
        }
    )

    assert normalized is not None
    assert normalized.get("_invalid") is True


def test_schema_rejection_logs_model_output_and_is_distinguishable() -> None:
    """模型判成 unknown 且不给动作时，必须记录它实际返回了什么。

    这类失败与「远端不可用」处置完全不同：前者要改提示词或换模型，后者要查端点。
    此前两者都塌缩成「未返回结构化结果」，无法区分。
    """
    from browser.openai_vision import OpenAIVisionClient, VisionClientConfig

    def transport(_url: str, **_kwargs: Any):
        # 真正矛盾的返回：同时给出 points 与 tile_indices，无法反推唯一类型。
        return {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"challenge_type":"unknown","confidence":0.9,'
                            '"points":[{"x":100,"y":200}],"tile_indices":[2],"note":"no target"}'
                        )
                    }
                }
            ]
        }

    client = OpenAIVisionClient(
        VisionClientConfig(api_key="k", base_url="https://v.example/v1", model="probe-model"),
        transport=transport,
    )
    logs: list[str] = []
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    solver = HCaptchaSolver(page, options=options(max_rounds=1), vision_client=client, log=logs.append)
    solver._network_task_type = "point"

    result = run(solver.solve())
    run(solver.aclose())

    rendered = "\n".join(logs)
    assert "model=probe-model" in rendered
    assert "no target" in rendered, "必须记录模型实际返回内容"
    assert result.failure_stage == "vision_schema", "应能与传输失败区分"


def test_model_id_and_endpoint_are_logged_on_every_call() -> None:
    """必须能从日志直接看出用的是哪个模型，否则无法判断端点/模型是否配错。"""

    class NamedVision:
        config = SimpleNamespace(
            model="my-vision-model", base_url="https://vision.example/v1", timeout=60.0
        )

        async def solve_hcaptcha(self, **_request: Any) -> dict[str, Any]:
            return {"challenge_type": "point", "confidence": 0.9, "points": [{"x": 500, "y": 500}]}

    logs: list[str] = []
    frame = challenge_frame()
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "tok")],
    )
    solver = HCaptchaSolver(
        page, options=options(post_action_wait_ms=20), vision_client=NamedVision(), log=logs.append
    )
    solver._network_task_type = "point"

    run(solver.solve())
    run(solver.aclose())

    rendered = "\n".join(logs)
    assert "model=my-vision-model" in rendered
    assert "endpoint=https://vision.example/v1" in rendered
    assert "模型原始返回" in rendered


def test_uploads_are_compressed_and_media_type_matches() -> None:
    """全尺寸 PNG 会让端点超时；上传前必须压缩，且 media_type 要与实际格式一致。"""
    from io import BytesIO

    from PIL import Image

    raw = _png(1200, 900, (200, 30, 30))
    compressed, media_type = compress_for_vision(raw)

    assert len(compressed) < len(raw)
    assert media_type == "image/jpeg"
    with Image.open(BytesIO(compressed)) as decoded:
        assert max(decoded.size) <= 640, "长边应被压到上限内"


def test_compression_falls_back_on_unusable_input() -> None:
    assert compress_for_vision(b"") == (b"", "image/png")
    assert compress_for_vision(b"not-an-image") == (b"not-an-image", "image/png")


def test_compression_can_be_disabled() -> None:
    frame = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]))
    solver = HCaptchaSolver(page, options=options(compress_uploads=False), vision_client=FakeVision([]))
    solver._network_task_type = "point"

    request = run(solver._capture_round(frame, 1))
    run(solver.aclose())

    assert request["media_type"] == "image/png"
    assert request["image"] == _png(200, 160, (250, 250, 250))


def test_empty_capture_never_reaches_the_model() -> None:
    """截图为空时不得发起视觉调用：模型只会回「图中没有目标」，白费一次调用。"""

    class BlankPage(FakePage):
        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return b""

    # 元素截图同样为空，模拟挑战区域整体不可截（换帧中/已收起）。
    frame = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="click the target", image=b""),
            ".challenge-view": FakeLocator(box={"x": 0, "y": 0, "width": 300, "height": 300}, image=b""),
            ".challenge-container": FakeLocator(box={"x": 0, "y": 0, "width": 300, "height": 300}, image=b""),
            "body": FakeLocator(box={"x": 0, "y": 0, "width": 300, "height": 300}, image=b""),
        },
    )
    page = BlankPage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    vision = FakeVision([{"type": "point", "confidence": 0.9, "points": [{"x": 1, "y": 1}]}])
    solver = HCaptchaSolver(page, options=options(), vision_client=vision)
    solver._network_task_type = "point"

    result = run(solver.solve())
    run(solver.aclose())

    assert vision.calls == [], "空截图不应发起模型调用"
    assert result.status == "failed"
    assert result.failure_stage == "empty_capture"


def test_align_temporal_phase_waits_for_matching_final_frame() -> None:
    class SequenceScreenshotPage(FakePage):
        def __init__(self, main_frame: FakeFrame, images: list[bytes]) -> None:
            super().__init__(main_frame)
            self.images = list(images)

        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return self.images.pop(0) if len(self.images) > 1 else self.images[0]

    target_image = _png(300, 200, (30, 50, 90))
    different_image = _png(300, 200, (220, 200, 150))
    fresh = challenge_frame()
    page = SequenceScreenshotPage(
        FakeFrame("https://site.invalid", children=[fresh]),
        [different_image, target_image],
    )
    solver = HCaptchaSolver(
        page,
        options=options(
            compress_uploads=False,
            temporal_phase_wait_ms=1_000,
            poll_interval_ms=10,
        ),
    )

    frame, target, aligned = run(
        solver._align_temporal_phase(
            fresh,
            task_type="point",
            prompt="请选择所有公交车",
            target_image=target_image,
        )
    )
    run(solver.aclose())

    assert aligned is True
    assert frame is fresh
    assert target is not None
    assert len(page.page_screenshots) == 2
    assert not page.mouse.events


def test_refresh_action_context_accepts_replaced_frame_for_same_image() -> None:
    """模型返回期间 iframe 可被替换；同题时应刷新 locator，而非使用 detached 旧节点。"""

    class SequenceScreenshotPage(FakePage):
        def __init__(self, main_frame: FakeFrame, images: list[bytes]) -> None:
            super().__init__(main_frame)
            self.images = list(images)

        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return self.images.pop(0) if len(self.images) > 1 else self.images[0]

    image = coordinate_grid_overlay(_png(300, 200, (90, 120, 160)))
    old = challenge_frame()
    fresh = challenge_frame()
    page = SequenceScreenshotPage(FakeFrame("https://site.invalid", children=[fresh]), [image, image])
    solver = HCaptchaSolver(page, options=options(compress_uploads=False))
    request = run(solver._capture_round(old, 1))

    frame, target, same = run(
        solver._refresh_action_context(
            old,
            task_type=str(request["task_type"]),
            prompt=str(request["prompt"]),
            original_fingerprint=str(request["challenge_fingerprint"]),
        )
    )
    run(solver.aclose())

    assert same is True
    assert frame is fresh
    assert target is not None


def test_refresh_action_context_rejects_new_challenge_image() -> None:
    """模型返回时已换题必须丢弃旧坐标，不能把旧答案点击到新题。"""

    class SequenceScreenshotPage(FakePage):
        def __init__(self, main_frame: FakeFrame, images: list[bytes]) -> None:
            super().__init__(main_frame)
            self.images = list(images)

        async def screenshot(self, **kwargs: Any) -> bytes:
            self.page_screenshots.append(kwargs)
            return self.images.pop(0) if len(self.images) > 1 else self.images[0]

    old_image = coordinate_grid_overlay(_png(300, 200, (20, 20, 20)))
    new_image = coordinate_grid_overlay(_png(300, 200, (240, 240, 240)), divisions=5)
    old = challenge_frame()
    fresh = challenge_frame()
    page = SequenceScreenshotPage(
        FakeFrame("https://site.invalid", children=[fresh]), [old_image, new_image]
    )
    solver = HCaptchaSolver(page, options=options(compress_uploads=False))
    request = run(solver._capture_round(old, 1))

    frame, target, same = run(
        solver._refresh_action_context(
            old,
            task_type=str(request["task_type"]),
            prompt=str(request["prompt"]),
            original_fingerprint=str(request["challenge_fingerprint"]),
        )
    )
    run(solver.aclose())

    assert same is False
    assert frame is fresh
    assert target is not None
    assert not any(event[0] == "click" for event in page.mouse.events)


def test_second_round_waits_for_token_when_challenge_dismissed() -> None:
    """第 1 轮动作后挑战收起时，应等令牌而不是把空白图发给模型。"""

    class DismissingFrame(FakeFrame):
        """第一次探测有题面，动作执行后题面消失（模拟挑战收起）。"""

        def __init__(self, url: str, selectors: dict[str, FakeLocator]) -> None:
            super().__init__(url, selectors)
            self._live_selectors = dict(selectors)
            self.dismissed = False

        def locator(self, selector: str) -> FakeLocator:
            if self.dismissed and selector in {".prompt-text", ".task-image"}:
                return EmptyLocator()
            return self._live_selectors.get(selector, EmptyLocator())

    frame = DismissingFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {
            ".prompt-text": FakeLocator(text="click the target"),
            ".challenge-view": FakeLocator(box={"x": 0, "y": 0, "width": 300, "height": 300}),
            ".challenge-container": FakeLocator(box={"x": 0, "y": 0, "width": 300, "height": 300}),
            "button.button-submit": FakeLocator(box={"x": 10, "y": 320, "width": 60, "height": 30}),
        },
    )
    # 第 1 轮读到空 token，动作后签发 token。
    page = FakePage(
        FakeFrame("https://site.invalid", children=[frame]),
        responses=[(True, ""), (True, ""), (True, "second-round-token")],
    )
    vision = FakeVision([{"type": "point", "confidence": 0.95, "points": [{"x": 500, "y": 500}]}])
    solver = HCaptchaSolver(page, options=options(max_rounds=2, post_action_wait_ms=40), vision_client=vision)
    solver._network_task_type = "point"

    original_apply = solver._apply_point

    async def apply_then_dismiss(*args: Any, **kwargs: Any) -> bool:
        applied = await original_apply(*args, **kwargs)
        frame.dismissed = True
        return applied

    solver._apply_point = apply_then_dismiss  # type: ignore[method-assign]

    result = run(solver.solve())
    run(solver.aclose())

    assert len(vision.calls) == 1, "挑战收起后不应再发起第 2 轮模型调用"
    assert result.status == "success"
    assert result.token == "second-round-token"


def test_dismissed_challenge_restarts_explicitly_unchecked_checkbox_once() -> None:
    """挑战收起且 aria-checked=false 时只重启一次，并可接收重启后的 token。"""

    class AriaLocator(FakeLocator):
        async def get_attribute(self, name: str) -> str | None:
            return "false" if name == "aria-checked" else None

    checkbox = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": AriaLocator()},
    )
    live = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[checkbox, live]))
    solver = HCaptchaSolver(
        page,
        options=options(
            max_rounds=3,
            round_timeout_ms=2_000,
            post_action_wait_ms=0,
            widget_mount_timeout_ms=0,
        ),
        vision_client=FakeVision([]),
    )
    live_state = True
    progress_calls = 0
    click_calls = 0

    async def locate() -> tuple[bool, str, Any | None, Any | None]:
        return True, "", None, live

    async def challenge_is_live(_frame: Any) -> bool:
        return live_state

    async def capture_round(_frame: Any, round_number: int) -> dict[str, Any]:
        image = _png(100, 100, (80, 90, 100))
        return {
            "round": round_number,
            "prompt": "click the target",
            "task_type": "point",
            "image": image,
            "media_type": "image/png",
            "tiles": [],
            "action_target": FakeLocator(),
            "challenge_fingerprint": _image_fingerprint(image),
        }

    async def ask_vision(_request: Any) -> dict[str, Any]:
        return {"challenge_type": "point", "confidence": 1, "points": [{"x": 500, "y": 500}]}

    async def refresh_context(*_args: Any, **_kwargs: Any) -> tuple[Any, Any, bool]:
        return live, FakeLocator(), True

    async def apply_answer(*_args: Any, **_kwargs: Any) -> str:
        nonlocal live_state
        live_state = False
        return "applied"

    async def wait_for_progress() -> tuple[str, Any | None, bool]:
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls < 3:
            return "", live, False
        return "reset-token", None, True

    async def find_frames() -> tuple[Any, Any]:
        return checkbox, live

    async def click_checkbox(_frame: Any) -> bool:
        nonlocal click_calls
        click_calls += 1
        return True

    solver._locate = locate  # type: ignore[method-assign]
    solver._challenge_is_live = challenge_is_live  # type: ignore[method-assign]
    solver._capture_round = capture_round  # type: ignore[method-assign]
    solver._ask_vision = ask_vision  # type: ignore[method-assign]
    solver._refresh_action_context = refresh_context  # type: ignore[method-assign]
    solver._apply_answer = apply_answer  # type: ignore[method-assign]
    solver._wait_for_progress = wait_for_progress  # type: ignore[method-assign]
    solver._find_frames = find_frames  # type: ignore[method-assign]
    solver._click_checkbox = click_checkbox  # type: ignore[method-assign]

    result = run(solver.solve())
    run(solver.aclose())

    assert result.status == "success"
    assert result.token == "reset-token"
    assert click_calls == 1


def test_blocking_dom_probe_cannot_consume_whole_budget() -> None:
    """detached 框架上的阻塞探测必须自带超时，否则会吃光预算并掩盖真实原因。"""

    class HangingLocator(FakeLocator):
        async def count(self) -> int:
            await asyncio.sleep(30)
            return 1

    class HangingFrame(FakeFrame):
        def locator(self, _selector: str) -> FakeLocator:
            return HangingLocator()

    frame = HangingFrame("https://newassets.hcaptcha.com/captcha/v1/challenge", {})
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), widget_present=True)
    solver = HCaptchaSolver(page, options=options(presence_timeout_ms=0, widget_mount_timeout_ms=0))

    # 用 asyncio.run + monotonic 计时：自建 loop 会污染同进程后续用例（实测让
    # 别的用例随机报 timeout，失败点随执行顺序漂移），这里不需要共享 loop。
    import time as _time

    async def _scenario() -> Any:
        try:
            return await solver.solve()
        finally:
            await solver.aclose()

    began = _time.monotonic()
    result = run(_scenario())
    elapsed = _time.monotonic() - began

    # 单次探测上限 2s；若无上限这里会阻塞 30s。
    assert elapsed < 15, f"探测未受超时约束，耗时 {elapsed:.1f}s"
    assert result.status in {"timeout", "failed", "not_present"}


def test_detached_frames_are_skipped_cheaply() -> None:
    probes: list[str] = []

    class TrackingFrame(FakeFrame):
        def __init__(self, url: str, detached: bool) -> None:
            super().__init__(url, {})
            self._detached = detached

        def is_detached(self) -> bool:
            return self._detached

        def locator(self, selector: str) -> FakeLocator:
            probes.append(selector)
            return EmptyLocator()

    detached = TrackingFrame("https://newassets.hcaptcha.com/captcha/v1/challenge", True)
    page = FakePage(FakeFrame("https://site.invalid", children=[detached]))
    solver = HCaptchaSolver(page, options=options())

    run(solver._find_frames())
    run(solver.aclose())

    assert probes == [], "detached 框架不应被逐个探测"


def test_widget_mount_timeout_reports_stage() -> None:
    """挂载始终不完成时给出可定位的失败阶段，而不是模糊的未等到令牌。"""
    page = FakePage(
        FakeFrame("https://site.invalid"),
        responses=[(True, "")],
        widget_present=True,
    )

    result = run(solve(page, options=options(presence_timeout_ms=0, widget_mount_timeout_ms=0)))

    assert result.status == "timeout"
    assert result.failure_stage == "widget_mount"
    assert "挂载超时" in result.message


def test_no_widget_still_reports_not_present() -> None:
    page = FakePage(FakeFrame("https://site.invalid"), responses=[(False, "")], widget_present=False)

    result = run(solve(page, options=options(presence_timeout_ms=0, widget_mount_timeout_ms=0)))

    assert result.status == "not_present"


def test_checkbox_falls_back_to_locator_click_when_coordinates_disappear() -> None:
    """iframe 在定位后替换时 bounding_box 可为空，应重新定位并用 locator.click。"""

    class ClickableNoBox(FakeLocator):
        def __init__(self) -> None:
            super().__init__()
            self.clicks = 0

        async def bounding_box(self) -> None:
            return None

        async def click(self, *, timeout: int) -> None:
            assert timeout == 5_000
            self.clicks += 1

    checkbox_locator = ClickableNoBox()
    checkbox = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": checkbox_locator},
    )
    page = FakePage(FakeFrame("https://site.invalid", children=[checkbox]))
    solver = HCaptchaSolver(page, options=options())

    clicked = run(solver._click_checkbox(checkbox))
    run(solver.aclose())

    assert clicked is True
    assert checkbox_locator.clicks == 1


def test_checkbox_is_retried_once_when_first_click_produces_no_challenge() -> None:
    """首次点击静默失效时重试一次；第二次出现题面后继续求解。"""
    empty = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {".challenge-container": FakeLocator(box={"x": 10, "y": 10, "width": 376, "height": 190})},
    )
    checkbox = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": FakeLocator(box={"x": 20, "y": 30, "width": 28, "height": 28})},
    )
    live = challenge_frame()
    page = FakePage(FakeFrame("https://site.invalid", children=[checkbox, empty]))
    solver = HCaptchaSolver(
        page,
        options=options(post_action_wait_ms=0, widget_mount_timeout_ms=0),
        vision_client=FakeVision(
            [{"type": "point", "confidence": 0.9, "points": [{"x": 500, "y": 500}]}]
        ),
    )
    click_calls = 0
    progress_calls = 0
    live_calls = 0

    async def click_checkbox(_frame: Any) -> bool:
        nonlocal click_calls
        click_calls += 1
        return True

    async def wait_for_progress() -> tuple[str, Any | None, bool]:
        nonlocal progress_calls
        progress_calls += 1
        if progress_calls < 3:
            return "", empty, False
        return "retry-token", None, True

    async def wait_for_live(_frame: Any, *, timeout_ms: int | None = None) -> Any | None:
        nonlocal live_calls
        live_calls += 1
        return None if live_calls == 1 else live

    solver._click_checkbox = click_checkbox  # type: ignore[method-assign]
    solver._wait_for_progress = wait_for_progress  # type: ignore[method-assign]
    solver._wait_for_live_challenge = wait_for_live  # type: ignore[method-assign]

    result = run(solver.solve())
    run(solver.aclose())

    assert click_calls == 2
    assert result.status == "success"
    assert result.token == "retry-token"


def test_prerendered_empty_challenge_still_clicks_checkbox() -> None:
    """空壳 challenge iframe 不得跳过复选框点击，也不得把黑图发给模型。"""
    empty = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {".challenge-container": FakeLocator(box={"x": 10, "y": 10, "width": 376, "height": 190})},
    )
    checkbox = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": FakeLocator(box={"x": 20, "y": 30, "width": 28, "height": 28})},
    )
    page = FakePage(
        FakeFrame("https://site.invalid", children=[checkbox, empty]),
        responses=[(True, ""), (True, ""), (True, "checkbox-token")],
    )
    vision = FakeVision([])

    result = run(solve(page, options=options(post_action_wait_ms=20), vision_client=vision))

    assert any(event[0] == "click" for event in page.mouse.events)
    assert vision.calls == []
    assert result.status == "success"
    assert result.token == "checkbox-token"


def test_first_challenge_uses_widget_mount_budget_after_checkbox_click() -> None:
    """首次题面挂载使用长预算，不能只等短的 post_action 窗口。"""
    empty = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {".challenge-container": FakeLocator(box={"x": 10, "y": 10, "width": 376, "height": 190})},
    )
    checkbox = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/checkbox",
        {"#checkbox": FakeLocator(box={"x": 20, "y": 30, "width": 28, "height": 28})},
    )
    live = challenge_frame()
    page = FakePage(
        FakeFrame("https://site.invalid", children=[checkbox, empty]),
        responses=[(True, ""), (True, ""), (True, "first-challenge-token")],
    )
    solver = HCaptchaSolver(
        page,
        options=options(
            post_action_wait_ms=20,
            widget_mount_timeout_ms=40_000,
        ),
        vision_client=FakeVision(
            [{"type": "point", "confidence": 0.9, "points": [{"x": 500, "y": 500}]}]
        ),
    )
    waits: list[int | None] = []
    progress_calls = 0

    async def wait_for_progress() -> tuple[str, Any | None, bool]:
        nonlocal progress_calls
        progress_calls += 1
        # 点击复选框后尚未签发 token，先出现题面；答题后再签发 token。
        if progress_calls == 1:
            return "", empty, False
        return "first-challenge-token", None, True

    async def wait_for_live(_frame: Any, *, timeout_ms: int | None = None) -> Any:
        waits.append(timeout_ms)
        return live

    solver._wait_for_progress = wait_for_progress  # type: ignore[method-assign]
    solver._wait_for_live_challenge = wait_for_live  # type: ignore[method-assign]

    result = run(solver.solve())
    run(solver.aclose())

    assert waits == [40_000]
    assert result.status == "success"
    assert result.token == "first-challenge-token"


def test_empty_challenge_without_checkbox_waits_then_reports_mount_timeout() -> None:
    """只有空壳、没有 checkbox 时应等待挂载而非立刻断言未加载。

    空壳 challenge iframe 常比 checkbox iframe 先可定位。若此时就结束，会跳过
    复选框点击并误报「挑战未加载」（实测使整轮失败）。等到预算耗尽再报挂载超时，
    既不误判也留下可定位的失败阶段。
    """
    empty = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {".challenge-container": FakeLocator(box={"x": 10, "y": 10, "width": 376, "height": 190})},
    )
    page = FakePage(FakeFrame("https://site.invalid", children=[empty]), responses=[(True, "")])
    vision = FakeVision([])

    result = run(
        solve(
            page,
            options=options(presence_timeout_ms=0, widget_mount_timeout_ms=0),
            vision_client=vision,
        )
    )

    assert result.status == "timeout"
    assert result.failure_stage == "widget_mount"
    assert vision.calls == []


def test_vision_transport_error_reports_safe_stage_and_status() -> None:
    class FailingVision:
        async def solve_hcaptcha(self, **_request: Any) -> dict[str, Any]:
            raise VisionClientError("secret-key rejected upstream", status=424)

    frame = challenge_frame(tiles=2)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])

    result = run(solve(page, options=options(max_rounds=1), vision_client=FailingVision()))

    assert result.status == "failed"
    assert result.failure_stage == "vision_request"
    assert result.error_type == "VisionClientError"
    assert result.http_status == 424
    assert "424" in result.message
    assert "secret-key" not in result.message
    assert "secret-key" not in repr(result)


def test_page_clip_screenshot_avoids_black_iframe_element_capture() -> None:
    frame = challenge_frame(tiles=1)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    solver = HCaptchaSolver(page, options=options(), vision_client=FakeVision([]))

    request = run(solver._capture_round(frame, 1))

    # 压缩后不再是原始 PNG 字节，但必须是可解码的同尺寸图像。
    assert request["image"]
    assert request["media_type"] in {"image/jpeg", "image/png"}
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(request["image"])) as decoded:
        assert decoded.size == (200, 160)
    assert page.page_screenshots
    assert page.page_screenshots[0]["clip"] == {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0}
    run(solver.aclose())


def test_failure_screenshot_callback_and_model_output_is_logged() -> None:
    """失败截图走回调，且模型返回内容完整入日志（排障需要），但不含图像字节。"""
    saved: list[str] = []
    logs: list[str] = []
    frame = challenge_frame(tiles=1)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    vision = FakeVision([{"type": "grid", "confidence": 0.1, "actions": [1], "note": "model-note"}])

    async def screenshot(name: str, *, target: Any = None) -> str:
        assert target is not None
        saved.append(name)
        return f"saved/{name}"

    result = run(
        solve(page, options=options(), vision_client=vision, log=logs.append, screenshot=screenshot)
    )

    assert result.status == "uncertain"
    assert result.challenge_type == "grid"
    assert result.screenshot == "saved/hcaptcha-uncertain.png"
    assert saved == ["hcaptcha-uncertain.png"]
    rendered = "\n".join(logs)
    # 模型原文按需求完整输出，便于判断是模型判错还是动作执行错。
    assert "model-note" in rendered
    assert "模型原始返回" in rendered
    # 图像字节永不入日志。
    assert "\\x89PNG" not in rendered and "\xff\xd8\xff" not in rendered


def test_api_key_is_masked_even_when_echoed_by_server() -> None:
    """服务端把 key 回显在错误正文里时，日志与异常都必须已脱敏。"""
    from browser.openai_vision import OpenAIVisionClient, VisionClientConfig, VisionClientError
    from providers.base import ApiError

    def transport(_url: str, **_kwargs: Any):
        raise ApiError(401, {"message": "Bearer sk-live-abcdef1234567890 invalid"}, "rejected")

    client = OpenAIVisionClient(
        VisionClientConfig(
            api_key="sk-live-abcdef1234567890",
            base_url="https://v.example/v1",
            model="vision-model",
        ),
        transport=transport,
    )

    try:
        run(client.analyze(b"image"))
        raise AssertionError("should have raised")
    except VisionClientError as exc:
        text = str(exc)

    assert "sk-live-abcdef1234567890" not in text
    assert "401" in text
