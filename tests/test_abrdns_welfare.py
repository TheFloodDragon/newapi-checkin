# -*- coding: utf-8 -*-
"""ABR 福利站浏览器脚本的本地回归测试。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from browser import script_loader

SCRIPT_PATH = "scripts/checkin/abrdns_welfare.py"
welfare = script_loader.load_site_script(SCRIPT_PATH)


class FakeLocator:
    def __init__(
        self,
        *,
        visible: bool = True,
        text: str = "",
        click_callback: Any = None,
        child: "FakeLocator | None" = None,
        disabled: bool = False,
    ) -> None:
        self.visible = visible
        self.text = text
        self.click_callback = click_callback
        self.child = child
        self.disabled = disabled
        self.clicks = 0

    async def is_disabled(self) -> bool:
        return self.disabled

    @property
    def first(self) -> "FakeLocator":
        return self

    def locator(self, _selector: str) -> "FakeLocator":
        return self.child or FakeLocator(visible=False)

    async def count(self) -> int:
        return 1

    async def is_visible(self) -> bool:
        return self.visible

    async def inner_text(self) -> str:
        return self.text

    async def text_content(self) -> str:
        return self.text

    async def click(self, **_kwargs: Any) -> None:
        self.clicks += 1
        if self.click_callback:
            self.click_callback()


class FakePage:
    def __init__(
        self,
        text: str,
        *,
        form: bool = False,
        hcaptcha: bool = False,
        submit_disabled: bool = False,
    ) -> None:
        self.text = text
        self.form_enabled = form
        self.hcaptcha = hcaptcha
        self.url = "https://checkin.new-api.abrdns.com/checkin"
        self.submit = FakeLocator(click_callback=self._submitted, disabled=submit_disabled)
        self.body = FakeLocator(text=text)
        self.form = FakeLocator(child=self.submit)
        # 真实页面同时存在 action=/auth/logout 的表单；宽松回退命中它会把账号登出。
        self.logout = FakeLocator(child=FakeLocator(click_callback=self._logged_out))
        self.body_text_selectors: list[str] = []
        self.submitted = False
        self.logged_out = False

    def _submitted(self) -> None:
        self.submitted = True

    def _logged_out(self) -> None:
        self.logged_out = True

    def locator(self, selector: str) -> FakeLocator:
        self.body_text_selectors.append(selector)
        if selector == "body":
            return self.body
        if "hcaptcha.com" in selector or "hCaptcha" in selector:
            return self.iframe if self.hcaptcha else FakeLocator(visible=False)
        if selector == "form":
            return self.logout
        if selector.startswith("form"):
            return self.form if self.form_enabled else FakeLocator(visible=False)
        if "submit" in selector or selector == "button":
            return self.submit if self.form_enabled else FakeLocator(visible=False)
        if selector == "a[href='/auth/linuxdo/login']":
            return FakeLocator(visible=False)
        return FakeLocator(visible=False)

    @property
    def iframe(self) -> FakeLocator:
        return FakeLocator(visible=self.hcaptcha)

    async def evaluate(self, _script: str) -> str:
        return ""

    async def wait_for_load_state(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def wait_for_timeout(self, _milliseconds: int) -> None:
        return None


class FakeHelpers:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")
        self.logs: list[str] = []
        self.solve_options: dict[str, Any] = {}

    def resolve_url(self, path: str = "/") -> str:
        return "https://checkin.new-api.abrdns.com" + (path if path.startswith("/") else "/" + path)

    async def goto(self, _path: str, **_kwargs: Any) -> None:
        self.page.url = "https://checkin.new-api.abrdns.com/checkin"

    async def screenshot(self, name: str, **_kwargs: Any) -> str:
        return f".cache-checkin/{name}"

    async def solve_hcaptcha(self, *, trigger: Any = None, options: Any = None) -> Any:
        self.solve_options = dict(options or {})
        return SimpleNamespace(
            status="failed",
            message="未取得验证令牌",
            token="",
            rounds=1,
            challenge_type="grid",
            screenshot=".cache-checkin/hcaptcha-failed.png",
        )

    def log(self, message: str) -> None:
        self.logs.append(message)

    def _result(self, status: str, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        value = {"status": status, "message": message, "detail": dict(detail or {})}
        value["detail"].update({key: item for key, item in kwargs.items() if item is not None})
        return value

    def already_done(self, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._result("already_done", message, detail, **kwargs)

    def success(self, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._result("success", message, detail, **kwargs)

    def need_login(self, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._result("need_login", message, detail, **kwargs)

    def need_verification(self, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._result("need_verification", message, detail, **kwargs)

    def error(self, message: str, detail: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._result("error", message, detail, **kwargs)


def test_script_declares_browser_own_flow_and_oauth_is_script_managed() -> None:
    hooks = script_loader.load_script_hooks(SCRIPT_PATH)

    assert hooks.owns_http_flow is True
    assert hooks.run is not None
    assert hooks.do_checkin is None


def test_extract_amount_only_uses_explicit_reward_text() -> None:
    assert welfare._extract_amount("签到成功，获得 $1.25") == 1.25
    assert welfare._extract_amount("当前余额 $99.00") == 99.0
    assert welfare._extract_amount("签到页面已打开") is None


def test_run_already_done_does_not_submit() -> None:
    page = FakePage("今日已签到，今日奖励 $0.80")
    helpers = FakeHelpers(page)
    site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")

    result = asyncio.run(welfare.run(page, None, site, helpers))

    assert result["status"] == "already_done"
    assert result["detail"]["auth_verified"] is True
    assert result["detail"]["today_reward"] == 0.8
    assert page.submitted is False


def test_run_returns_need_verification_when_hcaptcha_is_present() -> None:
    page = FakePage("请完成验证后签到", form=True, hcaptcha=True)
    helpers = FakeHelpers(page)
    site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")

    result = asyncio.run(welfare.run(page, None, site, helpers))

    assert result["status"] == "need_verification"
    assert result["detail"]["captcha"] == "hcaptcha"
    assert result["detail"]["captcha_status"] == "failed"
    assert page.submitted is False
    assert all("token" not in line.casefold() for line in helpers.logs)
    # 本站 widget 挂载慢，必须传入放宽后的挂载预算，否则会在挑战出现前就超时。
    assert helpers.solve_options["widget_mount_timeout_ms"] >= 40_000
    assert helpers.solve_options["presence_timeout_ms"] >= 20_000
    assert helpers.solve_options["max_rounds"] >= 5
    assert helpers.solve_options["vision_max_attempts"] == 2
    assert helpers.solve_options["vision_retry_reserve_ms"] >= 15_000
    assert helpers.solve_options["vision_max_edge"] == 400
    assert helpers.solve_options["vision_jpeg_quality"] == 72
    assert helpers.solve_options["move_before_click"] is False
    assert helpers.solve_options["click_timeout_ms"] == 5_000
    assert helpers.solve_options["temporal_frames"] == 6
    assert helpers.solve_options["temporal_interval_ms"] == 400
    assert helpers.solve_options["temporal_sheet_max_edge"] == 800
    assert helpers.solve_options["temporal_phase_wait_ms"] == 5_000


def test_run_never_submits_logout_form_as_checkin() -> None:
    """页面同时存在 /auth/logout 表单；未找到 /checkin 表单时不得回退点击它。"""
    page = FakePage("点击下方按钮领取今日额度奖励")
    helpers = FakeHelpers(page)
    site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")

    result = asyncio.run(welfare.run(page, None, site, helpers))

    assert page.logged_out is False
    assert page.submitted is False
    assert result["status"] in {"error", "need_verification", "need_login"}


def test_disabled_submit_button_reports_need_verification() -> None:
    """按钮在验证完成前是 disabled，不能把「点了但没提交」当成已提交。"""
    page = FakePage("请先进行验证 签到领取奖励", form=True, submit_disabled=True)
    helpers = FakeHelpers(page)
    site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")

    result = asyncio.run(welfare.run(page, None, site, helpers))

    assert result["status"] == "need_verification"
    assert result["detail"]["completion_signal"] == "submit_disabled"
    assert page.submitted is False


def test_run_submits_form_and_reports_success_text() -> None:
    page = FakePage("签到成功，获得 $0.50", form=True)
    helpers = FakeHelpers(page)
    site = SimpleNamespace(base_url="https://checkin.new-api.abrdns.com")

    result = asyncio.run(welfare.run(page, None, site, helpers))

    assert result["status"] == "success"
    assert result["detail"]["completion_signal"] == "success_text"
    assert result["detail"]["quota_awarded"] == 0.5
    assert page.submitted is True
