#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集、脱敏并解释站点前端的 Toast、console 与响应错误。"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from .runtime_loop import LogFn, noop

SITE_ERROR_REDACTIONS = [
    (re.compile(r'(?i)("password"\s*:\s*")[^"]*(")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(\\"password\\"\s*:\s*\\")[^\\"]*(\\")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(password=)[^&\s]+'), r'\1<redacted>'),
    (re.compile(r'(?i)("(?:access_token|auth_token|token|state|code|cookie|authorization)"\s*:\s*")[^"]*(")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(\\"(?:access_token|auth_token|token|state|code|cookie|authorization)\\"\s*:\s*\\")[^\\"]*(\\")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)((?:access_token|auth_token|token|state|code|cookie|authorization)=)[^&\s]+'), r'\1<redacted>'),
    (re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*'), r'\1<redacted>'),
]

SITE_ERROR_NOISE = [
    "获取公告失败",
    "jshandle@",
    "cloudflareinsights",
    "beacon.min.js",
    "integrity attribute",
    "storage access automatically granted",
    "e.response is undefined",
    "google-analytics",
    "googletagmanager",
]


def short_body(body: Any, limit: int = 180) -> str:
    """把响应体压成适合诊断日志的一行短文本。"""
    try:
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":"))
        else:
            text = str(body or "")
    except Exception:
        text = str(body or "")
    return text.replace("\r", " ").replace("\n", " ")[:limit]


def redact_site_error(text: Any, limit: int = 500) -> str:
    """保留站点错误含义，同时移除密码、token、Cookie 等凭据。"""
    message = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    message = re.sub(r"\s+", " ", message)
    for pattern, replacement in SITE_ERROR_REDACTIONS:
        message = pattern.sub(replacement, message)
    return message[:limit]


def short_url(url: str) -> str:
    """移除 URL 的查询与片段，避免 OAuth code/state 进入日志。"""
    return str(url or "").split("#", 1)[0].split("?", 1)[0]


def add_site_error(collector: dict[str, Any] | None, source: str, message: Any) -> None:
    """向采集器追加一条去重、脱敏且过滤噪声的诊断。"""
    if collector is None:
        return
    text = redact_site_error(message)
    if not text:
        return
    lowered = text.lower()
    if any(pattern in lowered for pattern in SITE_ERROR_NOISE):
        return
    item = f"{source}: {text}" if source else text
    items = collector.setdefault("items", [])
    if item not in items:
        items.append(item)
        del items[:-12]


def _console_message_text(message: Any) -> str:
    try:
        text = getattr(message, "text", "")
        return text() if callable(text) else str(text or "")
    except Exception:
        return ""


def _console_message_type(message: Any) -> str:
    try:
        message_type = getattr(message, "type", "")
        return (message_type() if callable(message_type) else str(message_type or "")).lower()
    except Exception:
        return ""


