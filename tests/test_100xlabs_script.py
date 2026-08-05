from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
import accounts_store


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "checkin" / "100xlabs.py"


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("test_100xlabs_browser_script", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCRIPT = _load_script()


def test_boolean_option_uses_default_for_null() -> None:
    assert SCRIPT.common._as_bool(None, True) is True
    assert SCRIPT.common._as_bool(None, False) is False
    assert SCRIPT.common._as_bool("false", True) is False
    assert SCRIPT.common._as_bool("yes", False) is True


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


class FakeMouse:
    """模拟 Playwright 的真实鼠标 API；click 会通知所属 page 累加点击计数。"""

    def __init__(self) -> None:
        self.page: "FakePage | None" = None
        self.moves: list[tuple[float, float]] = []
        self.clicks: list[tuple[float, float]] = []

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y))

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        if self.page is not None:
            self.page.turnstile_clicked += 1


class FakeResponse:
    def __init__(
        self,
        status: int,
        url: str,
        method: str = "POST",
        body: Any = None,
    ) -> None:
        self.status = status
        self.url = url
        self.request = SimpleNamespace(method=method)
        self._body = body

    async def json(self) -> Any:
        """签到响应体。body=None 时模拟「响应不是 JSON」，与真实站点一致地抛错。"""
        if self._body is None:
            raise ValueError("not json")
        return self._body


class FakePage:
    def __init__(
        self,
        elements: list[FakeElement],
        *,
        url: str = "https://example.invalid/check-in",
        has_password_field: bool = False,
        authenticated: bool = True,
        turnstile_token: str = "",
        form_available: bool = True,
        login_result: dict[str, Any] | None = None,
        api_checkin_result: dict[str, Any] | None = None,
        api_checkin_result_after_login: dict[str, Any] | None = None,
        status_result: dict[str, Any] | None = None,
    ) -> None:
        self.elements = elements
        self.url = url
        self.clicked: list[str] = []
        self.waits: list[int] = []
        self.listeners: dict[str, list[Callable[[Any], None]]] = {}
        self.api_checkin_result = api_checkin_result or {
            "ok": False,
            "status": 404,
            "already": False,
            "code": "",
            "message": "not found",
        }
        self.api_checkin_result_after_login = api_checkin_result_after_login
        self.api_checkin_requests = 0
        # 只读签到状态端点 GET /api/v1/check-in/status 的返回；None 表示端点不可用。
        self.status_result = status_result
        self.status_requests = 0
        self._login_succeeded = False
        # 密码登录兜底相关（仅登录闸门测试用）。
        self.has_password_field = has_password_field
        self.authenticated = authenticated
        self.turnstile_token = turnstile_token
        self.form_available = form_available
        self.login_result = login_result or {
            "ok": False,
            "status": 401,
            "two_factor": False,
            "message": "",
        }
        self.login_requests: list[list[Any]] = []
        self.prepared_login: list[str] = []
        # Turnstile 真实鼠标点击相关。
        self.mouse = FakeMouse()
        self.mouse.page = self
        # 需点击几次才签发令牌（模拟交互式 widget）；0 表示被动即可读到。
        self.turnstile_clicks_needed = 0
        self.turnstile_clicked = 0

    async def evaluate(self, expression: str, arg: Any = None) -> Any:
        if "/api/v1/check-in/status'" in expression:
            # 只读状态查询 GET /api/v1/check-in/status（必须排在签到端点判断之前，
            # 否则 "/api/v1/check-in" 前缀会先命中签到分支）。
            self.status_requests += 1
            return self.status_result
        if "/api/v1/check-in'" in expression:
            # 签到接口 POST /api/v1/check-in（注意排在 auth/me 等更长路径判断之前）。
            self.api_checkin_requests += 1
            if self._login_succeeded and self.api_checkin_result_after_login is not None:
                return self.api_checkin_result_after_login
            return self.api_checkin_result
        if "/api/v1/auth/me" in expression:
            return bool(self.authenticated)
        if "getBoundingClientRect" in expression:
            # 定位 Turnstile widget box；无 token 需求时也返回一个可点区域。
            return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}
        if "cf-turnstile-response" in expression:
            if self.turnstile_clicks_needed and self.turnstile_clicked < self.turnstile_clicks_needed:
                return ""
            return self.turnstile_token
        if "HTMLInputElement.prototype" in expression:
            assert isinstance(arg, list)
            self.prepared_login = [str(v) for v in arg]
            return self.form_available
        if "/api/v1/auth/login" in expression:
            assert isinstance(arg, list)
            self.login_requests.append(arg)
            if self.login_result.get("ok"):
                self.authenticated = True
                self.has_password_field = False
                self._login_succeeded = True
                self.url = "https://example.invalid/check-in"
            return self.login_result
        if "__x100_login_reset" in expression:
            # sentinel 的开/关不改变认证态，只是控制 init script 是否清理。
            return True
        if "sub2api_site_usage_notice" in expression:
            # 关闭「使用说明」模态框：写标记 + 点确认，不改变认证态。
            return False
        if "auth_user" in expression and "removeItem" not in expression:
            # 清理后确认 auth_user 已空：清理逻辑会把 has_password_field 置真。
            return False
        if "removeItem" in expression:
            self.authenticated = False
            self.has_password_field = True
            return True
        raise AssertionError(f"unexpected page.evaluate expression: {expression[:80]}")

    def get_by_role(self, role: str, *, name: str, exact: bool = False) -> FakeLocator:
        assert exact is False
        return FakeLocator(self, role=role, text=name)

    def get_by_text(self, text: str, *, exact: bool = False) -> FakeLocator:
        assert exact is False
        return FakeLocator(self, text=text)

    def locator(self, selector: str) -> Any:
        # 登录页密码框可见性由 has_password_field 控制。
        page = self

        class _PasswordLocator:
            @property
            def first(self) -> "_PasswordLocator":
                return self

            async def is_visible(self) -> bool:
                return bool(page.has_password_field)

        return _PasswordLocator()

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

    def emit_response(
        self,
        status: int,
        url: str = "https://example.invalid/api/check-in",
        body: Any = None,
    ) -> None:
        for callback in list(self.listeners.get("response", [])):
            callback(FakeResponse(status, url, body=body))


