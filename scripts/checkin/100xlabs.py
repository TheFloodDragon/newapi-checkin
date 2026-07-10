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

    checkin_texts = _list_arg("checkin_text", ["签到", "每日签到", "立即签到", "领取", "今日领取", "Check in", "Claim"])
    already_texts = _list_arg("already_text", ["已签到", "今日已签到", "已领取", "今日已领取", "Already", "Checked"])
    success_texts = _list_arg("success_text", ["签到成功", "领取成功", "成功", "获得", "Success"])
    goto_timeout = int(args.get("goto_timeout", 60000) or 60000)
    ready_timeout = int(args.get("ready_timeout", 10000) or 10000)
    start_url = str(args.get("start_url") or args.get("url") or "").strip()
    start_path = str(args.get("start_path") or args.get("path") or "/check-in").strip()
    target_url = start_url or start_path

    await helpers.goto(target_url, timeout=goto_timeout, wait_until=str(args.get("wait_until") or "commit"))
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ready_timeout)
    except Exception:
        # 部分站点/风控页不会按时触发 domcontentloaded；继续用页面可见文本判断。
        pass

    for text in already_texts:
        if await helpers.visible_text(text, timeout=1500):
            return helpers.already_done("今日已签到", {"matched_text": text, "target_url": helpers.resolve_url(target_url)})

    clicked_text = ""
    for text in checkin_texts:
        if await helpers.click_text(text, timeout=5000):
            clicked_text = text
            break
    if not clicked_text:
        screenshot = await helpers.screenshot("100xlabs-no-checkin-button.png")
        return helpers.need_config(
            f"未找到签到按钮文本：{', '.join(checkin_texts)}",
            {"checkin_texts": checkin_texts, "target_url": helpers.resolve_url(target_url), "screenshot": screenshot},
        )

    await page.wait_for_timeout(int(args.get("after_click_wait_ms", 2000) or 2000))
    for text in success_texts:
        if await helpers.visible_text(text, timeout=5000):
            return helpers.success("签到成功", {"matched_text": text, "clicked_text": clicked_text, "target_url": helpers.resolve_url(target_url)})
    for text in already_texts:
        if await helpers.visible_text(text, timeout=3000):
            return helpers.already_done("今日已签到", {"matched_text": text, "clicked_text": clicked_text, "target_url": helpers.resolve_url(target_url)})

    screenshot = await helpers.screenshot("100xlabs-after-click.png")
    return helpers.success("已点击签到按钮，请按页面结果确认", {"clicked_text": clicked_text, "target_url": helpers.resolve_url(target_url), "screenshot": screenshot})
