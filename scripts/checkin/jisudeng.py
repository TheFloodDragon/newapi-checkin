#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极速蹬（jisudeng.com）每日签到 browser_script。

该站点的签到页是登录后的 Vue SPA：
- 可签到按钮：立即签到
- 已签到状态：今日已签到
- 签到接口：POST /api/v1/play/checkin

脚本只消费 browser_script 运行器恢复的 browser_state，不保存或处理邮箱密码。
"""

from __future__ import annotations

import asyncio
from typing import Any


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """恢复登录态后执行极速蹬每日签到。"""
    del context
    args = dict(getattr(site, "script_args", {}) or {})

    def _texts(key: str, default: list[str]) -> list[str]:
        value = args.get(key)
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            return items or default
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return default

    start_url = str(args.get("start_url") or args.get("url") or "/check-in").strip()
    resolved_url = helpers.resolve_url(start_url)
    goto_timeout = int(args.get("goto_timeout", 60000) or 60000)
    ready_timeout = int(args.get("ready_timeout", 10000) or 10000)
    button_wait_ms = int(args.get("button_wait_ms", 25000) or 25000)
    completion_timeout_ms = int(args.get("completion_timeout_ms", 10000) or 10000)
    click_timeout = int(args.get("click_timeout", 5000) or 5000)
    poll_interval_ms = max(20, int(args.get("poll_interval_ms", 100) or 100))
    checkin_texts = _texts("checkin_text", ["立即签到"])
    already_texts = _texts("already_text", ["今日已签到", "已签到"])
    success_texts = _texts("success_text", ["已到账", "签到成功"])

    async def _is_visible(locator: Any) -> bool:
        try:
            return bool(await locator.is_visible())
        except Exception:
            return False

    async def _is_disabled(locator: Any) -> bool:
        try:
            return bool(await locator.is_disabled())
        except Exception:
            return False

    async def _visible_text(text: str) -> bool:
        try:
            return await _is_visible(page.get_by_text(text, exact=False).first)
        except Exception:
            return False

    async def _find_already() -> tuple[str, Any] | None:
        for text in already_texts:
            try:
                locator = page.get_by_role("button", name=text, exact=False).first
            except Exception:
                locator = None
            if locator is not None and await _is_visible(locator):
                return text, locator
            if await _visible_text(text):
                return text, None
        return None

    async def _find_checkin_button() -> tuple[str, Any] | None:
        for text in checkin_texts:
            try:
                locator = page.get_by_role("button", name=text, exact=False).first
            except Exception:
                continue
            if await _is_visible(locator) and not await _is_disabled(locator):
                return text, locator
        return None

    async def _login_page() -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if "/login" in url:
            return True
        # URL 在某些驱动异常时可能尚未刷新，使用登录页标题作为兜底。
        return await _visible_text("欢迎回来")

    await helpers.goto(start_url, timeout=goto_timeout, wait_until="commit")
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ready_timeout)
    except Exception:
        pass
    try:
        await page.wait_for_load_state("networkidle", timeout=min(ready_timeout, 8000))
    except Exception:
        pass

    if await _login_page():
        return helpers.need_login(
            "极速蹬登录态已失效，请重新完成邮箱登录和 Turnstile 后保存 browser_state",
            {"target_url": resolved_url, "login_url": str(getattr(page, "url", "") or "")},
        )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, button_wait_ms) / 1000
    checkin_button: tuple[str, Any] | None = None
    while True:
        already = await _find_already()
        if already:
            text, _ = already
            return helpers.already_done(
                "今日已签到",
                {"matched_text": text, "completion_signal": "already_state", "target_url": resolved_url},
            )

        checkin_button = await _find_checkin_button()
        if checkin_button is not None:
            break
        if loop.time() >= deadline:
            break
        remaining_ms = max(1, int((deadline - loop.time()) * 1000))
        await page.wait_for_timeout(min(poll_interval_ms * 3, remaining_ms))

    if checkin_button is None:
        screenshot = await helpers.screenshot("jisudeng-no-checkin-button.png")
        return helpers.need_config(
            f"未找到极速蹬签到按钮文本：{', '.join(checkin_texts)}",
            {
                "checkin_texts": checkin_texts,
                "target_url": resolved_url,
                "button_wait_ms": button_wait_ms,
                "screenshot": screenshot,
            },
        )

    response: dict[str, Any] = {}

    def _capture_response(item: Any) -> None:
        try:
            request = getattr(item, "request", None)
            method = str(getattr(request, "method", "") or "").upper()
            url = str(getattr(item, "url", "") or "")
            lowered = url.casefold()
            if method != "POST" or "/play/checkin" not in lowered or "/play/checkin/makeup" in lowered:
                return
            response.update({"status": int(getattr(item, "status", 0) or 0), "url": url})
        except Exception:
            return

    listener_registered = False
    try:
        page.on("response", _capture_response)
        listener_registered = True
    except Exception:
        pass

    clicked_text = ""
    click_strategy = ""
    clicked_locator: Any = None
    try:
        for attempt in range(3):
            current = checkin_button if attempt == 0 else await _find_checkin_button()
            if current is None:
                await page.wait_for_timeout(min(150, poll_interval_ms))
                continue
            text, locator = current
            try:
                await locator.scroll_into_view_if_needed(timeout=click_timeout)
            except Exception:
                pass
            attempts = (
                ("normal", lambda: locator.click(timeout=click_timeout)),
                ("force", lambda: locator.click(timeout=click_timeout, force=True)),
                ("dispatch", lambda: locator.dispatch_event("click")),
                ("dom", lambda: locator.evaluate("el => el.click()")),
            )
            for strategy, click in attempts:
                try:
                    await click()
                    clicked_text = text
                    click_strategy = strategy
                    clicked_locator = locator
                    break
                except Exception:
                    continue
            if clicked_text:
                break
            await page.wait_for_timeout(min(200, max(50, poll_interval_ms)))

        if not clicked_text:
            screenshot = await helpers.screenshot("jisudeng-click-failed.png")
            return helpers.error(
                "定位到极速蹬签到按钮但点击失败，请稍后重试",
                {"target_url": resolved_url, "screenshot": screenshot},
            )

        base_detail = {
            "clicked_text": clicked_text,
            "click_strategy": click_strategy,
            "target_url": resolved_url,
        }
        completion_deadline = loop.time() + max(0, completion_timeout_ms) / 1000
        while True:
            status = int(response.get("status", 0) or 0)
            if 200 <= status < 300:
                return helpers.success(
                    "极速蹬签到成功",
                    {
                        **base_detail,
                        "completion_signal": "checkin_response",
                        "response_status": status,
                        "response_url": response.get("url", ""),
                    },
                )
            if status == 409:
                return helpers.already_done(
                    "今日已签到",
                    {**base_detail, "completion_signal": "checkin_response", "response_status": status},
                )
            if status >= 400:
                return helpers.error(
                    f"极速蹬签到接口返回错误（HTTP {status}）",
                    {
                        **base_detail,
                        "completion_signal": "checkin_response",
                        "response_status": status,
                        "response_url": response.get("url", ""),
                    },
                )

            for text in success_texts:
                if await _visible_text(text):
                    return helpers.success(
                        "极速蹬签到成功",
                        {**base_detail, "completion_signal": "success_text", "matched_text": text},
                    )

            already = await _find_already()
            if already:
                text, _ = already
                return helpers.success(
                    "极速蹬签到成功",
                    {**base_detail, "completion_signal": "already_state", "matched_text": text},
                )

            if clicked_locator is not None and not await _is_visible(clicked_locator):
                return helpers.success(
                    "极速蹬签到成功",
                    {**base_detail, "completion_signal": "button_hidden"},
                )

            if loop.time() >= completion_deadline:
                break
            remaining_ms = max(1, int((completion_deadline - loop.time()) * 1000))
            await page.wait_for_timeout(min(poll_interval_ms, remaining_ms))

        screenshot = await helpers.screenshot("jisudeng-after-click.png")
        return helpers.error(
            "已点击极速蹬签到按钮，但未检测到签到完成信号",
            {**base_detail, "completion_timeout_ms": completion_timeout_ms, "screenshot": screenshot},
        )
    finally:
        if listener_registered:
            try:
                page.remove_listener("response", _capture_response)
            except Exception:
                pass