def install_site_error_collector(
    page: Any,
    base_url: str = "",
    collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """在页面安装 console 和失败响应采集器。"""
    collector = collector or {"items": [], "tasks": []}
    try:
        page.on(
            "console",
            lambda message: add_site_error(
                collector,
                f"console.{_console_message_type(message) or 'log'}",
                _console_message_text(message),
            )
            if _console_message_type(message) in {"error", "warning", "assert"}
            else None,
        )
    except Exception:
        pass
    # 不注册 pageerror：Playwright Firefox 在部分缺失 location.url 的页面错误上会崩溃。

    async def _capture_response(response: Any) -> None:
        try:
            status = int(getattr(response, "status", 0) or 0)
            url = str(getattr(response, "url", "") or "")
            if status < 400:
                return
            lowered_url = url.lower()
            if any(value in lowered_url for value in ("googletagmanager", "google-analytics", "umami", "/assets/")):
                return
            if base_url and base_url.rstrip("/") not in url and "/api/" not in lowered_url and "oauth" not in lowered_url:
                return
            body = ""
            try:
                headers = getattr(response, "headers", {}) or {}
                content_type = str(headers.get("content-type") or "").lower()
                if not any(value in content_type for value in ("image/", "font/", "octet-stream")):
                    body = await response.text()
            except Exception:
                body = ""
            detail = f"HTTP {status} {short_url(url)}"
            if body:
                detail += f" body={short_body(body, 240)}"
            add_site_error(collector, "response", detail)
        except Exception:
            return

    def _on_response(response: Any) -> None:
        try:
            task = asyncio.create_task(_capture_response(response))
            tasks = collector.setdefault("tasks", [])
            tasks.append(task)
            if len(tasks) > 50:
                del tasks[:-50]
        except Exception:
            pass

    try:
        page.on("response", _on_response)
    except Exception:
        pass
    return collector


async def collect_dom_site_errors(page: Any, collector: dict[str, Any] | None = None) -> list[str]:
    """读取当前可见的 Toast、弹窗和表单错误。"""
    try:
        texts = await page.evaluate(
            r"""() => {
                const selectors = [
                    '.semi-toast-wrapper', '.semi-toast', '.semi-notification',
                    '.Toastify__toast', '[role="alert"]', '.semi-form-field-error-message',
                    '.semi-modal-content', '.ant-message', '.ant-notification', '.ant-alert'
                ];
                const visible = (el) => {
                    if (!el || !el.isConnected) return false;
                    const s = getComputedStyle(el);
                    const r = el.getBoundingClientRect();
                    return s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') !== 0 && r.width > 0 && r.height > 0;
                };
                const out = [];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const text = (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
                        if (text && visible(el) && text.length <= 1000 && !out.includes(text)) out.push(text);
                    }
                }
                return out.slice(0, 8);
            }"""
        )
        for text in texts or []:
            add_site_error(collector, "dom", text)
    except Exception:
        pass
    return list((collector or {}).get("items") or [])


async def site_error_messages(page: Any = None, collector: dict[str, Any] | None = None) -> list[str]:
    """等待在途响应采集，并合并 DOM 中仍可见的消息。"""
    if collector:
        all_tasks = list(collector.get("tasks", []))
        tasks = [task for task in all_tasks if not task.done()]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=2)
            except Exception:
                pass
        collector["tasks"] = [task for task in all_tasks if not task.done()][-50:]
    if page is not None:
        await collect_dom_site_errors(page, collector)
    return list((collector or {}).get("items") or [])


def _message_text(item: Any) -> str:
    text = redact_site_error(item)
    source, separator, message = text.partition(": ")
    if separator and source in {"dom", "toast", "notification"}:
        return message.strip()
    return text


def site_success_message(messages: list[str] | None) -> str:
    """从站点消息中提取明确的签到或登录奖励成功提示。"""
    success_patterns = (
        "签到成功",
        "领取成功",
        "登录成功",
        "奖励已发放",
        "额度已发放",
        "成功获得",
        "check-in success",
        "check in success",
        "checked in successfully",
        "login successful",
        "reward has been credited",
    )
    reject_patterns = ("失败", "错误", "未成功", "今日已", "已经签到", "already", "failed", "error")
    for item in messages or []:
        message = _message_text(item)
        lowered = message.casefold()
        if any(pattern in lowered for pattern in reject_patterns):
            continue
        if any(pattern in lowered for pattern in success_patterns):
            return message
    return ""


def attach_site_errors(target: dict[str, Any], errors: list[str], log: LogFn = noop) -> None:
    """把成功提示与真实错误分别附加到流程结果。"""
    if not errors:
        return
    success_message = site_success_message(errors)
    if success_message:
        target.setdefault("site_success_message", success_message)
        log(f"站点成功提示：{success_message}")
    error_items = [item for item in errors if _message_text(item) != success_message]
    if not error_items:
        return
    summary = "；".join(error_items[:3])
    target["site_errors"] = error_items
    target["site_error"] = summary
    log(f"站点原始错误：{summary}")


async def wait_for_site_success_message(
    page: Any,
    collector: dict[str, Any] | None,
    target: dict[str, Any],
    timeout_ms: int = 3000,
) -> str:
    """短暂轮询 OAuth 回跳页，捕获可能瞬时消失的成功 Toast。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000
    while True:
        messages = await site_error_messages(page, collector)
        attach_site_errors(target, messages)
        success_message = str(target.get("site_success_message") or "").strip()
        if success_message:
            return success_message
        if loop.time() >= deadline:
            return ""
        await asyncio.sleep(min(0.15, max(0, deadline - loop.time())))


def message_with_site_error(message: str, link: dict[str, Any]) -> str:
    """在主结果文案后附加已脱敏的站点错误。"""
    site_error = str(link.get("site_error") or "").strip()
    if not site_error:
        return message
    return f"{message} 站点原始错误：{site_error}"


__all__ = [
    "SITE_ERROR_NOISE",
    "SITE_ERROR_REDACTIONS",
    "add_site_error",
    "attach_site_errors",
    "collect_dom_site_errors",
    "install_site_error_collector",
    "message_with_site_error",
    "redact_site_error",
    "short_body",
    "short_url",
    "site_error_messages",
    "site_success_message",
    "wait_for_site_success_message",
]
