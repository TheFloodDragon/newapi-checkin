#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cloudflare Turnstile 交互式验证：读取令牌 + 真实鼠标点击复选框。

Sub2API 系站点（极速蹬 / 百倍等）的登录页嵌入 Cloudflare Turnstile 交互式
widget（"Verify you are human" 复选框）。被动等待不会签发令牌，必须用真实
鼠标事件点击复选框（Cloudflare 校验 isTrusted，JS click 无效）。

实测结论（Camoufox 无头下可稳定拿到令牌）：
1. 定位 widget 的 bounding box（CF iframe 或 .turnstile-container 容器）；
2. 复选框在 widget 左侧约 30px、垂直居中；
3. 先做一段人类化鼠标移动轨迹，再用 page.mouse.click 点击（真实事件）。

本模块只与页面交互、不伪造/篡改令牌，也不处理任何账号凭据。
"""

from __future__ import annotations

from typing import Any

# widget 左边缘到复选框中心的水平偏移（像素）。
_CHECKBOX_X_OFFSET = 30

# 点击复选框后留给 Cloudflare 的处理窗口（秒）。窗口内仍按 step 密集轮询令牌，
# 所以这个值只决定「多久后才考虑重新点一次」，不会延后令牌的发现时机。
_CLICK_SETTLE_SECONDS = 3.0

# 没找到 widget 时的观察间隔（秒）。此时不该反复尝试点击：widget 可能尚未挂载，
# 也可能已经变成「验证成功」态或正由人工操作，继续观察令牌即可。
_RETRY_GAP_SECONDS = 1.0

# 读取 input[name=cf-turnstile-response] 的当前值。
_READ_TOKEN_JS = """() => {
    const el = document.querySelector('input[name="cf-turnstile-response"]');
    return el && typeof el.value === 'string' ? el.value : '';
}"""

# 定位可见的 Turnstile widget bounding box：优先 CF challenge iframe，
# 退回 .turnstile-container / .turnstile-wrapper / 令牌 input 的容器。
_FIND_BOX_JS = """() => {
    const pick = (el) => {
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (r.width < 10 || r.height < 10) return null;
        return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const cf = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    let box = pick(cf);
    if (box) return box;
    for (const sel of ['.turnstile-container', '.turnstile-wrapper',
                        'input[name="cf-turnstile-response"]']) {
        const el = document.querySelector(sel);
        const b = pick(el && el.parentElement ? el.parentElement : el);
        if (b) return b;
    }
    return null;
}"""


async def read_token(page: Any) -> str:
    """读取 Cloudflare 正常签发的 Turnstile 令牌（不伪造、不篡改）。为空表示尚未签发。"""
    try:
        value = await page.evaluate(_READ_TOKEN_JS)
        return str(value or "").strip()
    except Exception:
        return ""


async def find_box(page: Any) -> dict[str, Any] | None:
    """定位 Turnstile widget 的可见位置，返回 {x,y,width,height} 或 None。"""
    try:
        box = await page.evaluate(_FIND_BOX_JS)
        return box if isinstance(box, dict) else None
    except Exception:
        return None


async def click(page: Any) -> bool:
    """用真实鼠标事件点击 Turnstile 复选框，触发 Cloudflare 签发令牌。

    关键（实测确认）：必须用真实鼠标事件（page.mouse.move/click，Cloudflare 校验
    isTrusted），不能用 JS click。复选框在 widget 左侧约 30px、垂直居中。点击前做
    一段人类化鼠标移动轨迹，降低被判为自动化的概率。返回是否成功发出点击。
    """
    box = await find_box(page)
    if not box:
        return False
    try:
        click_x = float(box["x"]) + _CHECKBOX_X_OFFSET
        click_y = float(box["y"]) + float(box["height"]) / 2
        await page.mouse.move(click_x - 60, click_y - 20, steps=8)
        await page.wait_for_timeout(200)
        await page.mouse.move(click_x, click_y, steps=12)
        await page.wait_for_timeout(150)
        await page.mouse.click(click_x, click_y)
        return True
    except Exception:
        return False


async def solve(
    page: Any,
    *,
    timeout_ms: int,
    poll_interval_ms: int = 1000,
    log: Any = None,
) -> str:
    """获取 Turnstile 令牌：尽快点一次复选框，然后密集轮询直到签发或超时。

    与旧实现的三处差异，都是实测踩出来的：

    1. **首次点击不再等一轮**。旧实现先 read_token（必然为空）、再 sleep 一整个
       轮询间隔，才发出第一次点击，白等约 1 秒。widget 就绪后越早点越好。
    2. **令牌轮询与点击节奏解耦**。旧实现点击成功后固定 sleep 1.5–3 秒才再看一眼
       令牌，人工在这期间完成验证也要等满整段；现在点击后按 250ms 粒度持续查，
       令牌一出现立即返回 —— 这正是「人工完成后没有继续识别」的直接原因。
    3. **不重复点已经点过的 widget**。Cloudflare 处理中再点会重置挑战，反而更慢。
       只在等待窗口过完仍无令牌时才重试点击。

    Args:
        page: Playwright/Camoufox Page。
        timeout_ms: 整体超时（毫秒）。
        poll_interval_ms: 令牌轮询粒度（毫秒），会被限制在 100–500 之间。
        log: 可选日志回调，用于把「已点击/等待人工完成」写进签到日志。

    Returns:
        非空令牌字符串；超时未拿到返回 ""。
    """
    import asyncio

    def _log(message: str) -> None:
        if log:
            log(message)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000
    # 轮询粒度独立于调用方的 poll_interval_ms：太粗会让「人工刚点完」延迟数秒才
    # 被发现，太细则空转。100–500ms 足够及时且开销可忽略。
    step = min(max(poll_interval_ms, 100), 500)

    async def _poll_until(window_end: float) -> str:
        """在给定时刻前持续查令牌，一出现立即返回。"""
        while True:
            token = await read_token(page)
            if token:
                return token
            now = loop.time()
            if now >= window_end or now >= deadline:
                return ""
            await page.wait_for_timeout(int(min(step, max(1, (window_end - now) * 1000))))

    # 先看一眼：widget 可能是非交互式的，已经自动签发。
    token = await read_token(page)
    if token:
        return token

    clicks = 0
    while loop.time() < deadline:
        clicked = await click(page)
        if clicked:
            clicks += 1
            if clicks == 1:
                _log("已点击 Turnstile 复选框，等待 Cloudflare 签发令牌...")
            # 点击后给 Cloudflare 一段处理窗口，但窗口内保持密集轮询：
            # 人工帮忙点完或自动通过时都能立刻拿到令牌，不必等满整段。
            window = min(loop.time() + _CLICK_SETTLE_SECONDS, deadline)
        else:
            # 找不到 widget（尚未挂载，或已被替换成「验证成功」态）：不点，
            # 只继续观察。人工在有头模式下手动完成时走的正是这条路径。
            if clicks == 0:
                _log("未定位到 Turnstile 复选框，持续等待令牌（可人工完成验证）...")
            window = min(loop.time() + _RETRY_GAP_SECONDS, deadline)
        token = await _poll_until(window)
        if token:
            if clicks:
                _log("Turnstile 令牌已签发")
            else:
                _log("Turnstile 验证已完成（令牌由页面自行签发）")
            return token
    return ""
