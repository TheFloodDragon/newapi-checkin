#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义浏览器脚本的便捷 helper。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


class ScriptHelpers:
    """传给用户脚本的辅助对象。

    helper 只封装常用页面动作与结果格式；脚本仍可直接使用 Playwright 的
    page/context 完成更复杂的交互。
    """

    def __init__(self, page: Any, context: Any, site: Any, screenshot_dir: Path) -> None:
        self.page = page
        self.context = context
        self.site = site
        self.screenshot_dir = screenshot_dir

    def resolve_url(self, url: str | None = None) -> str:
        """把绝对 URL 或站点相对路径解析为可导航 URL。"""
        target = (url or getattr(self.site, "base_url", "") or "").strip()
        if not target:
            raise ValueError("未提供跳转 URL，且 site.base_url 为空")
        parsed = urlparse(target)
        if parsed.scheme in {"http", "https"}:
            return target
        base_url = str(getattr(self.site, "base_url", "") or "").strip()
        if not base_url:
            raise ValueError(f"相对路径 {target!r} 需要 site.base_url")
        return urljoin(base_url.rstrip("/") + "/", target)

    async def goto(self, url: str | None = None, **kwargs: Any) -> Any:
        """跳转到目标页。

        默认只等待导航提交（commit），避免部分站点长期不触发
        domcontentloaded/load 导致脚本直接失败。若仍超时，默认吞掉超时并
        交给脚本继续检查当前页面；传 ignore_timeout=False 可恢复抛错行为。
        """
        target = self.resolve_url(url)
        ignore_timeout = bool(kwargs.pop("ignore_timeout", True))
        options = {"wait_until": "commit", "timeout": 60000}
        options.update(kwargs)
        try:
            return await self.page.goto(target, **options)
        except Exception as exc:
            is_timeout = "Timeout" in type(exc).__name__ or "Timeout" in str(exc)
            if ignore_timeout and is_timeout:
                return None
            raise

    async def visible_text(self, text: str, timeout: int = 1000) -> bool:
        if not text:
            return False
        try:
            locator = self.page.get_by_text(text, exact=False).first
            await locator.wait_for(state="visible", timeout=timeout)
            return True
        except Exception:
            return False

    async def click_text(self, text: str, timeout: int = 5000) -> bool:
        if not text:
            return False
        try:
            locator = self.page.get_by_text(text, exact=False).first
            await locator.wait_for(state="visible", timeout=timeout)
            await locator.click(timeout=timeout)
            return True
        except Exception:
            return False

    async def click_first(self, selectors: list[str], timeout: int = 5000) -> bool:
        for selector in selectors or []:
            if not selector:
                continue
            try:
                locator = self.page.locator(selector).first
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click(timeout=timeout)
                return True
            except Exception:
                continue
        return False

    async def wait_text(self, text: str, timeout: int = 10000) -> bool:
        return await self.visible_text(text, timeout=timeout)

    async def screenshot(self, name: str = "browser_script.png") -> str:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", name or "browser_script.png").strip("._")
        if not safe_name:
            safe_name = "browser_script.png"
        if "." not in Path(safe_name).name:
            safe_name += ".png"
        path = self.screenshot_dir / safe_name
        try:
            await self.page.screenshot(path=str(path), full_page=True)
        except Exception:
            return ""
        return str(path)

    def success(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("success", message, detail)

    def already_done(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("already_done", message, detail)

    def need_login(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("need_login", message, detail)

    def need_verification(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("need_verification", message, detail)

    def need_config(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("need_config", message, detail)

    def error(self, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._result("error", message, detail)

    def _result(self, status: str, message: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"status": status, "message": message}
        if detail is not None:
            out["detail"] = detail
        return out
