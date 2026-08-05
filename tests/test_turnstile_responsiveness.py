# -*- coding: utf-8 -*-
"""Turnstile 等待的响应性回归。

修的是三个实测症状（百倍/极速蹬登录页）：
1. 首次点击太慢：旧实现先读一次令牌（必然为空）、再 sleep 一整个轮询间隔才点，
   白等约 1 秒；
2. 人工完成后不继续识别：旧实现点击成功后固定 sleep 1.5–3 秒才再看令牌，
   人在这期间点完也要等满整段；
3. 找不到 widget 就没事可做：应继续观察令牌（有头模式下人工完成走这条路径），
   而不是反复空点。

这里用假 Page 记录每次 wait_for_timeout 的时长，从而断言「等待节奏」本身，
而不是只断言最终拿到了令牌 —— 后者在旧实现下同样成立，测不出响应性差异。
"""

from __future__ import annotations

import asyncio

from browser import turnstile

BOX = {"x": 100.0, "y": 200.0, "width": 300.0, "height": 65.0}


class FakePage:
    """可控 widget / 令牌的假 Page，记录鼠标点击与每次等待时长。"""

    def __init__(
        self,
        *,
        has_widget: bool = True,
        token_after_clicks: int | None = 1,
        token_after_waits: int | None = None,
        token: str = "tk",
    ) -> None:
        self._has_widget = has_widget
        self._token_after_clicks = token_after_clicks
        self._token_after_waits = token_after_waits
        self._issue = token
        self._token = ""
        self.clicks = 0
        self.waits: list[int] = []
        # 按发生顺序记录 move / click / wait：区分「点击手势内部的短等待」和
        # 「轮询空等」，否则无法断言「首次点击前没有空等一轮」。
        self.events: list[tuple[str, int]] = []
        self.mouse = _FakeMouse(self)

    # -- turnstile 依赖的三个 page 接口 --
    async def evaluate(self, expr: str, arg=None):
        # 顺序与真实脚本一致：_FIND_BOX_JS 里也含 cf-turnstile-response。
        if "getBoundingClientRect" in expr:
            return BOX if self._has_widget else None
        if "cf-turnstile-response" in expr:
            return self._token
        return None

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(int(ms))
        self.events.append(("wait", int(ms)))
        if self._token_after_waits is not None and len(self.waits) >= self._token_after_waits:
            self._token = self._issue
        await asyncio.sleep(0)

    def on_click(self) -> None:
        self.clicks += 1
        self.events.append(("click", self.clicks))
        if self._token_after_clicks is not None and self.clicks >= self._token_after_clicks:
            self._token = self._issue


class _FakeMouse:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.page.events.append(("move", 0))
        await asyncio.sleep(0)

    async def click(self, x: float, y: float) -> None:
        self.page.on_click()


class _MultiFieldPage(FakePage):
    """模拟第一个响应字段为空、后续字段已有令牌的页面。"""

    async def evaluate(self, expr: str, arg=None):
        if "cf-turnstile-response" in expr and "getBoundingClientRect" not in expr:
            return "later-widget-token" if "querySelectorAll" in expr else ""
        return await super().evaluate(expr, arg)


class _SlowPollPage(FakePage):
    """让轮询等待真实推进时间，用于验证长人工挑战不会被再次点击。"""

    def __init__(self) -> None:
        super().__init__(token_after_clicks=None, token_after_waits=15)

    async def wait_for_timeout(self, ms: int) -> None:
        self.waits.append(int(ms))
        self.events.append(("wait", int(ms)))
        if self._token_after_waits is not None and len(self.waits) >= self._token_after_waits:
            self._token = self._issue
        await asyncio.sleep(max(0, ms) / 1000)


def _solve(page: FakePage, *, timeout_ms: int = 5000, log=None) -> str:
    return asyncio.run(turnstile.solve(page, timeout_ms=timeout_ms, poll_interval_ms=250, log=log))


