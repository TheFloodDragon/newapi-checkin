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
    _validated_answer,
    compress_for_vision,
    coordinate_grid_overlay,
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
    assert ("move", 100.0, 200.0, 5) in page.mouse.events
    assert ("move", 500.0, 500.0, 15) in page.mouse.events


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
