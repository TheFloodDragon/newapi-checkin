#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百倍/100xLabs 站点示例 browser_script。

配置示例：
{
  "checkin_action": "browser_script",
  "auth_method": "oauth",
  "script": "scripts/checkin/100xlabs.py",
  "script_args": {"checkin_text": "签到"}
}
"""

from __future__ import annotations

import asyncio
from typing import Any


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    args = dict(getattr(site, "script_args", {}) or {})

    def _list_arg(key: str, default: list[str]) -> list[str]:
        value = args.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return default

    checkin_texts = _list_arg(
        "checkin_text",
        ["签到", "每日签到", "立即签到", "领取", "今日领取", "Check in", "Claim", "now"],
    )
    already_texts = _list_arg(
        "already_text",
        ["已签到", "今日已签到", "已领取", "今日已领取", "Already", "Checked", "today"],
    )
    success_texts = _list_arg("success_text", ["签到成功", "领取成功", "成功", "获得", "Success"])
    goto_timeout = int(args.get("goto_timeout", 60000) or 60000)
    ready_timeout = int(args.get("ready_timeout", 10000) or 10000)
    click_timeout = int(args.get("click_timeout", 5000) or 5000)
    # SPA（React）站点：签到按钮要等前端拉取签到数据后才渲染，goto 后立即扫描会扑空。
    # 在放弃前轮询等待「已签到状态」或「签到按钮」出现，默认最多 25s（含浏览器/WAF 开销后，
    # 15s 曾在慢网络下与渲染擦肩而过）。
    button_wait_ms = int(args.get("button_wait_ms", 25000) or 25000)
    completion_timeout_ms = int(
        args.get("completion_timeout_ms", args.get("after_click_wait_ms", 10000)) or 10000
    )
    poll_interval_ms = max(20, int(args.get("poll_interval_ms", 100) or 100))
    start_url = str(args.get("start_url") or args.get("url") or "").strip()
    start_path = str(args.get("start_path") or args.get("path") or "/check-in").strip()
    target_url = start_url or start_path
    resolved_url = helpers.resolve_url(target_url)

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

    async def _find_already_control() -> tuple[str, Any] | None:
        for text in already_texts:
            try:
                locator = page.get_by_role("button", name=text, exact=False).first
            except Exception:
                continue
            if not await _is_visible(locator):
                continue
            # "today" 过于宽泛，只能由禁用按钮确认；其它完成文案仍兼容未禁用控件。
            if text.strip().casefold() != "today" or await _is_disabled(locator):
                return text, locator
        return None

    async def _find_already_text() -> str:
        for text in already_texts:
            # 避免把普通页面中的 today（例如标题/日期）误判为已签到。
            if text.strip().casefold() == "today":
                continue
            if await _visible_text(text):
                return text
        return ""

    async def _find_checkin_control() -> tuple[str, Any, str] | None:
        """找到可见且未禁用的签到控件（不点击）；找不到返回 None。"""
        # 先扫描所有 button/role=button，防止宽松文本候选点到页面标题。
        for text in checkin_texts:
            try:
                locator = page.get_by_role("button", name=text, exact=False).first
            except Exception:
                continue
            if not await _is_visible(locator) or await _is_disabled(locator):
                continue
            return text, locator, "button"

        # 部分 SPA 用 <a>/role=link 渲染签到入口（如 "Check in now" 链接按钮）。
        for text in checkin_texts:
            try:
                locator = page.get_by_role("link", name=text, exact=False).first
            except Exception:
                continue
            if not await _is_visible(locator) or await _is_disabled(locator):
                continue
            return text, locator, "link"

        # 兼容没有语义化 button/link 的旧页面，保留原有页面文本点击兜底。
        for text in checkin_texts:
            try:
                locator = page.get_by_text(text, exact=False).first
            except Exception:
                continue
            if not await _is_visible(locator):
                continue
            return text, locator, "text"
        return None

    async def _click_checkin() -> tuple[str, Any, str]:
        control = await _find_checkin_control()
        if control is None:
            return "", None, ""
        text, locator, kind = control
        try:
            element = await locator.element_handle()
        except Exception:
            element = None
        try:
            await locator.click(timeout=click_timeout)
            return text, element, kind
        except Exception:
            return "", None, ""

    await helpers.goto(target_url, timeout=goto_timeout, wait_until=str(args.get("wait_until") or "commit"))
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ready_timeout)
    except Exception:
        # 部分站点/风控页不会按时触发 domcontentloaded；继续用页面可见文本判断。
        pass

    # React SPA 的签到按钮要等前端 XHR 拉完签到数据后才渲染。先尽力等一次 networkidle，
    # 让首屏数据请求落地，明显降低「按钮刚要渲染、轮询窗口就到点」的临界竞态。失败/超时
    # 不致命，后面仍有 button_wait_ms 轮询兜底。
    try:
        await page.wait_for_load_state("networkidle", timeout=min(ready_timeout, 8000))
    except Exception:
        pass

    # SPA 站点签到按钮是前端拉取数据后才渲染的，goto 完成时通常还没出现。
    # 轮询等待，直到命中「已签到状态」或「可点击的签到按钮」，或超过 button_wait_ms。
    loop = asyncio.get_running_loop()
    button_deadline = loop.time() + max(0, button_wait_ms) / 1000
    checkin_control: tuple[str, Any, str] | None = None
    while True:
        already_control = await _find_already_control()
        if already_control:
            text, _ = already_control
            return helpers.already_done(
                "今日已签到",
                {"matched_text": text, "completion_signal": "button_state", "target_url": resolved_url},
            )
        matched_already_text = await _find_already_text()
        if matched_already_text:
            return helpers.already_done(
                "今日已签到",
                {"matched_text": matched_already_text, "completion_signal": "page_text", "target_url": resolved_url},
            )

        checkin_control = await _find_checkin_control()
        if checkin_control is not None:
            break

        if loop.time() >= button_deadline:
            break
        remaining_ms = max(1, int((button_deadline - loop.time()) * 1000))
        await page.wait_for_timeout(min(poll_interval_ms * 3, remaining_ms))

    if checkin_control is None:
        screenshot = await helpers.screenshot("100xlabs-no-checkin-button.png")
        return helpers.need_config(
            f"未找到签到按钮文本：{', '.join(checkin_texts)}",
            {
                "checkin_texts": checkin_texts,
                "target_url": resolved_url,
                "button_wait_ms": button_wait_ms,
                "screenshot": screenshot,
            },
        )

    checkin_response: dict[str, Any] = {}

    def _capture_checkin_response(response: Any) -> None:
        try:
            request = getattr(response, "request", None)
            method = str(getattr(request, "method", "") or "").upper()
            url = str(getattr(response, "url", "") or "")
            lowered_url = url.casefold()
            if method != "POST" or ("check-in" not in lowered_url and "checkin" not in lowered_url):
                return
            status = int(getattr(response, "status", 0) or 0)
            checkin_response.update({"status": status, "url": url})
        except Exception:
            return

    listener_registered = False
    try:
        page.on("response", _capture_checkin_response)
        listener_registered = True
    except Exception:
        pass

    try:
        # 点击轮询阶段已定位到的签到控件（避免重新扫描再次遇到 SPA 渲染时序问题）。
        found_text, found_locator, found_kind = checkin_control
        clicked_text, clicked_locator, clicked_kind = "", found_locator, found_kind
        try:
            clicked_element = await found_locator.element_handle()
        except Exception:
            clicked_element = None
        try:
            await found_locator.click(timeout=click_timeout)
            clicked_text, clicked_locator = found_text, clicked_element or found_locator
        except Exception:
            # 定位到但点击失败（可能刚好被重渲染替换）：兜底重新扫描点击一次。
            clicked_text, clicked_locator, clicked_kind = await _click_checkin()

        if not clicked_text:
            screenshot = await helpers.screenshot("100xlabs-click-failed.png")
            return helpers.error(
                "定位到签到按钮但点击失败，请稍后重试",
                {"checkin_texts": checkin_texts, "target_url": resolved_url, "screenshot": screenshot},
            )

        base_detail = {
            "clicked_text": clicked_text,
            "clicked_kind": clicked_kind,
            "target_url": resolved_url,
        }
        deadline = loop.time() + max(0, completion_timeout_ms) / 1000

        while True:
            response_status = int(checkin_response.get("status", 0) or 0)
            if 200 <= response_status < 300:
                return helpers.success(
                    "签到成功",
                    {
                        **base_detail,
                        "completion_signal": "checkin_response",
                        "response_status": response_status,
                        "response_url": checkin_response.get("url", ""),
                    },
                )
            if response_status >= 400:
                return helpers.error(
                    f"签到接口返回错误（HTTP {response_status}）",
                    {
                        **base_detail,
                        "completion_signal": "checkin_response",
                        "response_status": response_status,
                        "response_url": checkin_response.get("url", ""),
                    },
                )

            for text in success_texts:
                if await _visible_text(text):
                    return helpers.success(
                        "签到成功",
                        {**base_detail, "completion_signal": "success_text", "matched_text": text},
                    )

            already_control = await _find_already_control()
            if already_control:
                text, _ = already_control
                return helpers.success(
                    "签到成功",
                    {**base_detail, "completion_signal": "button_state", "matched_text": text},
                )

            matched_already_text = await _find_already_text()
            if matched_already_text:
                return helpers.success(
                    "签到成功",
                    {
                        **base_detail,
                        "completion_signal": "already_text",
                        "matched_text": matched_already_text,
                    },
                )

            if clicked_locator is not None and not await _is_visible(clicked_locator):
                return helpers.success(
                    "签到成功",
                    {**base_detail, "completion_signal": "button_hidden"},
                )

            if loop.time() >= deadline:
                break
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            await page.wait_for_timeout(min(poll_interval_ms, remaining_ms))

        screenshot = await helpers.screenshot("100xlabs-after-click.png")
        return helpers.error(
            "已点击签到按钮，但未检测到签到完成信号",
            {
                **base_detail,
                "completion_timeout_ms": completion_timeout_ms,
                "screenshot": screenshot,
            },
        )
    finally:
        if listener_registered:
            try:
                page.remove_listener("response", _capture_checkin_response)
            except Exception:
                pass
