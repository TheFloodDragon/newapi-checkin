# -*- coding: utf-8 -*-
"""hCaptcha 核心离线测试：仅使用 Fake Page/Frame/Locator/Mouse。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from browser.hcaptcha import HCaptchaOptions, HCaptchaSolveResult, HCaptchaSolver, solve
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
    ) -> None:
        self.main_frame = main_frame
        self.frames = [main_frame]
        self.mouse = FakeMouse()
        self.responses = list(responses or [(False, "")])
        self.listeners: dict[str, list[Callable[..., Any]]] = {}
        self.page_screenshots: list[dict[str, Any]] = []

    async def screenshot(self, **kwargs: Any) -> bytes:
        self.page_screenshots.append(kwargs)
        return b"page-png"

    def on(self, event: str, callback: Callable[..., Any]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable[..., Any]) -> None:
        self.listeners[event].remove(callback)

    async def evaluate(self, _script: str) -> dict[str, Any]:
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


def test_empty_challenge_without_checkbox_reports_not_loaded() -> None:
    empty = FakeFrame(
        "https://newassets.hcaptcha.com/captcha/v1/challenge",
        {".challenge-container": FakeLocator(box={"x": 10, "y": 10, "width": 376, "height": 190})},
    )
    page = FakePage(FakeFrame("https://site.invalid", children=[empty]), responses=[(True, "")])
    vision = FakeVision([])

    result = run(solve(page, options=options(), vision_client=vision))

    assert result.status == "failed"
    assert result.message == "hCaptcha 挑战未加载"
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

    assert request["image"] == b"page-png"
    assert page.page_screenshots
    assert page.page_screenshots[0]["clip"] == {"x": 100.0, "y": 200.0, "width": 400.0, "height": 300.0}
    run(solver.aclose())


def test_failure_screenshot_callback_and_safe_logs() -> None:
    saved: list[str] = []
    logs: list[str] = []
    frame = challenge_frame(tiles=1)
    page = FakePage(FakeFrame("https://site.invalid", children=[frame]), responses=[(True, "")])
    vision = FakeVision([{"type": "grid", "confidence": 0.1, "actions": [1], "raw": "model-secret"}])

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
    assert "model-secret" not in rendered
    assert "png" not in rendered
