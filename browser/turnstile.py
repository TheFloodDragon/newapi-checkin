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


async def solve(page: Any, *, timeout_ms: int, poll_interval_ms: int = 1000) -> str:
    """获取 Turnstile 令牌：轮询读取，未签发则真实点击复选框，直到拿到令牌或超时。

    Args:
        page: Playwright/Camoufox Page。
        timeout_ms: 整体超时（毫秒）。
        poll_interval_ms: 轮询基准间隔（毫秒）。

    Returns:
        非空令牌字符串；超时未拿到返回 ""。
    """
    import asyncio

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000
    base = max(poll_interval_ms, 500)
    while True:
        token = await read_token(page)
        if token:
            return token
        if loop.time() >= deadline:
            return ""
        # 未拿到令牌：主动点击 widget（成功点击后给 Cloudflare 更久处理时间）。
        if await click(page):
            await page.wait_for_timeout(min(max(base, 1500), 3000))
        else:
            await page.wait_for_timeout(min(base, 1000))
