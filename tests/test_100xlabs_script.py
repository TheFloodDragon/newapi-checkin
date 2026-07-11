from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "checkin" / "100xlabs.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("test_100xlabs_browser_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


class FakeElement:
    def __init__(
        self,
        text: str,
        *,
        role: str = "",
        visible: bool = True,
        disabled: bool = False,
        on_click: Callable[["FakePage", "FakeElement"], None] | None = None,
    ) -> None:
        self.text = text
        self.role = role
        self.visible = visible
        self.disabled = disabled
        self.on_click = on_click


class FakeElementHandle:
    def __init__(self, element: FakeElement) -> None:
        self.element = element

    async def is_visible(self) -> bool:
        return self.element.visible


class FakeLocator:
    def __init__(self, page: "FakePage", *, role: str = "", text: str = "") -> None:
        self.page = page
        self.role = role
        self.text = text

    @property
    def first(self) -> "FakeLocator":
        return self

    def _element(self) -> FakeElement | None:
        needle = self.text.casefold()
        for element in self.page.elements:
            if self.role and element.role != self.role:
                continue
            if needle not in element.text.casefold():
                continue
            return element
        return None

    async def is_visible(self) -> bool:
        element = self._element()
        return bool(element and element.visible)

    async def element_handle(self) -> FakeElementHandle | None:
        element = self._element()
        return FakeElementHandle(element) if element is not None else None

    async def is_disabled(self) -> bool:
        element = self._element()
        return bool(element and element.disabled)

    async def click(self, timeout: int = 5000) -> None:
        del timeout
        element = self._element()
        if element is None or not element.visible or element.disabled:
            raise RuntimeError("element is not clickable")
        self.page.clicked.append(element.text)
        if element.on_click is not None:
            element.on_click(self.page, element)


class FakeResponse:
    def __init__(self, status: int, url: str, method: str = "POST") -> None:
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)


class FakePage:
    def __init__(self, elements: list[FakeElement]) -> None:
        self.elements = elements
        self.clicked: list[str] = []
        self.waits: list[int] = []
        self.listeners: dict[str, list[Callable[[Any], None]]] = {}

    def get_by_role(self, role: str, *, name: str, exact: bool = False) -> FakeLocator:
        assert exact is False
        return FakeLocator(self, role=role, text=name)

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        assert exact is False
        return FakeLocator(self, text=text)

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        assert state == "domcontentloaded"
        assert timeout > 0

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)
        await asyncio.sleep(timeout / 1000)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.get(event, []).remove(callback)

    def emit_response(self, status: int, url: str = "https://example.invalid/api/check-in") -> None:
        for callback in list(self.listeners.get("response", [])):
            callback(FakeResponse(status, url))


class FakeHelpers:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.screenshots: list[str] = []

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))

    def resolve_url(self, url: str) -> str:
        return f"https://example.invalid/{url.lstrip('/')}"

    async def screenshot(self, name: str) -> str:
        self.screenshots.append(name)
        return f"results/{name}"

    @staticmethod
    def _result(status: str, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return {"status": status, "message": message, "detail": detail}

    def success(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("success", message, detail)

    def already_done(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("already_done", message, detail)

    def need_config(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("need_config", message, detail)

    def error(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("error", message, detail)


def _run(page: FakePage, script_args: dict[str, Any] | None = None) -> tuple[dict[str, Any], FakeHelpers]:
    helpers = FakeHelpers()
    site = SimpleNamespace(script_args=script_args or {})
    result = asyncio.run(SCRIPT.run(page, None, site, helpers))
    return result, helpers


def _emit_success(page: FakePage, element: FakeElement) -> None:
    del element
    page.emit_response(200)


def test_clicks_checkin_now_and_now_buttons() -> None:
    for label in ("Check in now", "now"):
        page = FakePage([FakeElement(label, role="button", on_click=_emit_success)])

        result, _ = _run(page)

        assert result["status"] == "success"
        assert result["detail"]["completion_signal"] == "checkin_response"
        assert page.clicked == [label]
        assert page.waits == []


def test_prefers_clickable_control_over_matching_page_text() -> None:
    page = FakePage(
        [
            FakeElement("now"),
            FakeElement("Claim", role="button", on_click=_emit_success),
        ]
    )

    result, _ = _run(page)

    assert result["status"] == "success"
    assert page.clicked == ["Claim"]
    assert result["detail"]["clicked_kind"] == "button"


def test_disabled_today_button_exits_as_already_done_before_click() -> None:
    page = FakePage([FakeElement("今日已签到", role="button", disabled=True)])

    result, _ = _run(page)

    assert result["status"] == "already_done"
    assert result["detail"]["completion_signal"] == "button_state"
    assert page.clicked == []
    assert page.listeners == {}


def test_button_switches_to_disabled_today_state_and_exits_immediately() -> None:
    def finish(_page: FakePage, element: FakeElement) -> None:
        element.text = "今日已签到"
        element.disabled = True

    page = FakePage([FakeElement("Check in now", role="button", on_click=finish)])

    result, _ = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "button_state"
    assert result["detail"]["matched_text"] in {"已签到", "今日已签到"}
    assert page.waits == []


def test_success_prompt_exits_immediately() -> None:
    def show_success(page: FakePage, _element: FakeElement) -> None:
        page.elements.append(FakeElement("签到成功"))

    page = FakePage([FakeElement("签到", role="button", on_click=show_success)])

    result, _ = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "success_text"
    assert page.waits == []


def test_hidden_button_is_treated_as_completion() -> None:
    def hide(_page: FakePage, element: FakeElement) -> None:
        element.visible = False

    page = FakePage([FakeElement("now", role="button", on_click=hide)])

    result, _ = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "button_hidden"


def test_button_text_change_without_completion_signal_is_not_false_success() -> None:
    def show_loading(_page: FakePage, element: FakeElement) -> None:
        element.text = "Loading..."

    page = FakePage([FakeElement("now", role="button", on_click=show_loading)])

    result, helpers = _run(page, {"completion_timeout_ms": 25, "poll_interval_ms": 20})

    assert result["status"] == "error"
    assert helpers.screenshots == ["100xlabs-after-click.png"]


def test_failed_checkin_response_returns_error() -> None:
    def fail(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(503, "https://example.invalid/api/checkin")

    page = FakePage([FakeElement("签到", role="button", on_click=fail)])

    result, _ = _run(page)

    assert result["status"] == "error"
    assert result["detail"]["response_status"] == 503
    assert result["detail"]["completion_signal"] == "checkin_response"


def test_missing_completion_signal_returns_error_and_screenshot() -> None:
    page = FakePage([FakeElement("签到", role="button")])

    result, helpers = _run(page, {"completion_timeout_ms": 25, "poll_interval_ms": 20})

    assert result["status"] == "error"
    assert "未检测到签到完成信号" in result["message"]
    assert result["detail"]["screenshot"] == "results/100xlabs-after-click.png"
    assert helpers.screenshots == ["100xlabs-after-click.png"]
    assert page.waits


def test_custom_script_texts_and_target_path_remain_supported() -> None:
    def finish(page: FakePage, _element: FakeElement) -> None:
        page.elements.append(FakeElement("Done!"))

    page = FakePage([FakeElement("Collect bonus", on_click=finish)])
    result, helpers = _run(
        page,
        {
            "checkin_text": "Collect bonus",
            "success_text": ["Done!"],
            "start_path": "/daily",
        },
    )

    assert result["status"] == "success"
    assert result["detail"]["clicked_kind"] == "text"
    assert page.clicked == ["Collect bonus"]
    assert helpers.goto_calls[0][0] == "/daily"
