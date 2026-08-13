# -*- coding: utf-8 -*-
"""Cloudflare / Turnstile 处理策略回归测试。

守护三条曾出问题的语义：
1. 检测覆盖：新版 managed challenge、JS/cookie 提示页、challenge-platform iframe
   等变体必须被识别为挑战页。旧实现只认 "Just a moment" / "Checking your browser"，
   漏判时 solve_cloudflare 直接返回 True，调用方在仍被拦截的页面上继续操作。
2. 交互式 widget 必须走真实鼠标点击（Cloudflare 校验事件 isTrusted），
   被动等待与 ClickSolver 的 interstitial 策略都拿不到令牌。
3. 不谎报成功：拿到令牌 ≠ 挑战已通过，必须回读页面确认后才返回 True。
"""

from __future__ import annotations

import asyncio

import pytest

from browser import bypass, turnstile


# ── 检测覆盖 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("title", "html", "expected"),
    [
        ("Just a moment...", "<html><body>please wait</body></html>", True),
        ("", "<html><body>Checking your browser before accessing</body></html>", True),
        # 新版 managed challenge：旧词表漏判
        ("Just a moment...", "<html><body>Verifying you are human</body></html>", True),
        ("", "<html><body>Enable JavaScript and cookies to continue</body></html>", True),
        ("", '<html><body><div id="cf-chl-widget"></div></body></html>', True),
        ("Attention Required!", "<html><body>blocked</body></html>", True),
        ("", '<html><body><div class="cf-wrapper">blocked</div></body></html>', True),
        ("", '<html><body><form id="challenge-form"></form></body></html>', True),
        # 正常页面不得误判
        ("Dashboard", "<html><body>welcome back</body></html>", False),
        ("极速蹬", "<html><body>余额 $1.00</body></html>", False),
        # challenge-platform 是「受 CF 保护」的环境脚本，正常页面同样会加载它，
        # 不能作为挑战页判据。实测 Linux DO 授权页（title="authorize - linux do
        # connect"，无任何 CF 容器）仅因含该脚本就被误报「检测到 Cloudflare 挑战」，
        # 白跑一轮 ClickSolver 并掩盖真实失败原因。
        ("authorize - linux do connect", '<html><body><script src="/cdn-cgi/challenge-platform/x.js"></script></body></html>', False),
        # 含 hCaptcha widget 的正常业务页同样不得误判
        ("签到 - 福利站", '<html><body><iframe src="https://newassets.hcaptcha.com/captcha/v1/x"></iframe></body></html>', False),
    ],
)
def test_cf_challenge_detection_covers_modern_variants(title: str, html: str, expected: bool) -> None:
    assert bypass._is_cf_challenge(title.lower(), html.lower()) is expected


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ('<input name="cf-turnstile-response">', True),
        ('<div class="turnstile-container"></div>', True),
        ('<iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js"></iframe>', True),
        # interstitial 页没有可点复选框
        ("<html><body>Verifying you are human</body></html>", False),
        ("<html><body>welcome</body></html>", False),
    ],
)
def test_interactive_widget_detection(html: str, expected: bool) -> None:
    assert bypass._has_interactive_widget(html.lower()) is expected


# ── 伪造 Page ───────────────────────────────────────────────────────────────
class FakeMouse:
    """记录真实鼠标事件；click 通知 page 签发令牌。"""

    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.moves: list[tuple[float, float]] = []
        self.page: "FakePage | None" = None

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y))

    async def click(self, x: float, y: float) -> None:
        self.clicks.append((x, y))
        if self.page is not None:
            self.page.on_click()


class FakePage:
    def __init__(
        self,
        title: str,
        html: str,
        *,
        clears_after_click: bool = True,
        clear_after_waits: int | None = None,
        token: str = "real-turnstile-token",
    ) -> None:
        self._title = title
        self._html = html
        self._clears = clears_after_click
        self._clear_after_waits = clear_after_waits
        self._wait_count = 0
        self._issue = token
        self._token = ""
        self.mouse = FakeMouse()
        self.mouse.page = self

    def on_click(self) -> None:
        self._token = self._issue
        if self._clears:
            self._title = "Dashboard"
            self._html = "<html><body>ok</body></html>"

    async def title(self) -> str:
        return self._title

    async def content(self) -> str:
        return self._html

    async def evaluate(self, expr: str, arg=None):
        # 顺序关键：_FIND_BOX_JS 里同时含 cf-turnstile-response，
        # 必须先判 getBoundingClientRect，否则 find_box 会拿到令牌字符串。
        if "getBoundingClientRect" in expr:
            return {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}
        if "cf-turnstile-response" in expr:
            return self._token
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        del ms
        self._wait_count += 1
        if self._clear_after_waits is not None and self._wait_count >= self._clear_after_waits:
            self._title = "Dashboard"
            self._html = "<html><body>ok</body></html>"
        await asyncio.sleep(0)


