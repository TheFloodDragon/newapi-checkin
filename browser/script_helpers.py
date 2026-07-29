#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自定义浏览器脚本的便捷 helper。"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


class ScriptHelpers:
    """传给用户脚本的辅助对象。

    helper 只封装常用页面动作与结果格式；脚本仍可直接使用 Playwright 的
    page/context 完成更复杂的交互。
    """

    def __init__(
        self,
        page: Any,
        context: Any,
        site: Any,
        screenshot_dir: Path,
        log: Any = None,
    ) -> None:
        self.page = page
        self.context = context
        self.site = site
        self.screenshot_dir = screenshot_dir
        self._log = log

    def log(self, message: str) -> None:
        """输出一行脚本进度日志（stderr，已脱敏）。

        browser_script 流程动辄跑几十秒（启浏览器、过 WAF、登录、轮询按钮），
        此前脚本没有任何输出能力，失败时只能看到最终状态、无法判断卡在哪一步；
        而 relogin / newapi / sub2api 等路径早就有 _log。这里补齐能力，让站点
        脚本与其它 provider 的可观测性一致。

        worker 模式下 stdout 是机器协议通道，所以日志一律走 stderr。
        """
        text = str(message or "").strip()
        if not text:
            return
        if self._log is not None:
            try:
                self._log(text)
                return
            except Exception:
                pass
        from mask_utils import mask_secrets

        print(mask_secrets(text), file=sys.stderr, flush=True)

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

    # ── 图形验证码 ──
    def solve_captcha(self, image: Any, scheme: str = "newapi_bitmap") -> str:
        """识别签到图形验证码，返回答案文本；识别不确定时返回空串。

        `image` 接受 data URL、base64 字符串、PNG 字节流或 numpy 数组 —— 站点脚本
        通常直接把接口返回的 `captcha_image` 原样传进来。

        返回空串而不是抛异常，也不是返回「猜一个」：验证码 id 大多单次有效，
        识别不确定时换一张重试的成本远低于提交一次错误答案。调用方应据此循环。

        目前只有 `newapi_bitmap` 一种方案（New API fork 的点阵验证码，见
        captcha_ocr/newapi_bitmap.py）。纯 API 路径已在 providers/profiles/newapi.py
        内置同一识别器，这里是给必须走浏览器的站点脚本用的。
        """
        if scheme != "newapi_bitmap":
            self.log(f"未知验证码方案 {scheme!r}")
            return ""
        try:
            from captcha_ocr import newapi_bitmap
        except Exception as exc:
            self.log(f"验证码识别器不可用：{exc}")
            return ""

        try:
            if isinstance(image, str):
                result = newapi_bitmap.solve_data_url(image)
            elif isinstance(image, (bytes, bytearray)):
                result = newapi_bitmap.solve_bytes(bytes(image))
            else:
                result = newapi_bitmap.solve_array(image)
        except Exception as exc:
            self.log(f"验证码识别失败：{exc}")
            return ""
        if not result.exact:
            self.log(f"验证码识别不确定（{result.text or '空'}），建议换一张重试")
            return ""
        return result.text

    def success(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        awarded: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("success", message, detail, quota=quota, awarded=awarded, quota_is_usd=quota_is_usd)

    def already_done(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        awarded: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("already_done", message, detail, quota=quota, awarded=awarded, quota_is_usd=quota_is_usd)

    def need_login(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("need_login", message, detail, quota=quota, quota_is_usd=quota_is_usd)

    def need_verification(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("need_verification", message, detail, quota=quota, quota_is_usd=quota_is_usd)

    def need_config(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("need_config", message, detail, quota=quota, quota_is_usd=quota_is_usd)

    def error(
        self,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        return self._result("error", message, detail, quota=quota, quota_is_usd=quota_is_usd)

    @staticmethod
    def parse_quota(text: Any) -> float | None:
        """从页面文本里抽取金额，例如 "余额 $26.55" / "￥12,3.40" → 26.55 / 123.4。

        脚本常见做法是读一段带货币符号和千分位的文案，手写正则容易出错，
        这里统一处理：取第一个数字（允许千分位逗号与小数点），无法识别返回 None。
        """
        if isinstance(text, bool):
            return None
        if isinstance(text, (int, float)):
            return float(text)
        raw = str(text or "")
        match = re.search(r"-?\d[\d,]*(?:\.\d+)?", raw)
        if not match:
            return None
        try:
            return float(match.group(0).replace(",", ""))
        except ValueError:
            return None

    def _result(
        self,
        status: str,
        message: str,
        detail: dict[str, Any] | None = None,
        *,
        quota: Any = None,
        awarded: Any = None,
        quota_is_usd: bool = True,
    ) -> dict[str, Any]:
        """组装脚本结果；quota/awarded 会写成聚合层认识的标准键。

        额度提取的唯一实现在 providers.base（detail_current_quota /
        detail_quota_awarded），它按固定键名递归查找。脚本此前只能自己往 detail
        里塞字典、键名写错就静默丢额度，所以这里提供显式参数并负责映射：
        - quota   → detail["current_quota"]
        - awarded → detail["quota_awarded"]
        - quota_is_usd → detail["quota_is_usd"]，避免美元值被再除 500000。
        """
        out: dict[str, Any] = {"status": status, "message": message}
        merged: dict[str, Any] = dict(detail) if isinstance(detail, dict) else {}

        current = self.parse_quota(quota) if quota is not None else None
        gained = self.parse_quota(awarded) if awarded is not None else None
        if current is not None:
            merged["current_quota"] = current
        if gained is not None:
            merged["quota_awarded"] = gained
        if current is not None or gained is not None:
            # 脚本读到的通常已是站点展示的美元金额，标记后聚合层不会二次换算。
            merged["quota_is_usd"] = bool(quota_is_usd)

        if detail is not None and not isinstance(detail, dict):
            merged["script_detail"] = detail
        if merged:
            out["detail"] = merged
        elif detail is not None:
            out["detail"] = detail
        return out
