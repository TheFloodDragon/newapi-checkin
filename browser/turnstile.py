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

# 没找到 widget 时的观察间隔（秒）。此时不该反复尝试点击：widget 可能尚未挂载，
# 也可能已经变成「验证成功」态或正由人工操作，继续观察令牌即可。
_RETRY_GAP_SECONDS = 1.0

# 读取页面中所有 Turnstile 响应字段的当前值。
# 页面可能同时保留多个 widget 或旧字段；只读第一个字段会一直读到空值，
# 即使人工已经在后续 widget 完成验证。
_READ_TOKEN_JS = """() => {
    const fields = Array.from(document.querySelectorAll(
        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    ));
    for (const field of fields) {
        const value = typeof field.value === 'string'
            ? field.value
            : String(field.textContent || '');
        if (value.trim()) return value.trim();
    }
    return '';
}"""

# 定位可见的 Turnstile widget bounding box。
# 优先可见的 Cloudflare iframe，再退回 widget 容器/响应字段父容器；
# 不能只取第一个 iframe，因为页面可能同时挂有隐藏的 challenge iframe。
_FIND_BOX_JS = """() => {
    const visible = (el) => {
        if (!el || !el.isConnected) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            return false;
        }
        const r = el.getBoundingClientRect();
        return r.width >= 10 && r.height >= 10;
    };
    const boxOf = (el) => {
        if (!visible(el)) return null;
        const r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
    };
    const candidates = [];
    const add = (el, priority) => {
        const box = boxOf(el);
        if (box) candidates.push({ priority, box });
    };

    for (const iframe of document.querySelectorAll(
        'iframe[src*="challenges.cloudflare.com"], iframe[title*="Cloudflare"]'
    )) {
        add(iframe, 0);
    }
    for (const selector of [
        '.cf-turnstile',
        '.turnstile-container',
        '.turnstile-wrapper',
        '[data-sitekey]'
    ]) {
        for (const element of document.querySelectorAll(selector)) {
            add(element, 1);
        }
    }
    for (const field of document.querySelectorAll(
        'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    )) {
        add(field.parentElement || field, 2);
    }

    candidates.sort((left, right) => left.priority - right.priority);
    return candidates.length ? candidates[0].box : null;
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
    """获取 Turnstile 令牌：立即尝试一次真实点击，然后持续观察到超时。

    成功点击后不再重复点击。Cloudflare 在处理挑战时再次点击可能会重置验证，
    也是人工完成后「明明点过却不算数」的主要竞态；此时应保持密集轮询，让页面自行
    填入令牌。只有 widget 尚未挂载或点击明确失败时，才按短间隔重新定位。

    Args:
        page: Playwright/Camoufox Page。
        timeout_ms: 整体超时（毫秒）。
        poll_interval_ms: 令牌轮询粒度（毫秒），会被限制在 100–500 之间。
        log: 可选日志回调，用于把「已点击/等待人工/令牌已签发」写进签到日志。

    Returns:
        非空令牌字符串；超时未拿到返回 ""。
    """
    import asyncio

    def _log(message: str) -> None:
        if callable(log):
            try:
                log(message)
            except Exception:
                pass

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000
    # 轮询粒度独立于调用方的 poll_interval_ms：太粗会让人工刚完成后的令牌迟迟不被
    # 发现，太细则空转。100–500ms 足够及时且开销可忽略。
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

    clicked = False
    logged_waiting = False
    while loop.time() < deadline:
        if not clicked:
            clicked = await click(page)
            if clicked:
                _log("已点击 Turnstile 复选框，持续等待 Cloudflare 令牌（可人工完成验证）...")
            elif not logged_waiting:
                _log("未定位到 Turnstile 复选框，持续等待令牌（可人工完成验证）...")
                logged_waiting = True

        # 成功点击后一直观察到总超时，不能用固定窗口再次点击重置 challenge。
        window_end = deadline if clicked else min(loop.time() + _RETRY_GAP_SECONDS, deadline)
        token = await _poll_until(window_end)
        if token:
            _log("Turnstile 令牌已签发" if clicked else "Turnstile 验证已完成（令牌由页面自行签发）")
            return token
        if clicked:
            return ""
    return ""