# ── turnstile 真实点击 ──────────────────────────────────────────────────────
def test_turnstile_clicks_checkbox_at_measured_offset() -> None:
    """复选框在 widget 左侧 30px、垂直居中（实测结论），且必须用真实鼠标事件。"""
    page = FakePage("Sign in", '<input name="cf-turnstile-response">')

    token = asyncio.run(turnstile.solve(page, timeout_ms=2000, poll_interval_ms=20))

    assert token == "real-turnstile-token"
    assert page.mouse.clicks == [(130.0, 232.5)]  # x+30, y+height/2
    assert page.mouse.moves, "点击前应有人类化鼠标移动轨迹"


def test_turnstile_returns_empty_on_timeout_without_token() -> None:
    page = FakePage("Sign in", '<input name="cf-turnstile-response">', token="")

    token = asyncio.run(turnstile.solve(page, timeout_ms=200, poll_interval_ms=20))

    assert token == ""


# ── solve_cloudflare 策略 ───────────────────────────────────────────────────
def test_interactive_challenge_uses_real_mouse_click(monkeypatch) -> None:
    """交互式 widget 必须真实点击，而不是被动等待签发。"""
    monkeypatch.setattr(bypass, "_check_camoufox", lambda: None)
    page = FakePage("Sign in", '<div class="turnstile-container"><input name="cf-turnstile-response"></div>')
    logs: list[str] = []

    ok = asyncio.run(bypass.solve_cloudflare(page, log=logs.append, wait_seconds=1))

    assert ok is True
    assert len(page.mouse.clicks) == 1
    assert any("真实鼠标点击" in line for line in logs)


def test_no_challenge_page_is_passed_through_without_clicking(monkeypatch) -> None:
    monkeypatch.setattr(bypass, "_check_camoufox", lambda: None)
    page = FakePage("Dashboard", "<html><body>welcome</body></html>")
    logs: list[str] = []

    ok = asyncio.run(bypass.solve_cloudflare(page, log=logs.append, wait_seconds=1))

    assert ok is True
    assert page.mouse.clicks == []
    assert logs == []


def test_token_waits_for_async_page_clear(monkeypatch) -> None:
    """人工/Cloudflare 回调先填令牌、页面稍后刷新时仍应算验证通过。"""
    monkeypatch.setattr(bypass, "_check_camoufox", lambda: None)
    page = FakePage(
        "Just a moment...",
        '<div class="turnstile-container"><input name="cf-turnstile-response"></div>',
        clear_after_waits=2,
    )
    logs: list[str] = []

    ok = asyncio.run(bypass.solve_cloudflare(page, log=logs.append, wait_seconds=1))

    assert ok is True
    assert page.mouse.clicks == [(130.0, 232.5)]
    assert any("异步放行" in line for line in logs)


def test_token_issued_but_page_still_blocked_is_not_success(monkeypatch) -> None:
    """拿到令牌 ≠ 挑战已通过：页面仍是挑战页时必须返回 False。

    旧实现在交互式分支拿到令牌就 return True，导致调用方在仍被拦截的页面上
    继续操作（表现为后续读额度/点签到全部失败但报「已通过」）。
    """
    monkeypatch.setattr(bypass, "_check_camoufox", lambda: None)
    page = FakePage(
        "Just a moment...",
        '<div class="turnstile-container"><input name="cf-turnstile-response"></div>',
        clears_after_click=False,
    )
    logs: list[str] = []

    ok = asyncio.run(bypass.solve_cloudflare(page, log=logs.append, wait_seconds=1))

    assert ok is False
    assert len(page.mouse.clicks) >= 1, "仍应尝试过真实点击"
    assert any("未能通过" in line for line in logs)


def test_managed_challenge_without_widget_is_reported_unsolved(monkeypatch) -> None:
    """新版 managed challenge 无可点 widget：ClickSolver 失败后不得谎报成功。"""
    monkeypatch.setattr(bypass, "_check_camoufox", lambda: None)

    class _FailingSolver:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc) -> None:
            return None

        async def solve_captcha(self, **_kwargs) -> None:
            raise RuntimeError("Cloudflare iframes not found")

    monkeypatch.setattr(bypass, "ClickSolver", _FailingSolver)
    page = FakePage("Just a moment...", "<html><body>Verifying you are human</body></html>", clears_after_click=False)
    logs: list[str] = []

    ok = asyncio.run(bypass.solve_cloudflare(page, log=logs.append, wait_seconds=1))

    assert ok is False
    assert page.mouse.clicks == []  # 无 widget，不该尝试点击