class FakeHelpers:
    def __init__(self) -> None:
        self.goto_calls: list[tuple[str, dict[str, Any]]] = []
        self.screenshots: list[str] = []
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(str(message))

    async def goto(self, url: str, **kwargs: Any) -> None:
        self.goto_calls.append((url, kwargs))

    def resolve_url(self, url: str) -> str:
        return f"https://example.invalid/{url.lstrip('/')}"

    async def screenshot(self, name: str) -> str:
        self.screenshots.append(name)
        return f"{accounts_store.RESULTS_DIR_NAME}/{name}"

    @staticmethod
    def _result(
        status: str,
        message: str,
        detail: dict[str, Any] | None = None,
        quota: Any = None,
        awarded: Any = None,
    ) -> dict[str, Any]:
        # 与真实 ScriptHelpers._result 保持同样的额度注入语义：只在传了额度时
        # 才写标准键，并标记 quota_is_usd 避免聚合层二次换算。
        out_detail = dict(detail) if detail else {}
        if quota is not None:
            out_detail["current_quota"] = quota
        if awarded is not None:
            out_detail["quota_awarded"] = awarded
        if quota is not None or awarded is not None:
            out_detail["quota_is_usd"] = True
        return {"status": status, "message": message, "detail": out_detail or detail}

    def success(self, message, detail=None, quota=None, awarded=None):
        return self._result("success", message, detail, quota, awarded)

    def already_done(self, message, detail=None, quota=None, awarded=None):
        return self._result("already_done", message, detail, quota, awarded)

    def need_login(self, message, detail=None, quota=None, awarded=None):
        return self._result("need_login", message, detail, quota, awarded)

    def need_verification(self, message, detail=None, quota=None, awarded=None):
        return self._result("need_verification", message, detail, quota, awarded)

    def need_config(self, message, detail=None, quota=None, awarded=None):
        return self._result("need_config", message, detail, quota, awarded)

    def error(self, message, detail=None, quota=None, awarded=None):
        return self._result("error", message, detail, quota, awarded)


