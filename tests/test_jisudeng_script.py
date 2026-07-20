from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "checkin" / "jisudeng.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("test_jisudeng_browser_script", SCRIPT_PATH)
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
        normal_click_failures: int = 0,
    ) -> None:
        self.text = text
        self.role = role
        self.visible = visible
        self.disabled = disabled
        self.on_click = on_click
        self.normal_click_failures = normal_click_failures


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
            if needle in element.text.casefold():
                return element
        return None

    async def is_visible(self) -> bool:
        element = self._element()
        return bool(element and element.visible)

    async def is_disabled(self) -> bool:
        element = self._element()
        return bool(element and element.disabled)

    async def scroll_into_view_if_needed(self, timeout: int = 5000) -> None:
        del timeout

    async def click(self, timeout: int = 5000, force: bool = False) -> None:
        del timeout
        element = self._element()
        if element is None or not element.visible or element.disabled:
            raise RuntimeError("element is not clickable")
        if not force and element.normal_click_failures > 0:
            element.normal_click_failures -= 1
            raise RuntimeError("element is temporarily covered")
        self.page.clicked.append(element.text)
        if element.on_click is not None:
            element.on_click(self.page, element)

    async def dispatch_event(self, event: str) -> None:
        assert event == "click"
        await self.click(force=True)

    async def evaluate(self, _expression: str) -> None:
        await self.click(force=True)


class FakeResponse:
    def __init__(self, status: int, url: str, method: str = "POST") -> None:
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)


class FakePage:
    def __init__(self, elements: list[FakeElement], *, url: str = "https://www.jisudeng.com/check-in") -> None:
        self.elements = elements
        self.url = url
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
        assert state in {"domcontentloaded", "networkidle"}
        assert timeout > 0

    async def wait_for_timeout(self, timeout: int) -> None:
        self.waits.append(timeout)
        await asyncio.sleep(timeout / 1000)

    def on(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.setdefault(event, []).append(callback)

    def remove_listener(self, event: str, callback: Callable[[Any], None]) -> None:
        self.listeners.get(event, []).remove(callback)

    def emit_response(self, status: int, url: str = "https://www.jisudeng.com/api/v1/play/checkin") -> None:
        for callback in list(self.listeners.get("response", [])):
            callback(FakeResponse(status, url))


class FakeHelpers:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.screenshots: list[str] = []

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))

    def resolve_url(self, url: str) -> str:
        return f"https://www.jisudeng.com/{url.lstrip('/')}"

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

    def need_login(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("need_login", message, detail)

    def need_config(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("need_config", message, detail)

    def error(self, message: str, detail: dict[str, Any]) -> dict[str, Any]:
        return self._result("error", message, detail)


def _run(page: FakePage, script_args: dict[str, Any] | None = None) -> tuple[dict[str, Any], FakeHelpers]:
    helpers = FakeHelpers()
    site = SimpleNamespace(script_args=script_args or {})
    result = asyncio.run(SCRIPT.run(page, None, site, helpers))
    return result, helpers


def test_clicks_checkin_button_and_accepts_success_response() -> None:
    def finish(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(200)

    page = FakePage([FakeElement("立即签到", role="button", on_click=finish)])

    result, helpers = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "checkin_response"
    assert page.clicked == ["立即签到"]
    assert helpers.goto_calls[0][0] == "/check-in"
    assert page.listeners == {"response": []}


def test_already_done_state_does_not_click() -> None:
    page = FakePage([FakeElement("今日已签到", role="button", disabled=True)])

    result, _ = _run(page)

    assert result["status"] == "already_done"
    assert result["detail"]["completion_signal"] == "already_state"
    assert page.clicked == []


def test_login_redirect_returns_need_login() -> None:
    page = FakePage(
        [FakeElement("欢迎回来")],
        url="https://www.jisudeng.com/login?redirect=/check-in",
    )

    result, _ = _run(page)

    assert result["status"] == "need_login"
    assert "Turnstile" in result["message"]
    assert page.clicked == []


def test_success_text_after_click_is_accepted() -> None:
    def show_success(page: FakePage, _element: FakeElement) -> None:
        page.elements.append(FakeElement("已到账 $0.25"))

    page = FakePage([FakeElement("立即签到", role="button", on_click=show_success)])

    result, _ = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "success_text"
    assert result["detail"]["matched_text"] == "已到账"


def test_failed_checkin_response_returns_error() -> None:
    def fail(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(503)

    page = FakePage([FakeElement("立即签到", role="button", on_click=fail)])

    result, _ = _run(page)

    assert result["status"] == "error"
    assert result["detail"]["response_status"] == 503


def test_missing_completion_signal_returns_error_and_screenshot() -> None:
    page = FakePage([FakeElement("立即签到", role="button")])

    result, helpers = _run(page, {"completion_timeout_ms": 25, "poll_interval_ms": 20})

    assert result["status"] == "error"
    assert "未检测到签到完成信号" in result["message"]
    assert helpers.screenshots == ["jisudeng-after-click.png"]
