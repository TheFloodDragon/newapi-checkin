#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器内的 WAF 检测、求解熔断与用户信息读取。"""

from __future__ import annotations

import asyncio
from typing import Any

from config import WAFConfig

from .runtime_loop import LogFn, is_driver_closed_error, noop, safe_goto

WAF_RETRY = WAFConfig.RETRY_ATTEMPTS
WAF_BLOCK_THRESHOLD = WAFConfig.BLOCK_THRESHOLD


def _quota_to_usd(value: Any) -> str:
    from providers.base import format_usd

    return format_usd(value, is_usd=False, fallback=str(value))


async def fetch_self(page: Any, base_url: str, fallback_uid: str) -> dict[str, Any] | None:
    """在页面上下文读取 ``/api/user/self``，包含同源 Cookie 与用户头。"""
    try:
        return await page.evaluate(
            """async ([baseUrl, fallbackUid, timeoutMs]) => {
                let uid = '';
                for (const key of ['user', 'auth_user']) {
                    try {
                        const stored = JSON.parse(localStorage.getItem(key) || '{}');
                        const id = stored.id ?? stored.user_id;
                        if (id != null && id !== '') { uid = String(id); break; }
                    } catch (_) { /* 忽略 */ }
                }
                if (!uid && fallbackUid) uid = String(fallbackUid);
                const headers = { 'Accept': 'application/json' };
                if (uid) headers['New-Api-User'] = uid;
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const r = await fetch(baseUrl + '/api/user/self', { credentials: 'include', headers, signal: controller.signal });
                    const t = await r.text();
                    const isWaf = /aliyun_waf|slidecaptcha|acw_sc__|Just a moment|cf-challenge/i.test(t);
                    try { return { ok: r.ok, status: r.status, uid, body: JSON.parse(t), is_waf: false }; }
                    catch { return { ok: r.ok, status: r.status, uid, body: t.slice(0, 200), is_waf: isWaf }; }
                } catch (e) {
                    return { ok: false, status: 0, uid, body: String(e && e.name === 'AbortError' ? 'fetch timeout' : e), is_waf: false };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            [base_url, fallback_uid, 15000],
        )
    except Exception as exc:
        if is_driver_closed_error(exc):
            raise
        return None


def waf_circuit(page: Any) -> dict[str, Any]:
    """返回附着在 page 上、跨多次求解共享的熔断状态。"""
    circuit = getattr(page, "_waf_circuit_state", None)
    if not isinstance(circuit, dict):
        circuit = {"fails": 0, "blocked": False}
        try:
            setattr(page, "_waf_circuit_state", circuit)
        except Exception:
            pass
    return circuit


def waf_is_blocked(page: Any) -> bool:
    """当前页面的出口 IP 是否已被判定为持续风控。"""
    return bool(waf_circuit(page).get("blocked"))


async def is_waf_html(page: Any) -> bool:
    """检测当前 HTML 是否仍为阿里云或 Cloudflare 挑战页。"""
    try:
        html = (await page.content() or "").lower()
    except Exception:
        return True
    return (
        "aliyun_waf" in html
        or "acw_sc__" in html
        or "slidecaptcha" in html
        or "just a moment" in html
        or "cf-challenge" in html
        or "checking your browser" in html
    )


async def wait_for_ready(page: Any, timeout_ms: int = 30000, log: LogFn = noop) -> bool:
    """等待页面脱离挑战并渲染出可见链接或按钮。"""
    ready_js = """() => {
        const text = document.body ? document.body.innerText : '';
        const blocked = /请进行验证|为了更好的访问体验|访问受限|Access denied|verify you are human|Just a moment|Checking your browser/i.test(text);
        if (blocked) return false;
        const isVisible = (el) => {
            if (!el || !el.isConnected) return false;
            const s = window.getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        };
        const countVisible = (sel) => [...document.querySelectorAll(sel)].filter(isVisible).length;
        return countVisible('a') > 0 || countVisible('button') > 0;
    }"""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass
    if await is_waf_html(page):
        log("页面为 WAF 挑战页，跳过就绪等待")
        return False
    try:
        await page.wait_for_function(ready_js, timeout=min(timeout_ms, 10000))
        return True
    except Exception:
        log("页面就绪检测超时（继续尝试操作）")
        return False


async def solve_waf(page: Any, base_url: str, log: LogFn = noop, rounds: int = 3) -> bool:
    """通过真实页面导航执行 WAF JS 挑战，并对持续失败进行熔断。"""
    circuit = waf_circuit(page)
    if circuit.get("blocked"):
        log("WAF 已熔断（出口 IP 被持续风控），跳过重复求解")
        return False

    for round_index in range(rounds):
        log(f"WAF 绕过尝试 {round_index + 1}/{rounds}（页面导航触发 JS 挑战）...")
        try:
            await safe_goto(page, base_url + "/console", wait_until="domcontentloaded", timeout=25000, log=log)
        except Exception as exc:
            if is_driver_closed_error(exc):
                raise
            log(f"WAF 求解导航中断（继续等待）：{type(exc).__name__}")
        for _ in range(15):
            await asyncio.sleep(1.0)
            if not await is_waf_html(page):
                log(f"WAF 挑战已通过（第 {round_index + 1} 轮）")
                circuit["fails"] = 0
                return True
        log(f"WAF 挑战未通过，重试 {round_index + 1}/{rounds}")

    circuit["fails"] = int(circuit.get("fails", 0)) + 1
    if circuit["fails"] >= WAF_BLOCK_THRESHOLD:
        circuit["blocked"] = True
        log(f"WAF 求解连续失败 {circuit['fails']} 次，判定出口 IP 被持续风控（熔断，后续跳过求解）")
    else:
        log(f"WAF 挑战求解失败（{circuit['fails']}/{WAF_BLOCK_THRESHOLD}，IP 可能被持续风控）")
    return False


async def read_user(
    page: Any,
    base_url: str,
    fallback_uid: str = "",
    log: LogFn = noop,
) -> dict[str, Any] | None:
    """读取用户信息，在 WAF 命中时求解并按配置重试。"""
    if await is_waf_html(page):
        if waf_is_blocked(page):
            log("WAF 已熔断（出口 IP 被持续风控），跳过读取额度")
            return None
        log("当前页面为 WAF 挑战页，先用页面导航求解...")
        await solve_waf(page, base_url, log, rounds=WAF_RETRY)
        if waf_is_blocked(page):
            return None

    for attempt in range(WAF_RETRY):
        result = await fetch_self(page, base_url, fallback_uid)
        if not isinstance(result, dict):
            await asyncio.sleep(1.0)
            continue

        body = result.get("body")
        if isinstance(body, dict) and body.get("success") and body.get("data"):
            data = body["data"]
            username = data.get("username") or ""
            quota = data.get("quota")
            log(f"当前用户：{username}，额度 {_quota_to_usd(quota)}")
            return data

        if result.get("is_waf") and attempt < WAF_RETRY - 1:
            if waf_is_blocked(page):
                log("WAF 已熔断，停止重试读取额度")
                break
            log(f"命中 WAF，导航求解后重试 {attempt + 1}/{WAF_RETRY - 1}")
            await solve_waf(page, base_url, log, rounds=2)
            if waf_is_blocked(page):
                log("WAF 已熔断，停止重试读取额度")
                break
            await asyncio.sleep(1.0)
            continue

        snippet = body if isinstance(body, str) else str(body)[:120]
        log(
            f"读取额度未成功：status={result.get('status')} uid={result.get('uid')!r} "
            f"waf={result.get('is_waf')} body={snippet}"
        )
        return None

    log("读取用户信息失败（登录态可能已失效或 WAF 无法绕过）")
    return None


__all__ = [
    "WAF_BLOCK_THRESHOLD",
    "WAF_RETRY",
    "fetch_self",
    "is_waf_html",
    "read_user",
    "solve_waf",
    "waf_circuit",
    "waf_is_blocked",
    "wait_for_ready",
]