def _run(page: FakePage, script_args: dict[str, Any] | None = None) -> tuple[dict[str, Any], FakeHelpers]:
    helpers = FakeHelpers()
    site = SimpleNamespace(script_args=script_args or {})
    result = asyncio.run(SCRIPT.run(page, None, site, helpers))
    return result, helpers


def _emit_success(page: FakePage, element: FakeElement) -> None:
    del element
    page.emit_response(200)


def test_page_auth_operations_share_one_refresh_state_machine() -> None:
    scripts = (
        SCRIPT.common._AUTHENTICATED_JS,
        SCRIPT.common._query_status_js(SCRIPT.SPEC.status_path),
        SCRIPT.common._api_checkin_js(SCRIPT.SPEC.checkin_path),
    )

    for script in scripts:
        # 每个 page.evaluate 操作只嵌入一份共享 refresh 状态机。
        assert script.count("/api/v1/auth/refresh") == 1
        assert "let refreshAttempted = false" in script
        assert "if (refreshAttempted) return '';" in script
        assert "if (response.status === 401)" in script
        assert "if (refreshed) response = await request(token);" in script
        # refresh token 轮换时，新 token 必须与 access token 一起写回 localStorage。
        assert "localStorage.setItem('auth_token', accessToken)" in script
        assert "localStorage.setItem('refresh_token', newRefreshToken)" in script

    status_script = scripts[1]
    assert "localStorage.getItem('refresh_token')" in status_script
    assert "requestWithAuth((accessToken)" in status_script


def test_clicks_checkin_now_and_now_buttons() -> None:
    for label in ("Check in now", "now"):
        page = FakePage([FakeElement(label, role="button", on_click=_emit_success)])

        result, helpers = _run(page)

        assert result["status"] == "success"
        assert result["detail"]["completion_signal"] == "checkin_response"
        assert page.clicked == [label]
        assert page.waits == []
        assert helpers.goto_calls == [
            ("/check-in", {"timeout": 60000, "wait_until": "commit"})
        ]


def test_force_click_recovers_from_temporary_overlay() -> None:
    page = FakePage(
        [
            FakeElement(
                "签到",
                role="button",
                on_click=_emit_success,
                normal_click_failures=1,
            )
        ]
    )

    result, _ = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["click_strategy"] == "force"
    assert page.clicked == ["签到"]


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
    assert result["detail"]["screenshot"] == f"{accounts_store.RESULTS_DIR_NAME}/100xlabs-after-click.png"
    assert helpers.screenshots == ["100xlabs-after-click.png"]
    assert page.waits


def test_blank_page_uses_authenticated_api_checkin_fallback() -> None:
    page = FakePage(
        [],
        api_checkin_result={"ok": True, "status": 200, "already": False, "code": "0", "message": ""},
    )

    result, _ = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "api_fallback"
    assert result["detail"]["response_status"] == 200
    assert page.api_checkin_requests == 1


def test_blank_page_api_reports_already_done() -> None:
    page = FakePage(
        [],
        api_checkin_result={
            "ok": False,
            "status": 409,
            "already": True,
            "code": "ALREADY_CHECKED_IN",
            "message": "今日已签到",
        },
    )

    result, _ = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "already_done"
    assert result["detail"]["completion_signal"] == "api_fallback"
    assert page.api_checkin_requests == 1