# ── 1. 首次点击不再空等一轮 ──────────────────────────────────────────────────
def test_first_click_happens_before_any_wait() -> None:
    """widget 就绪时应立刻点击，而不是先 sleep 一个轮询间隔。

    旧实现的等待序列是 [1000, ...]（先等再点）；现在点击必须发生在第一次
    wait_for_timeout 之前，所以拿到令牌时点击数已 >=1。
    """
    page = FakePage()
    assert _solve(page) == "tk"
    assert page.clicks == 1
    # 断言首个事件是点击手势的鼠标移动，而不是轮询空等。
    # 注意不能直接断言 page.waits == []：click() 内部的人类化轨迹本身含
    # wait_for_timeout(200/150)，那属于点击手势的一部分，不是「先等一轮再点」。
    assert page.events, "应至少产生一次交互事件"
    assert page.events[0][0] == "move", f"首个事件应是点击手势，实际 {page.events}"
    # 点击之前不应出现轮询粒度（>=250ms）的等待。
    before_click = page.events[: [kind for kind, _ in page.events].index("click")]
    assert not [ms for kind, ms in before_click if kind == "wait" and ms >= 250], (
        f"首次点击前不该有轮询等待，实际 {before_click}"
    )


# ── 2. 点击后保持密集轮询（人工完成能被及时发现）──────────────────────────
def test_polling_after_click_uses_fine_grained_steps() -> None:
    """点击后不能固定 sleep 数秒：必须按小步长持续查，令牌一出现立即返回。"""
    # 令牌在第 3 次等待后才出现（模拟 Cloudflare 处理中 / 人工刚点完）。
    page = FakePage(token_after_clicks=None, token_after_waits=3)
    assert _solve(page) == "tk"
    assert page.waits, "应有轮询等待"
    assert max(page.waits) <= 500, f"轮询步长不得超过 500ms，实际 {page.waits}"
    assert len(page.waits) == 3, f"令牌出现后应立即返回，实际等待 {page.waits}"


def test_human_completion_is_detected_without_extra_click() -> None:
    """有头模式下人工点完验证：即使脚本没点成功，也要认出令牌已签发。"""
    page = FakePage(has_widget=False, token_after_clicks=None, token_after_waits=2)
    logs: list[str] = []
    assert _solve(page, log=logs.append) == "tk"
    assert page.clicks == 0, "定位不到 widget 时不应点击"
    assert any("人工" in line for line in logs), "应提示可人工完成验证"
    assert any("已完成" in line for line in logs)


# ── 3. 不重复点击已在处理中的 widget ────────────────────────────────────────
def test_widget_is_not_reclicked_while_cloudflare_processes() -> None:
    """处理中重复点击会重置挑战。等待窗口内只应点一次。"""
    # 永不签发令牌：跑满超时，用点击次数衡量点击节奏。
    page = FakePage(token_after_clicks=None, token_after_waits=None)
    assert _solve(page, timeout_ms=1200) == ""
    # 窗口 3s > 超时 1.2s，因此整个过程只该点一次。
    assert page.clicks == 1, f"等待窗口内只应点一次，实际 {page.clicks}"


def test_timeout_returns_empty_token() -> None:
    page = FakePage(token_after_clicks=None, token_after_waits=None)
    assert _solve(page, timeout_ms=300) == ""


def test_already_issued_token_short_circuits() -> None:
    """非交互式 widget 可能已自动签发：不该再点一次。"""
    page = FakePage()
    page._token = "pre-issued"
    assert _solve(page) == "pre-issued"
    assert page.clicks == 0
    assert page.waits == []


def test_reads_non_empty_token_from_later_response_field() -> None:
    page = _MultiFieldPage()

    assert _solve(page) == "later-widget-token"
    assert page.clicks == 0


def test_long_manual_challenge_is_not_reset_by_a_second_click() -> None:
    page = _SlowPollPage()

    assert _solve(page, timeout_ms=5000) == "tk"
    assert page.clicks == 1, "人工验证超过原处理窗口时也不能再次点击重置 challenge"