def test_blank_page_api_401_then_password_login_retry_succeeds(monkeypatch: Any) -> None:
    # api 兜底 401（token+refresh 均失效）→ 账密登录成功 → 重试签到成功。
    email = "user@example.test"
    password = "not-a-real-password"
    monkeypatch.setenv("X100LABS_EMAIL", email)
    monkeypatch.setenv("X100LABS_PASSWORD", password)
    page = FakePage(
        [],
        url="https://example.invalid/check-in",
        api_checkin_result={"ok": False, "status": 401, "already": False, "code": "", "message": ""},
        api_checkin_result_after_login={"ok": True, "status": 200, "already": False, "code": "0", "message": ""},
        has_password_field=False,
        turnstile_token="real-turnstile-token",
        login_result={"ok": True, "status": 200, "two_factor": False, "message": ""},
    )

    result, _ = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "api_fallback_after_login"
    assert len(page.login_requests) == 1
    rendered = repr(result)
    assert email not in rendered
    assert password not in rendered


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


def test_login_redirect_without_credentials_returns_need_login(monkeypatch: Any) -> None:
    monkeypatch.delenv("X100LABS_EMAIL", raising=False)
    monkeypatch.delenv("X100LABS_PASSWORD", raising=False)
    page = FakePage(
        [FakeElement("签到", role="button")],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
    )

    result, _ = _run(page)

    assert result["status"] == "need_login"
    assert result["detail"]["login_fallback"] == "missing_credentials"
    assert page.clicked == []
    assert page.login_requests == []


def test_password_login_fallback_reads_credentials_from_script_args(
    monkeypatch: Any,
) -> None:
    # 环境变量缺失，凭据直接从 script_args 的 email/password 读取。
    monkeypatch.delenv("X100LABS_EMAIL", raising=False)
    monkeypatch.delenv("X100LABS_PASSWORD", raising=False)
    email = "arg@example.test"
    password = "arg-password"

    def finish(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(200)

    page = FakePage(
        [FakeElement("签到", role="button", on_click=finish)],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
        turnstile_token="real-turnstile-token",
        login_result={"ok": True, "status": 200, "two_factor": False, "message": ""},
    )

    result, _ = _run(
        page,
        {
            "button_wait_ms": 1,
            "poll_interval_ms": 20,
            "email": email,
            "password": password,
        },
    )

    assert result["status"] == "success"
    assert page.prepared_login == [email, password]
    assert page.login_requests[0][1:] == [email, password, "real-turnstile-token"]
    rendered = repr(result)
    assert email not in rendered
    assert password not in rendered


def test_password_login_fallback_signs_in_then_checks_in(monkeypatch: Any) -> None:
    email = "user@example.test"
    password = "not-a-real-password"
    monkeypatch.setenv("X100LABS_EMAIL", email)
    monkeypatch.setenv("X100LABS_PASSWORD", password)

    def finish(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(200)

    page = FakePage(
        [FakeElement("签到", role="button", on_click=finish)],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
        turnstile_token="real-turnstile-token",
        login_result={"ok": True, "status": 200, "two_factor": False, "message": ""},
    )

    result, helpers = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "success"
    assert page.prepared_login == [email, password]
    assert len(page.login_requests) == 1
    assert page.login_requests[0][1:] == [email, password, "real-turnstile-token"]
    assert [target for target, _kwargs in helpers.goto_calls] == [
        "/check-in",
        "/login?redirect=/check-in",
        "/check-in",
    ]
    rendered = repr(result)
    assert email not in rendered
    assert password not in rendered


def test_password_login_fallback_clicks_turnstile_to_obtain_token(monkeypatch: Any) -> None:
    # 交互式 Turnstile：被动读不到令牌，必须真实鼠标点击复选框后才签发。
    email = "user@example.test"
    password = "not-a-real-password"
    monkeypatch.setenv("X100LABS_EMAIL", email)
    monkeypatch.setenv("X100LABS_PASSWORD", password)

    def finish(page: FakePage, _element: FakeElement) -> None:
        page.emit_response(200)

    page = FakePage(
        [FakeElement("签到", role="button", on_click=finish)],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
        turnstile_token="real-turnstile-token",
        login_result={"ok": True, "status": 200, "two_factor": False, "message": ""},
    )
    # 需点击 1 次才签发令牌。
    page.turnstile_clicks_needed = 1

    result, helpers = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "success"
    # 确认确实发生了真实鼠标点击，并记录了人工可观察的验证阶段。
    assert page.mouse.clicks
    assert page.login_requests[0][1:] == [email, password, "real-turnstile-token"]
    assert any("持续等待 Cloudflare 令牌" in line for line in helpers.logs)
    assert any("令牌已签发" in line for line in helpers.logs)


def test_password_login_fallback_turnstile_timeout_is_need_verification(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("X100LABS_EMAIL", "user@example.test")
    monkeypatch.setenv("X100LABS_PASSWORD", "not-a-real-password")
    page = FakePage(
        [FakeElement("签到", role="button")],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
        turnstile_token="",
    )

    result, helpers = _run(page, {"login_timeout_ms": 1000, "poll_interval_ms": 20})

    assert result["status"] == "need_verification"
    assert result["detail"]["login_fallback"] == "turnstile_timeout"
    assert page.login_requests == []
    assert "100xlabs-turnstile-timeout.png" in helpers.screenshots


def test_password_login_fallback_rejected_does_not_leak_credentials(
    monkeypatch: Any,
) -> None:
    email = "user@example.test"
    password = "not-a-real-password"
    monkeypatch.setenv("X100LABS_EMAIL", email)
    monkeypatch.setenv("X100LABS_PASSWORD", password)
    page = FakePage(
        [FakeElement("签到", role="button")],
        url="https://example.invalid/login?redirect=/check-in",
        authenticated=False,
        has_password_field=True,
        turnstile_token="real-turnstile-token",
        login_result={"ok": False, "status": 403, "two_factor": False, "message": ""},
    )

    result, _ = _run(page, {"button_wait_ms": 1, "poll_interval_ms": 20})

    assert result["status"] == "need_verification"
    assert result["detail"]["response_status"] == 403
    rendered = repr(result)
    assert email not in rendered
    assert password not in rendered


# ── 点击路径必须回传额度（回归：站点回了额度却只显示「签到成功」）──────────────
def test_click_path_reports_amounts_from_response_body() -> None:
    """点击签到后，响应体里的 reward_amount / balance 必须进入结果。

    回归：click_and_confirm 的响应监听此前只记录 status/url、从不读 body，
    站点明明回了 {"reward_amount":0.5,"balance":26.55} 也全被丢弃，结果只剩
    「签到成功」而无额度——而 api_fallback 路径早就在读这些字段，两条路径产出不一致。
    """
    def finish(page: FakePage, element: FakeElement) -> None:
        del element
        page.emit_response(200, body={"reward_amount": 0.5, "balance": 26.55})

    page = FakePage([FakeElement("签到", role="button", on_click=finish)])

    result, _helpers = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["quota_awarded"] == 0.5
    assert result["detail"]["current_quota"] == 26.55
    # 必须标记美元，否则聚合层会再除 500000（$0.50 → $0.0000）
    assert result["detail"]["quota_is_usd"] is True


def test_click_path_reads_amounts_nested_under_data() -> None:
    """Sub2API 统一响应是 {code:0, data:{...}}，额度在 data 里。"""
    def finish(page: FakePage, element: FakeElement) -> None:
        del element
        page.emit_response(200, body={"code": 0, "data": {"balance_added": 1.25, "balance": 100.0}})

    page = FakePage([FakeElement("签到", role="button", on_click=finish)])

    result, _helpers = _run(page)

    assert result["status"] == "success"
    assert result["detail"]["quota_awarded"] == 1.25
    assert result["detail"]["current_quota"] == 100.0


def test_click_path_without_json_body_still_succeeds() -> None:
    """响应不是 JSON 时不能影响签到结论：额度只是附加信息。"""
    def finish(page: FakePage, element: FakeElement) -> None:
        del element
        page.emit_response(200)  # body=None → json() 抛错

    page = FakePage([FakeElement("签到", role="button", on_click=finish)])

    result, _helpers = _run(page)

    assert result["status"] == "success"
    assert "quota_awarded" not in result["detail"]


# ── 「今日已签到」也要带余额状态，与 API 路径对等 ──────────────────────────────
def test_already_done_by_button_state_reports_balance_from_status_endpoint() -> None:
    """页面判定已签到时，主动查状态端点补出余额与连续天数。

    回归：这条分支在点击之前就返回，手上没有任何签到响应可读，此前只回一句
    「今日已签到」而不带额度；同一站点走 API 路径却能报出「今日已签=True
    余额=$607.51」，两条路径信息量不对等。字段名取自实测响应
    GET /api/v1/check-in/status → {"data":{"balance":897,"today_reward":5,
    "current_streak":3,"total_check_in_days":3,"checked_in_today":true}}。
    """
    page = FakePage(
        [FakeElement("今日已签到", role="button", disabled=True)],
        status_result={
            "balance": 897.0,
            "today_reward": 5.0,
            "checked_in_today": True,
            "current_streak": 3.0,
            "total_check_in_days": 3.0,
        },
    )

    result, _helpers = _run(page)

    assert result["status"] == "already_done"
    assert result["detail"]["completion_signal"] == "button_state"
    assert result["detail"]["current_quota"] == 897.0
    assert result["detail"]["consecutive_days"] == 3.0
    assert result["detail"]["total_checkins"] == 3.0
    assert result["detail"]["checked_in_today"] is True
    # 余额是美元，必须标记，否则汇总层会再除 500000（$897 → $0.0018）
    assert result["detail"]["quota_is_usd"] is True
    assert page.status_requests == 1
    assert page.clicked == []


def test_already_done_still_works_when_status_endpoint_unavailable() -> None:
    """状态端点不可用时照常返回 already_done：额度只是附加信息。"""
    page = FakePage(
        [FakeElement("今日已签到", role="button", disabled=True)],
        status_result=None,
    )

    result, _helpers = _run(page)

    assert result["status"] == "already_done"
    assert "current_quota" not in result["detail"]
    assert page.status_requests == 1


def test_click_then_409_falls_back_to_status_query_for_balance() -> None:
    """点击后拿到 409（今日已签到）时，补一次状态查询把余额带出来。

    409 响应体通常只有一句「今日已签到」、不含余额，此前这条分支直接返回，
    结果里没有任何额度信息。与上面 button_state 分支同理：已签到也该报余额。
    """
    def finish(page: FakePage, element: FakeElement) -> None:
        del element
        page.emit_response(409)

    page = FakePage(
        [FakeElement("签到", role="button", on_click=finish)],
        status_result={"balance": 897.0, "current_streak": 3.0, "total_check_in_days": 3.0},
    )

    result, _helpers = _run(page)

    assert result["status"] == "already_done"
    assert result["detail"]["response_status"] == 409
    assert result["detail"]["current_quota"] == 897.0
    assert result["detail"]["consecutive_days"] == 3.0
    assert page.status_requests == 1


def test_click_then_409_with_balance_in_body_skips_extra_query() -> None:
    """409 响应体自带余额时不再多打一次状态接口。"""
    def finish(page: FakePage, element: FakeElement) -> None:
        del element
        page.emit_response(409, body={"message": "今日已签到", "balance": 500.0})

    page = FakePage(
        [FakeElement("签到", role="button", on_click=finish)],
        status_result={"balance": 897.0},
    )

    result, _helpers = _run(page)

    assert result["status"] == "already_done"
    assert result["detail"]["current_quota"] == 500.0
    assert page.status_requests == 0
