#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_session —— 浏览器登录态的共享操作（async 版本，基于 Camoufox）。

核心功能（CLI 与 GUI 复用）：
1. capture_login：有头浏览器人工登录捕获站点登录态（storage_state）。
2. capture_oauth_state：有头浏览器人工捕获 linux.do/github 共享登录态。
3. verify_state：验证站点登录态是否有效（读 /api/user/self）。
4. run_oauth_checkin：自动重放 OAuth 登录触发发额度（真正的签到）。

技术架构：
- 浏览器：Camoufox（Firefox 反检测，绕过 webdriver 检测）。
- 绕过：集成 Cloudflare cf_clearance、阿里云 WAF cookies、滑块拖拽。
- 登录态：Playwright storage_state（跨平台 JSON，含 cookies + localStorage）。
- 异步：全面改用 asyncio，提升并发性能。

依赖：
- camoufox[geoip]：反检测浏览器。
- playwright-captcha：验证码破解。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from . import bypass, oauth_providers, popups, state

# WAF/OAuth 相关配置集中到 config 模块
from config import WAFConfig, Timeouts

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # checkin/
QUOTA_UNIT = 500_000
OAUTH_WAIT_SECONDS = Timeouts.OAUTH_WAIT
WAF_RETRY = WAFConfig.RETRY_ATTEMPTS
# 连续多少次「整轮」WAF 求解失败后，判定出口 IP 被阿里云 WAF 持续风控（熔断），
# 后续跳过所有重复求解，避免在被风控的 IP 上空耗数分钟。
WAF_BLOCK_THRESHOLD = WAFConfig.BLOCK_THRESHOLD


def _env_headless() -> bool:
    """读取 CHECKIN_HEADLESS 环境变量决定无头模式。

    显式设置优先；未设置时 CI/GitHub Actions 默认无头，本地默认有头。
    """
    raw = os.getenv("CHECKIN_HEADLESS", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))


# OAuth 登录入口候选选择器（站点未显式配置 login_selector 时使用）
DEFAULT_LOGIN_SELECTORS = [
    "text=/linux.?do/i",
    "text=/使用.*登录/i",
    "text=/登录|登入|Sign in|Log in/i",
    "[href*='oauth']",
    "[href*='/login']",
    "button:has-text('Linux')",
    "button:has-text('GitHub')",
    "text=/github/i",
]


class BrowserSessionError(Exception):
    """浏览器会话相关错误（供 provider 捕获）。"""


LogFn = Callable[[str], None]


def _noop(_msg: str) -> None:
    pass


def _origin_from_url(url: str) -> str:
    parsed = urlparse(str(url or ""))
    if not parsed.scheme or not parsed.netloc:
        return str(url or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"


def _browser_mode_label(headless: bool) -> str:
    ci = bool(os.getenv("GITHUB_ACTIONS") or os.getenv("CI"))
    return f"{'headless' if headless else 'headful'} / {'CI' if ci else 'local'}"


def _site_cookie_string(cookies: list[dict[str, Any]], base_url: str) -> str:
    """从 Playwright cookies 里提取站点域的 cookie，拼成 "k=v; k2=v2"。

    只保留与站点 host 同域（含父域）的 cookie，过滤掉 linux.do/github 等第三方域，
    避免把无关的第三方登录态 cookie 发给站点接口。重复键保留最后一个。
    """
    host = urlparse(_origin_from_url(base_url)).netloc.lower()
    if not host:
        return ""
    pairs: dict[str, str] = {}
    for cookie in cookies or []:
        name = str(cookie.get("name") or "")
        value = cookie.get("value")
        if not name or value is None:
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if not domain:
            continue
        # 站点 host 等于 cookie 域，或是其子域（cookie 域为站点父域）
        if host == domain or host.endswith("." + domain) or domain.endswith("." + host):
            pairs[name] = str(value)
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


# 驱动/浏览器已关闭的错误特征（模块级常量，避免每次调用重建元组）
_DRIVER_CLOSED_MARKERS = (
    "connection closed",
    "target closed",
    "browser has been closed",
    "browser closed",
    "page closed",
    "socket.send()",
    "closed while reading from the driver",
    "playwright driver",
    "pipe closed by peer",
    "os.write(pipe",
    "cannot read properties of undefined (reading 'url')",
)


def _is_driver_closed_error(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _DRIVER_CLOSED_MARKERS)


async def _safe_close_page(page) -> None:
    if page is None:
        return
    try:
        await page.close()
    except Exception:
        pass


async def _safe_close_browser(browser) -> None:
    if browser is None:
        return
    try:
        await browser.close()
    except Exception:
        pass


async def _safe_storage_state(context, log: LogFn = _noop) -> dict[str, Any]:
    try:
        return await context.storage_state()
    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise BrowserSessionError(
                "浏览器驱动已关闭，无法导出登录态；这通常是站点页面脚本触发了 Playwright Firefox 兼容问题，请重试。"
            ) from exc
        raise


async def _safe_goto(page, url: str, *, wait_until: str = "domcontentloaded", timeout: int = 30000, log: LogFn = _noop) -> bool:
    """容错导航；domcontentloaded 失败时降级到 commit，驱动断连则继续抛出。"""
    try:
        await page.goto(url, wait_until=wait_until, timeout=timeout)
        return True
    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise
        if wait_until != "commit":
            try:
                await page.goto(url, wait_until="commit", timeout=min(timeout, 15000))
                log(f"导航等待 {wait_until} 失败，已降级到 commit：{url}")
                return True
            except Exception as retry_exc:
                if _is_driver_closed_error(retry_exc):
                    raise
                log(f"导航失败（{type(exc).__name__}，降级也失败：{type(retry_exc).__name__}）：{url}")
                return False
        log(f"导航失败（{type(exc).__name__}）：{url}")
        return False


async def _fetch_json_in_page(page, url: str, timeout_ms: int = 15000) -> dict[str, Any] | None:
    """在页面上下文 fetch JSON，使用 AbortController 避免长期挂起。"""
    try:
        return await page.evaluate(
            """async ([u, timeoutMs]) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const r = await fetch(u, {
                        credentials: 'include',
                        headers: { 'Accept': 'application/json' },
                        signal: controller.signal,
                    });
                    const t = await r.text();
                    try { return { ok: r.ok, status: r.status, body: JSON.parse(t) }; }
                    catch { return { ok: r.ok, status: r.status, body: t.slice(0, 200) }; }
                } catch (e) {
                    return { ok: false, status: 0, body: String(e && e.name === 'AbortError' ? 'fetch timeout' : e) };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            [url, timeout_ms],
        )
    except Exception:
        return None


def _patch_windows_asyncio_finalizers() -> None:
    """静默 Windows Proactor 管道关闭后的 __del__ 噪声。"""
    if os.name != "nt":
        return
    try:
        import asyncio.base_subprocess as base_subprocess
        import asyncio.proactor_events as proactor_events
    except Exception:
        return

    def _wrap(cls) -> None:
        if getattr(cls, "_checkin_safe_del", False):
            return
        original = getattr(cls, "__del__", None)
        if original is None:
            return

        def _safe_del(self):
            try:
                original(self)
            except ValueError as exc:
                if "I/O operation on closed pipe" not in str(exc):
                    raise
            except Exception:
                pass

        cls.__del__ = _safe_del
        cls._checkin_safe_del = True

    _wrap(proactor_events._ProactorBasePipeTransport)
    _wrap(base_subprocess.BaseSubprocessTransport)


def _run_loop(loop: asyncio.AbstractEventLoop, coro: Any) -> Any:
    """在给定 loop 上运行协程，并在 finally 中做优雅清理。

    适合在独立线程里调用（该线程已通过 asyncio.set_event_loop(loop) 绑定）。
    Windows 专有清理逻辑（sleep + shutdown_asyncgens）同样在此执行。
    """
    import sys as _sys

    try:
        return loop.run_until_complete(coro)
    finally:
        # 取消所有残留 task（例如 Playwright Connection.run）
        try:
            pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
            for t in pending:
                t.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        # Windows：给传输一点时间优雅关闭，避免 __del__ 阶段的管道错误日志
        if _sys.platform == "win32":
            try:
                loop.run_until_complete(asyncio.sleep(0.3))
            except Exception:
                pass
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def run_sync(coro: Any) -> Any:
    """同步执行一个 async 协程，并规避 Windows ProactorEventLoop 的清理警告。

    Windows 上 Camoufox/Playwright 子进程退出时，asyncio 在 __del__ 里清理
    管道传输会抛 "I/O operation on closed pipe" / "unclosed transport"。
    通过手动创建 loop + 退出前 sleep 让传输优雅关闭，避免污染日志。

    当调用线程已有一个正在运行的 event loop（例如 Jupyter / FastAPI / GUI 框架）时，
    直接 loop.run_until_complete() 会抛 RuntimeError。此时把协程提交到一个独立线程
    的新 loop 里执行，确保阻塞等待完成后再返回结果；任何异常都会原样重新抛出。
    协程仅被调度一次，不会泄漏。
    """
    import concurrent.futures
    import sys as _sys

    _patch_windows_asyncio_finalizers()

    # 探测当前线程是否已有运行中的 event loop
    _running_loop: asyncio.AbstractEventLoop | None = None
    try:
        _running_loop = asyncio.get_running_loop()
    except RuntimeError:
        _running_loop = None

    if _running_loop is not None:
        # 当前线程在 loop 内 —— 必须在独立线程里跑新 loop，否则会死锁。
        # 用 concurrent.futures.Future 把结果/异常传回主线程。
        result_future: concurrent.futures.Future = concurrent.futures.Future()

        def _thread_target() -> None:
            if _sys.platform == "win32":
                try:
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                except Exception:
                    pass
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                value = _run_loop(new_loop, coro)
                result_future.set_result(value)
            except BaseException as exc:  # noqa: BLE001
                result_future.set_exception(exc)

        t = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        t.submit(_thread_target)
        t.shutdown(wait=True)
        # 重新抛出在子线程中捕获的异常（含原始 traceback）
        return result_future.result()

    # 普通路径：当前线程没有运行中的 event loop
    if _sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
        except Exception:
            pass

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return _run_loop(loop, coro)


def quota_to_usd(value: Any) -> str:
    try:
        return f"${float(value) / QUOTA_UNIT:.4g}"
    except (TypeError, ValueError):
        return str(value)


# ───────────────────────── 辅助函数：/api/user/self ────────────────────────
async def _fetch_self(page, base_url: str, fallback_uid: str) -> dict[str, Any] | None:
    """在【页面上下文】里 fetch /api/user/self，返回 {ok,status,uid,body,is_waf}。

    关键：用 page.evaluate 在页面里 fetch（credentials:'include' 带同源 cookie），
    并从 localStorage 的 user/auth_user 读取真实 uid 作为 New-Api-User 头。
    New API 的 /api/user/self 必须带正确的 New-Api-User，否则拒绝返回数据。
    """
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
        # Let a dead driver bubble up so the caller can stop instead of spinning.
        if _is_driver_closed_error(exc):
            raise
        return None


def _waf_circuit(page) -> dict[str, Any]:
    """返回附着在 page 上的 WAF 熔断状态（跨多次求解调用共享）。

    结构：{"fails": 连续整轮失败次数, "blocked": 是否已熔断}。
    出口 IP 被阿里云 WAF 持续风控时，靠这个状态短路后续所有求解，避免空耗。
    """
    state = getattr(page, "_waf_circuit_state", None)
    if not isinstance(state, dict):
        state = {"fails": 0, "blocked": False}
        try:
            setattr(page, "_waf_circuit_state", state)
        except Exception:
            # page 是 C 扩展对象、无法附加属性时，退化为「无熔断」（每次新建）。
            pass
    return state


def _waf_is_blocked(page) -> bool:
    """当前 page 的出口 IP 是否已被判定为持续风控（熔断开启）。"""
    return bool(_waf_circuit(page).get("blocked"))


async def _is_waf_html(page) -> bool:
    """检测当前页面是否仍是 WAF 拦截 / 挑战页（阿里云 / Cloudflare）。

    阿里云 WAF 挑战页的特征是 HTML 源码里的 meta 标签（aliyun_waf_aa/bb），
    用 innerText 检测不到，必须取 page.content() 的完整 HTML 源码匹配。
    """
    try:
        html = (await page.content() or "").lower()
    except Exception:
        # content() 在 reload 瞬间可能抛异常，视为「仍在挑战中」
        return True
    return (
        "aliyun_waf" in html
        or "acw_sc__" in html
        or "slidecaptcha" in html
        or "just a moment" in html
        or "cf-challenge" in html
        or "checking your browser" in html
    )


async def _wait_for_ready(page, timeout_ms: int = 30000, log: LogFn = _noop) -> bool:
    """等待页面真正可交互：WAF 通过 + 有可见的链接/按钮元素。

    用 JS 探测排除 WAF 拦截文本，并确认页面渲染出可交互元素（SPA 站点
    DOM 加载完不代表元素已渲染）。失败仅警告不抛异常，保持容错。

    返回 True 表示页面就绪，False 表示超时/未就绪（调用方可继续尝试）。
    """
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
    # 若是 WAF 挑战页，提前返回（交由 read_user 的 _solve_waf 处理，不空等）
    if await _is_waf_html(page):
        log("页面为 WAF 挑战页，跳过就绪等待")
        return False
    try:
        # 就绪检测超时取较短值（10s），避免在异常页面空等
        await page.wait_for_function(ready_js, timeout=min(timeout_ms, 10000))
        return True
    except Exception:
        log("页面就绪检测超时（继续尝试操作）")
        return False


async def _solve_waf(page, base_url: str, log: LogFn = _noop, rounds: int = 3) -> bool:
    """用页面导航触发并等待阿里云 WAF 的 JS 挑战自动求解。

    阿里云 WAF 的 aliyun_waf_aa/bb 挑战机制：返回的 HTML 内嵌 JS，浏览器执行后
    计算出 acw_sc__v2 cookie 并【自动 reload】，带上该 cookie 后续请求即放行。
    fetch() 不执行挑战页的 JS，所以必须用真实页面导航 + 等待自动 reload。

    关键：不手动调 page.reload（会与 WAF 的自动 reload 冲突导致导航 pending 超时），
    而是导航后轮询等待页面内容脱离挑战页。

    返回 True 表示页面最终不再是 WAF 挑战页（挑战已解或本就无挑战）。

    熔断：连续 WAF_BLOCK_THRESHOLD 次「整轮」求解失败后，判定出口 IP 被持续风控，
    之后任何调用直接短路返回 False，不再重复导航，避免在被封 IP 上空耗数分钟。
    """
    circuit = _waf_circuit(page)
    if circuit.get("blocked"):
        log("WAF 已熔断（出口 IP 被持续风控），跳过重复求解")
        return False

    for r in range(rounds):
        log(f"WAF 绕过尝试 {r + 1}/{rounds}（页面导航触发 JS 挑战）...")
        try:
            # domcontentloaded 即返回；挑战 JS 随后执行并自动 reload
            await _safe_goto(page, base_url + "/console", wait_until="domcontentloaded", timeout=25000, log=log)
        except Exception as exc:
            # A dead driver would make every following poll spin uselessly.
            # Only WAF auto-reload navigation aborts are expected here; bubble
            # up real driver/browser crashes so the flow stops fast.
            if _is_driver_closed_error(exc):
                raise
            # 导航可能因 WAF 自动 reload 中断，属正常，忽略继续轮询
            log(f"WAF 求解导航中断（继续等待）：{type(exc).__name__}")
        # 轮询等待挑战页被 WAF JS 替换（最多 ~15s 每轮）
        for _ in range(15):
            await asyncio.sleep(1.0)
            if not await _is_waf_html(page):
                log(f"WAF 挑战已通过（第 {r + 1} 轮）")
                circuit["fails"] = 0  # 成功一次即重置失败计数
                return True
        log(f"WAF 挑战未通过，重试 {r + 1}/{rounds}")

    # 整轮求解失败：累加失败计数，达到阈值则熔断
    circuit["fails"] = int(circuit.get("fails", 0)) + 1
    if circuit["fails"] >= WAF_BLOCK_THRESHOLD:
        circuit["blocked"] = True
        log(f"WAF 求解连续失败 {circuit['fails']} 次，判定出口 IP 被持续风控（熔断，后续跳过求解）")
    else:
        log(f"WAF 挑战求解失败（{circuit['fails']}/{WAF_BLOCK_THRESHOLD}，IP 可能被持续风控）")
    return False


async def read_user(
    page,
    base_url: str,
    fallback_uid: str = "",
    log: LogFn = _noop,
) -> dict[str, Any] | None:
    """读取 /api/user/self 用户信息（含 WAF 绕过 + 重试）。

    Args:
        page: Playwright Page 对象（已登录）。
        base_url: 站点地址（如 "https://example.com"）。
        fallback_uid: 兜底用户 ID（用于 New-Api-User 头）。
        log: 日志回调。

    Returns:
        用户数据 dict（含 id/username/quota）或 None。
    """
    # 若当前页面本身就是 WAF 挑战页，先用页面导航解挑战（fetch 不执行挑战 JS）
    if await _is_waf_html(page):
        if _waf_is_blocked(page):
            log("WAF 已熔断（出口 IP 被持续风控），跳过读取额度")
            return None
        log("当前页面为 WAF 挑战页，先用页面导航求解...")
        await _solve_waf(page, base_url, log, rounds=WAF_RETRY)
        # 熔断后不再进入 fetch 重试循环，直接返回失败（由调用方判定为 WAF 风控）
        if _waf_is_blocked(page):
            return None

    for attempt in range(WAF_RETRY):
        result = await _fetch_self(page, base_url, fallback_uid)
        if not isinstance(result, dict):
            await asyncio.sleep(1.0)
            continue

        body = result.get("body")
        # 成功：New API 返回 {success:true, data:{...}}
        if isinstance(body, dict) and body.get("success") and body.get("data"):
            data = body["data"]
            username = data.get("username") or ""
            quota = data.get("quota")
            log(f"当前用户：{username}，额度 {quota_to_usd(quota)}")
            return data

        # 命中 WAF：用页面导航求解挑战后重试；熔断后不再重试，快速失败
        if result.get("is_waf") and attempt < WAF_RETRY - 1:
            if _waf_is_blocked(page):
                log("WAF 已熔断，停止重试读取额度")
                break
            log(f"命中 WAF，导航求解后重试 {attempt + 1}/{WAF_RETRY - 1}")
            await _solve_waf(page, base_url, log, rounds=2)
            if _waf_is_blocked(page):
                log("WAF 已熔断，停止重试读取额度")
                break
            await asyncio.sleep(1.0)
            continue

        # 诊断日志（uid / 状态 / 响应片段），便于排错
        snippet = body if isinstance(body, str) else str(body)[:120]
        log(
            f"读取额度未成功：status={result.get('status')} uid={result.get('uid')!r} "
            f"waf={result.get('is_waf')} body={snippet}"
        )
        return None

    log("读取用户信息失败（登录态可能已失效或 WAF 无法绕过）")
    return None


# ────────────────────────── OAuth 登录触发 ──────────────────────────────

async def _api_get_json(page, url: str) -> dict[str, Any] | None:
    """在页面上下文里 GET 一个 JSON 接口（带同源 cookie + 超时）。"""
    return await _fetch_json_in_page(page, url, timeout_ms=15000)


def _short_body(body: Any, limit: int = 180) -> str:
    try:
        if isinstance(body, (dict, list)):
            text = json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":"))
        else:
            text = str(body or "")
    except Exception:
        text = str(body or "")
    return text.replace("\r", " ").replace("\n", " ")[:limit]


def _extract_oauth_state(body: Any) -> str:
    """兼容不同 New API 派生站的 state 响应结构。"""
    if isinstance(body, dict):
        for key in ("data", "state", "oauth_state", "oauthState"):
            val = body.get(key)
            if isinstance(val, dict):
                nested = _extract_oauth_state(val)
                if nested:
                    return nested
            elif val:
                return str(val)
    elif isinstance(body, str):
        text = body.strip()
        if text and not text.startswith("<") and len(text) <= 512:
            return text
    return ""


_SITE_ERROR_REDACTIONS = [
    (re.compile(r'(?i)("password"\s*:\s*")[^"]*(")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(\\"password\\"\s*:\s*\\")[^\\"]*(\\")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(password=)[^&\s]+'), r'\1<redacted>'),
    (re.compile(r'(?i)("(?:access_token|auth_token|token|state|code|cookie|authorization)"\s*:\s*")[^"]*(")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)(\\"(?:access_token|auth_token|token|state|code|cookie|authorization)\\"\s*:\s*\\")[^\\"]*(\\")'), r'\1<redacted>\2'),
    (re.compile(r'(?i)((?:access_token|auth_token|token|state|code|cookie|authorization)=)[^&\s]+'), r'\1<redacted>'),
    (re.compile(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/-]+=*'), r'\1<redacted>'),
]


def _redact_site_error(text: Any, limit: int = 500) -> str:
    """保留站点原始错误含义，同时避免把密码/token/cookie 带进日志和返回值。"""
    msg = str(text or "").replace("\r", " ").replace("\n", " ").strip()
    msg = re.sub(r"\s+", " ", msg)
    for pattern, repl in _SITE_ERROR_REDACTIONS:
        msg = pattern.sub(repl, msg)
    return msg[:limit]


def _short_url(url: str) -> str:
    return str(url or "").split("#", 1)[0].split("?", 1)[0]


def _add_site_error(collector: dict[str, Any] | None, source: str, message: Any) -> None:
    if collector is None:
        return
    text = _redact_site_error(message)
    if not text:
        return
    item = f"{source}: {text}" if source else text
    items = collector.setdefault("items", [])
    if item not in items:
        items.append(item)
        del items[:-12]


def _console_message_text(msg) -> str:
    try:
        text = getattr(msg, "text", "")
        return text() if callable(text) else str(text or "")
    except Exception:
        return ""


def _console_message_type(msg) -> str:
    try:
        typ = getattr(msg, "type", "")
        return (typ() if callable(typ) else str(typ or "")).lower()
    except Exception:
        return ""


def _install_site_error_collector(page, base_url: str = "", collector: dict[str, Any] | None = None) -> dict[str, Any]:
    """采集站点前端原始错误：Toast/console/接口错误响应。"""
    collector = collector or {"items": [], "tasks": []}
    try:
        page.on("console", lambda msg: _add_site_error(
            collector,
            f"console.{_console_message_type(msg) or 'log'}",
            _console_message_text(msg),
        ) if _console_message_type(msg) in {"error", "warning", "assert"} else None)
    except Exception:
        pass
    # 不注册 pageerror：Playwright Firefox 驱动在部分页面错误缺少 location.url 时会崩溃。

    async def _capture_response(response) -> None:
        try:
            status = int(getattr(response, "status", 0) or 0)
            url = str(getattr(response, "url", "") or "")
            if status < 400:
                return
            low_url = url.lower()
            if any(x in low_url for x in ("googletagmanager", "google-analytics", "umami", "/assets/")):
                return
            if base_url and base_url.rstrip("/") not in url and "/api/" not in low_url and "oauth" not in low_url:
                return
            body = ""
            try:
                headers = getattr(response, "headers", {}) or {}
                content_type = str(headers.get("content-type") or "").lower()
                if not any(x in content_type for x in ("image/", "font/", "octet-stream")):
                    body = await response.text()
            except Exception:
                body = ""
            detail = f"HTTP {status} {_short_url(url)}"
            if body:
                detail += f" body={_short_body(body, 240)}"
            _add_site_error(collector, "response", detail)
        except Exception:
            return

    def _on_response(response) -> None:
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


async def _collect_dom_site_errors(page, collector: dict[str, Any] | None = None) -> list[str]:
    """从页面 DOM 中读取当前可见的 Toast / 弹窗 / 表单错误。"""
    try:
        texts = await page.evaluate(
            """() => {
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
                        const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        if (text && visible(el) && text.length <= 1000 && !out.includes(text)) out.push(text);
                    }
                }
                return out.slice(0, 8);
            }"""
        )
        for text in texts or []:
            _add_site_error(collector, "dom", text)
    except Exception:
        pass
    return list((collector or {}).get("items") or [])


async def _site_error_messages(page=None, collector: dict[str, Any] | None = None) -> list[str]:
    if collector:
        all_tasks = list(collector.get("tasks", []))
        tasks = [t for t in all_tasks if not t.done()]
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=2)
            except Exception:
                pass
        collector["tasks"] = [t for t in all_tasks if not t.done()][-50:]
    if page is not None:
        await _collect_dom_site_errors(page, collector)
    return list((collector or {}).get("items") or [])


def _message_text(item: Any) -> str:
    text = _redact_site_error(item)
    source, separator, message = text.partition(": ")
    if separator and source in {"dom", "toast", "notification"}:
        return message.strip()
    return text


def _site_success_message(messages: list[str] | None) -> str:
    """从站点 Toast/弹窗中提取明确的签到或登录奖励成功提示。"""
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
    reject_patterns = (
        "失败",
        "错误",
        "未成功",
        "今日已",
        "已经签到",
        "already",
        "failed",
        "error",
    )
    for item in messages or []:
        message = _message_text(item)
        lowered = message.casefold()
        if any(pattern in lowered for pattern in reject_patterns):
            continue
        if any(pattern in lowered for pattern in success_patterns):
            return message
    return ""


def _attach_site_errors(target: dict[str, Any], errors: list[str], log: LogFn = _noop) -> None:
    if not errors:
        return
    success_message = _site_success_message(errors)
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


async def _wait_for_site_success_message(
    page,
    collector: dict[str, Any] | None,
    target: dict[str, Any],
    timeout_ms: int = 3000,
) -> str:
    """短暂轮询 OAuth 回跳页，避免瞬时成功 Toast 消失后只能按额度差判断。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000
    while True:
        messages = await _site_error_messages(page, collector)
        _attach_site_errors(target, messages)
        success_message = str(target.get("site_success_message") or "").strip()
        if success_message:
            return success_message
        if loop.time() >= deadline:
            return ""
        await asyncio.sleep(min(0.15, max(0, deadline - loop.time())))


def _message_with_site_error(message: str, link: dict[str, Any]) -> str:
    site_error = str(link.get("site_error") or "").strip()
    if not site_error:
        return message
    return f"{message} 站点原始错误：{site_error}"


def _oauth_checkin_result(quota_before: Any, quota_after: Any, link: dict[str, Any]) -> dict[str, Any]:
    """综合额度变化、OAuth 回跳状态和站点弹窗生成签到结果。"""
    result: dict[str, Any] = {
        "quota_before": quota_before,
        "quota_after": quota_after,
        "delta": None,
        "link": link,
    }

    if quota_before is not None and quota_after is not None and quota_after > quota_before:
        delta = quota_after - quota_before
        result["delta"] = delta
        result["status"] = "success"
        result["message"] = f"OAuth 重登成功，额度增加 {quota_to_usd(delta)}（当前 {quota_to_usd(quota_after)}）。"
        return result

    success_message = str(link.get("site_success_message") or "").strip()
    oauth_completed = (
        link.get("landed_back")
        and not link.get("cloudflare")
        and not link.get("need_human")
        and not link.get("waf_blocked")
    )
    if oauth_completed and success_message:
        result["status"] = "success"
        result["message"] = f"签到成功（站点弹窗：{success_message}）。"
        return result

    if quota_before is None and quota_after is None:
        if link.get("waf_blocked"):
            # 出口 IP 被阿里云 WAF 持续风控：登录态本身可能仍有效，不是登录问题。
            result["status"] = "need_verification"
            result["message"] = _message_with_site_error(
                "站点阿里云 WAF 持续拦截当前出口 IP（数据中心/CI IP 信誉过低），"
                "浏览器无法通过 JS 挑战，本次签到中止。登录态可能仍有效，无需重新捕获；"
                "请为该账号配置住宅代理（proxy 字段），或改用住宅 IP 环境运行。",
                link,
            )
        elif link.get("cloudflare"):
            result["status"] = "need_verification"
            result["message"] = _message_with_site_error(
                "OAuth 过程命中 Cloudflare/WAF 人机验证，无法自动完成，请重新捕获登录态。",
                link,
            )
        else:
            result["status"] = "need_login"
            result["message"] = _message_with_site_error("无法读取额度，登录态可能已失效，请重新捕获登录态。", link)
        return result

    if oauth_completed:
        cur = quota_after if quota_after is not None else quota_before
        result["status"] = "already_done"
        result["message"] = f"OAuth 重登完成，额度无变化（当前 {quota_to_usd(cur)}，今日可能已发放）。"
        return result

    reason = (
        "停在第三方登录页（共享登录态可能已过期）"
        if link.get("need_human")
        else ("OAuth 授权未带 code 顺畅跳回站点" if not link.get("landed_back") else "OAuth 链路未顺畅完成")
    )
    result["status"] = "need_login"
    result["message"] = _message_with_site_error(f"OAuth 自动重登未完成：{reason}。请重新捕获登录态。", link)
    return result


async def _fetch_oauth_client_id(page, base_url: str, provider) -> tuple[str, bool]:
    """从 {origin}/api/status 读取该 provider 的 client_id 与开关。"""
    res = await _api_get_json(page, base_url + "/api/status")
    body = res.get("body") if isinstance(res, dict) else None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return "", False
    cid = str(data.get(provider.status_client_id_field()) or "")
    raw_enabled = data.get(provider.status_oauth_field())
    if raw_enabled is None:
        enabled = bool(cid)
    elif isinstance(raw_enabled, bool):
        enabled = raw_enabled
    else:
        enabled = str(raw_enabled).strip().lower() not in {"", "0", "false", "no", "off"}
    return cid, enabled


async def _fetch_oauth_state(page, base_url: str, log: LogFn = _noop) -> tuple[str, str]:
    """从 {origin}/api/oauth/state 读取一次性 state；返回 (state, 诊断信息)。"""
    last_diag = "接口无响应"
    for attempt in range(3):
        res = await _api_get_json(page, base_url + "/api/oauth/state")
        if not isinstance(res, dict):
            last_diag = "接口无响应"
        else:
            status = res.get("status")
            body = res.get("body")
            oauth_state = _extract_oauth_state(body)
            if oauth_state:
                return oauth_state, f"status={status}"
            last_diag = f"status={status} body={_short_body(body)}"
            if status not in (408, 425, 429, 500, 502, 503, 504):
                break
        if attempt < 2:
            delay = 5 * (attempt + 1)
            log(f"/api/oauth/state 暂不可用（{last_diag}），等待 {delay}s 后重试...")
            await asyncio.sleep(delay)
    return "", last_diag


def _site_oauth_selectors(provider) -> list[str]:
    if provider.key == "linuxdo":
        return [
            "main button:has-text('使用 LinuxDO 继续')",
            "button:has-text('使用 LinuxDO 继续')",
            "button:has-text('Continue with LinuxDO')",
            "button:has-text('LinuxDO')",
            "button:has-text('Linux.do')",
            "button:has-text('Linux')",
            "button:has(#linuxdo_icon)",
            "text=/使用\\s*LinuxDO\\s*继续/i",
            "text=/Continue\\s+with\\s+LinuxDO/i",
            "text=/LinuxDO|Linux\\.do/i",
            "#linuxdo_icon",
        ]
    if provider.key == "github":
        return [
            "main button:has-text('使用 GitHub 继续')",
            "button:has-text('使用 GitHub 继续')",
            "button:has-text('GitHub')",
            "button:has([aria-label='github_logo'])",
            "text=/使用\\s*GitHub\\s*继续/i",
            "text=/GitHub/i",
        ]
    return DEFAULT_LOGIN_SELECTORS


SITE_OAUTH_TOGGLE_SELECTORS = [
    # AgentRouter / 部分 New API fork 首次进 /login 只显示账号密码表单，点“注册”后才显示 OAuth 按钮。
    "main a[href='/register']",
    "main a[href$='/register']",
    "main a:has-text('注册')",
    "main button:has-text('注册')",
    "main >> text=/没有账户|No account|Create account|Sign up|Register/i",
    # 也兼容反向情况：注册页无 OAuth 时再点回登录页。
    "main a[href='/login']",
    "main a[href$='/login']",
    "main a:has-text('登录')",
    "main button:has-text('登录')",
    "main >> text=/已有账户|Already have|Sign in|Log in/i",
]


SITE_LOGIN_SELECTORS = [
    "a[href='/login']",
    "a[href$='/login']",
    "a:has-text('登录')",
    "a:has-text('登 录')",
    "a:has-text('Log In')",
    "a:has-text('Sign in')",
    "button:has-text('登录')",
    "button:has-text('登 录')",
    "button:has-text('Log In')",
    "button:has-text('Sign in')",
    "text=/^\\s*登\\s*录\\s*$/",
    "text=/^\\s*登录\\s*$/",
    "text=/^\\s*Log\\s*In\\s*$/i",
    "text=/^\\s*Sign\\s*in\\s*$/i",
]


async def _maybe_click_with_popup(page, locator, log: LogFn, error_collector: dict[str, Any] | None = None, base_url: str = ""):
    popup_task = asyncio.create_task(page.wait_for_event("popup", timeout=10000))
    before_url = page.url

    async def _drain_popup_task() -> None:
        popup_task.cancel()
        try:
            await popup_task
        except BaseException:
            pass

    # 普通点击可能因不可见遮罩拦截指针事件而超时（Playwright 认为元素“可见/可用/稳定”
    # 却卡在派发点击），此时不应让整轮 relogin 失败：依次尝试强制点击、DOM dispatch，
    # 全部失败再返回 None，交由 _trigger_oauth 回退到直连授权 URL。仅真实驱动崩溃才上抛。
    clicked = False
    click_attempts = (
        ("普通点击", lambda: locator.click(timeout=7000)),
        ("强制点击", lambda: locator.click(timeout=3000, force=True)),
        ("DOM dispatch", lambda: locator.dispatch_event("click")),
    )
    for label, do_click in click_attempts:
        try:
            await do_click()
            clicked = True
            break
        except Exception as exc:
            if _is_driver_closed_error(exc):
                await _drain_popup_task()
                raise
            log(f"OAuth 入口{label}失败（{type(exc).__name__}）")

    if not clicked:
        log("OAuth 入口所有点击方式均失败，回退到直连授权 URL")
        await _drain_popup_task()
        return None

    popup = None
    try:
        popup = await popup_task
    except Exception:
        popup = None
    if popup:
        try:
            if error_collector is not None:
                _install_site_error_collector(popup, base_url, error_collector)
            await popup.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        log(f"站点前端已打开 OAuth 弹窗：{popup.url}")
        return popup

    await asyncio.sleep(2.5)
    if page.url != before_url:
        log(f"站点前端已跳转：{page.url}")
        return page
    log("点击后未检测到 OAuth 弹窗或跳转，可能 /api/oauth/state 被限流或按钮请求失败")
    return None


async def _click_site_oauth_entry(
    page,
    base_url: str,
    provider,
    log: LogFn = _noop,
    error_collector: dict[str, Any] | None = None,
):
    """关闭公告并点击站点前端的 OAuth 登录按钮，作为 /api/oauth/state 直取失败的兜底。

    AgentRouter 等 New API fork 可能首次进入 /login 只显示账号密码表单，
    需要点一次“注册/登录”切换或直接进入 /register 后才渲染 LinuxDO/GitHub OAuth 按钮。
    """
    selectors = _site_oauth_selectors(provider)

    async def _first_visible(selectors_to_try: list[str]):
        for sel in selectors_to_try:
            try:
                loc = page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                try:
                    visible = await loc.is_visible()
                except Exception as vis_exc:
                    if _is_driver_closed_error(vis_exc):
                        raise
                    visible = True
                if not visible:
                    continue
                return sel, loc
            except Exception as exc:
                if _is_driver_closed_error(exc):
                    raise
                continue
        return "", None

    async def _dismiss_current_popups() -> None:
        closed = await popups.dismiss_popups(page)
        if closed:
            log(f"已关闭 {closed} 个公告/弹窗")
            await asyncio.sleep(0.5)

    async def _click_oauth_if_visible():
        sel, loc = await _first_visible(selectors)
        if loc is None:
            return None
        log(f"点击站点前端 OAuth 登录入口：{sel}")
        return await _maybe_click_with_popup(page, loc, log, error_collector, base_url)

    async def _try_switch_auth_panel() -> bool:
        """尝试切到另一个登录/注册面板；返回是否执行了点击。"""
        for sel in SITE_OAUTH_TOGGLE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() <= 0:
                    continue
                try:
                    visible = await loc.is_visible()
                except Exception as vis_exc:
                    if _is_driver_closed_error(vis_exc):
                        raise
                    visible = True
                if not visible:
                    continue
                before_url = page.url
                log(f"切换站点登录/注册面板以显示 OAuth 入口：{sel}")
                await loc.click(timeout=7000)
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                await asyncio.sleep(1.2)
                if page.url != before_url:
                    log(f"站点登录/注册页已切换：{page.url}")
                await _wait_for_ready(page, timeout_ms=8000, log=log)
                await _dismiss_current_popups()
                return True
            except Exception as exc:
                if _is_driver_closed_error(exc):
                    raise
                continue
        return False

    root = base_url.rstrip("/")
    targets = [root + "/login", root + "/register", root]
    seen: set[str] = set()
    for target in targets:
        if target in seen:
            continue
        # WAF 熔断（IP 被持续风控）：每个兜底页都是挑战页，逐个打开纯属空耗
        if _waf_is_blocked(page):
            log("WAF 熔断，停止逐个打开站点登录页兜底")
            break
        seen.add(target)
        try:
            current_url = page.url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
            target_url = target.rstrip("/")
            if current_url != target_url:
                log(f"打开站点登录页兜底：{target}")
                await _safe_goto(page, target, wait_until="domcontentloaded", timeout=30000, log=log)
            await _wait_for_ready(page, timeout_ms=15000, log=log)
        except Exception as exc:
            # Driver/browser crash must stop the whole flow, not spin against a
            # dead process. Let it bubble up so run_oauth_checkin flags
            # driver_crashed and returns immediately.
            if _is_driver_closed_error(exc):
                raise
            log(f"打开登录页失败（继续尝试当前页）：{type(exc).__name__}")
        await _dismiss_current_popups()

        entry_page = await _click_oauth_if_visible()
        if entry_page is not None:
            return entry_page

        # 部分站点要先从 /login 点“注册”，或从 /register 点“登录”，OAuth 按钮才渲染。
        for _ in range(2):
            if not await _try_switch_auth_panel():
                break
            entry_page = await _click_oauth_if_visible()
            if entry_page is not None:
                return entry_page

    log("未找到可点击的站点前端 OAuth 登录入口")
    return None


async def _finish_oauth_authorization(
    page,
    base_url: str,
    provider,
    result: dict[str, Any],
    log: LogFn = _noop,
    error_collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """完成 provider 授权页：解验证、点击授权、等待回跳。"""
    # 解 Cloudflare（linux.do 常套 CF）
    if not await bypass.solve_cloudflare(page, log=log):
        result["cloudflare"] = True

    # 检测是否停在第三方登录页（共享登录态失效）
    for marker in provider.login_markers:
        try:
            if await page.query_selector(marker):
                result["need_human"] = True
                log(f"停在 {provider.key} 登录页：共享登录态失效，请在 GUI 重新捕获 {provider.key} 登录态")
                _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
                return result
        except Exception as exc:
            # A dead driver must stop the flow, not be swallowed as "marker absent".
            if _is_driver_closed_error(exc):
                raise
            pass

    # 点「同意授权」按钮（已授权过的账号可能自动回跳，无按钮）
    for sel in provider.approve_selectors:
        try:
            await page.wait_for_selector(sel, timeout=8000)
            btn = await page.query_selector(sel)
            if btn:
                log(f"点击授权按钮：{sel}")
                await btn.click()
                result["clicked"] = True
                await asyncio.sleep(2)
                await bypass.solve_cloudflare(page, log=log)
                break
        except Exception as exc:
            # Let a dead driver bubble up instead of trying the next selector.
            if _is_driver_closed_error(exc):
                raise
            continue
    if not result["clicked"]:
        log("未见授权按钮（可能已自动授权），继续等待回跳...")

    # 等待带 code 回跳站点（{origin}/api/oauth/... 或 /console，或 URL 含 code=）
    def _landed(u: str) -> bool:
        return base_url in u and ("/console" in u or "code=" in u or "/oauth" in u)

    try:
        await page.wait_for_url(_landed, timeout=OAUTH_WAIT_SECONDS * 1000)
        result["landed_back"] = True
        log(f"OAuth 已跳回站点：{page.url}")
        if await _is_waf_html(page):
            await _solve_waf(page, base_url, log, rounds=2)
    except Exception:
        try:
            cur = page.url
        except Exception:
            cur = ""
        if base_url in cur or "code=" in cur:
            result["landed_back"] = True
            log(f"OAuth 回跳（超时但已在站点）：{cur}")
        else:
            content_low = ""
            try:
                content_low = (await page.content()).lower()
            except Exception:
                pass
            if "just a moment" in content_low or "cloudflare" in content_low:
                result["cloudflare"] = True
                log("OAuth 被 Cloudflare 拦截")
            else:
                log(f"OAuth 未跳回站点，停在：{cur}")

    _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
    return result


async def _trigger_oauth(
    page,
    base_url: str,
    oauth_provider: str,
    log: LogFn = _noop,
    error_collector: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OAuth 授权流程：站点前端登录入口优先，直连授权 URL 兜底。

    优先路径（AgentRouter 等 fork）：
      进入 /login，必要时切到 /register，再点击「使用 LinuxDO/GitHub 继续」，
      让站点前端自己请求 /api/oauth/state 并打开第三方授权页。
    兜底路径：
      client_id ← GET /api/status（provider_client_id）
      state     ← GET /api/oauth/state（一次性，每次重取）
      授权 URL   ← provider.build_authorize_url(client_id, state)
    浏览器已持有第三方登录态时，授权页会出现「同意授权」按钮或自动回跳；
    回跳站点 {origin}/api/oauth/{provider} 后触发发额度。

    Returns:
        {clicked, landed_back, need_human, cloudflare, provider, driver_crashed?}
    """
    provider = oauth_providers.get_oauth_provider(oauth_provider)
    result: dict[str, Any] = {
        "clicked": False, "landed_back": False, "need_human": False,
        "cloudflare": False, "provider": provider.key,
    }

    # 站点当前页须先脱离 WAF，否则 /api/status 也会被拦
    if await _is_waf_html(page):
        if not _waf_is_blocked(page):
            await _solve_waf(page, base_url, log, rounds=2)
        # WAF 熔断（IP 被持续风控）：前端入口和直连授权都会被同样拦截，直接早退
        if _waf_is_blocked(page):
            result["waf_blocked"] = True
            log("WAF 熔断，跳过 OAuth 触发（出口 IP 被持续风控）")
            _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
            return result

    # 1) 优先走站点前端：AgentRouter 等站点访问 /login 后需切到注册/登录面板，点击 LinuxDO/GitHub 按钮。
    log(f"尝试通过站点前端登录页触发 {provider.key} OAuth...")
    entry_page = await _click_site_oauth_entry(page, base_url, provider, log, error_collector)
    if entry_page is not None:
        result["frontend_entry"] = True
        return await _finish_oauth_authorization(entry_page, base_url, provider, result, log, error_collector)
    log("站点前端 OAuth 入口未触发，回退到直连授权 URL")

    # 2) client_id + 开关
    client_id, enabled = await _fetch_oauth_client_id(page, base_url, provider)
    if not client_id:
        log(f"未能从 /api/status 获取 {provider.key}_client_id（站点未开启该 OAuth 或被 WAF 拦截）")
        _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
        return result
    if not enabled:
        log(f"站点未开启 {provider.key} OAuth 登录")
        _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
        return result
    log(f"已获取 {provider.key} client_id={client_id}")

    # 3) state（一次性）。前端路径已尝试过，直取失败则返回失败诊断。
    oauth_state, state_diag = await _fetch_oauth_state(page, base_url, log)
    if not oauth_state:
        result["state_error"] = state_diag
        log(f"未能获取 /api/oauth/state（{state_diag}）")
        _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
        return result

    # 4) 导航到第三方授权页
    authorize_url = provider.build_authorize_url(client_id, oauth_state)
    log(f"导航到 {provider.key} 授权页：{provider.authorize_endpoint}")
    try:
        await _safe_goto(page, authorize_url, wait_until="domcontentloaded", timeout=30000, log=log)
    except Exception as exc:
        if _is_driver_closed_error(exc):
            result["driver_crashed"] = True
            log(f"浏览器驱动崩溃：{exc}")
        else:
            log(f"导航授权页失败：{exc}")
        _add_site_error(error_collector, "exception", exc)
        _attach_site_errors(result, await _site_error_messages(page, error_collector), log)
        return result

    return await _finish_oauth_authorization(page, base_url, provider, result, log, error_collector)


# ═══════════════════════════ 公开 API（async）═══════════════════════════

async def capture_login(
    base_url: str,
    fallback_uid: str = "",
    proxy: str = "",
    log: LogFn = _noop,
    wait_for_close: Any = None,
) -> dict[str, Any]:
    """有头浏览器人工登录捕获登录态（async 版本）。

    Args:
        base_url: 站点地址。
        fallback_uid: 兜底用户 ID（用于 New-Api-User 头）。
        proxy: 代理 URL（如 "http://user:pass@host:port"，可选）。
        log: 日志回调。
        wait_for_close: 等待用户关闭浏览器的回调（async 函数）。

    Returns:
        {"ok": bool, "message": str, "state": str, "username": str}
    """
    log("启动 Camoufox 浏览器（有头模式），请在浏览器中完成登录...")

    try:
        browser, context = await bypass.launch_camoufox(
            headless=False,  # 有头，用户可见
            humanize=True,
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败（请先运行 `camoufox fetch` 安装浏览器）：{exc}") from exc

    page = None
    try:
        # 打开登录页
        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)

        # 等待用户完成登录（阻塞式回调，支持 async / sync）
        if wait_for_close:
            import inspect
            ret = wait_for_close()
            if inspect.isawaitable(ret):
                await ret  # 阻塞直到用户按「完成登录」
        else:
            log("等待 60 秒后自动关闭浏览器...")
            await asyncio.sleep(60)

        # 验证登录态
        user_data = await read_user(page, base_url, fallback_uid, log)
        if not user_data:
            return {
                "ok": False,
                "message": "登录态验证失败（未读到用户信息），请确认已登录并刷新页面。",
                "state": "",
                "username": "",
            }

        # 导出 storage_state（含所有域名：站点 + linux.do/github 第三方登录态）
        storage_state_dict = await _safe_storage_state(context, log)
        encoded_state = state.encode_state(storage_state_dict)

        username = user_data.get("username") or user_data.get("display_name") or "未知用户"

        # 检测是否包含第三方 OAuth 登录态（linux.do / github），OAuth 重放需要
        cookies = storage_state_dict.get("cookies", [])
        domains = {c.get("domain", "").lstrip(".") for c in cookies}
        has_oauth = any("linux.do" in d or "github.com" in d for d in domains)
        oauth_hint = ""
        if not has_oauth:
            oauth_hint = "（⚠️ 未检测到 linux.do/github 登录态，OAuth 重放可能失败，请确保登录时完成了第三方登录）"
        log(f"登录态捕获成功：{username}，域名：{','.join(sorted(d for d in domains if d))}{oauth_hint}")

        return {
            "ok": True,
            "message": f"登录态捕获成功（{username}）{oauth_hint}",
            "state": encoded_state,
            "username": username,
        }

    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise BrowserSessionError("浏览器驱动已关闭，登录态捕获中断；请重试，若反复出现请更新 camoufox/playwright。") from exc
        raise

    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


async def capture_oauth_state(
    oauth_provider: str = "linuxdo",
    proxy: str = "",
    log: LogFn = _noop,
    wait_for_close: Any = None,
) -> dict[str, Any]:
    """有头浏览器人工捕获第三方 OAuth provider 的共享登录态。

    该登录态写入 ACCOUNTS.json 顶层 oauth_states[provider]，供多个 relogin
    站点复用；不绑定任何站点，也不读取 /api/user/self。
    """
    provider = oauth_providers.get_oauth_provider(oauth_provider)
    log(f"启动 Camoufox 浏览器（有头模式），请登录 {provider.key}...")

    try:
        browser, context = await bypass.launch_camoufox(
            headless=False,
            humanize=True,
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败（请先运行 `camoufox fetch` 安装浏览器）：{exc}") from exc

    page = None
    try:
        page = await context.new_page()
        # provider 页面不安装通用站点公告守卫，避免误作用到 OAuth 授权/提示弹窗。
        await _safe_goto(page, provider.capture_url, wait_until="domcontentloaded", timeout=30000, log=log)

        # 自动轮询真正的认证 Cookie。访问 provider 登录页本身也会产生匿名/CSRF Cookie，
        # 因此不能再以“存在 provider 域 Cookie”作为登录成功依据。
        if wait_for_close:
            import inspect
            ret = wait_for_close()
            close_task = asyncio.create_task(ret) if inspect.isawaitable(ret) else None
        else:
            log("等待登录成功，最长 60 秒后自动结束...")
            close_task = asyncio.create_task(asyncio.sleep(60))

        authenticated = False
        try:
            while True:
                cookies = await context.cookies()
                if provider.has_authenticated_state(cookies):
                    authenticated = True
                    log(f"已自动检测到 {provider.key} 有效登录态，正在关闭浏览器并保存...")
                    break
                if close_task is None or close_task.done():
                    if close_task is not None:
                        close_task.result()
                    break
                await asyncio.sleep(0.4)
        finally:
            if close_task is not None and not close_task.done():
                close_task.cancel()
                try:
                    await close_task
                except asyncio.CancelledError:
                    pass

        if not authenticated:
            msg = f"未检测到 {provider.key} 有效认证 Cookie，请确认登录成功后重试。"
            log(msg)
            return {"ok": False, "message": msg, "state": "", "username": "", "provider": provider.key}

        storage_state_dict = await _safe_storage_state(context, log)
        encoded_state = state.encode_state(storage_state_dict)
        cookies = storage_state_dict.get("cookies", [])
        domains = {str(c.get("domain", "")).lstrip(".") for c in cookies if c.get("domain")}
        username = ""
        try:
            username = (await page.title()) or ""
        except Exception:
            pass

        log(f"{provider.key} 登录态捕获成功，域名：{','.join(sorted(domains))}")
        return {
            "ok": True,
            "message": f"{provider.key} 登录态捕获成功",
            "state": encoded_state,
            "username": username,
            "provider": provider.key,
        }
    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise BrowserSessionError(f"浏览器驱动已关闭，{provider.key} 登录态捕获中断；请重试，若反复出现请更新 camoufox/playwright。") from exc
        raise
    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


async def capture_sub2api_login(
    base_url: str,
    proxy: str = "",
    log: LogFn = _noop,
    wait_for_close: Any = None,
) -> dict[str, Any]:
    """有头浏览器人工捕获 Sub2API 站点登录态。

    Sub2API 不是 New API，不能用 /api/user/self 验证。捕获时只要求：
    - 用户已在站点完成登录；
    - localStorage/sessionStorage 中存在 auth_token/access_token/token/jwt；
    - 尽量用 /api/v1/user/profile、/api/v1/auth/me 验证该前端登录 token。
    """
    log("启动 Camoufox 浏览器（有头模式），请在 Sub2API 站点中完成登录...")

    try:
        browser, context = await bypass.launch_camoufox(
            headless=False,
            humanize=True,
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败（请先运行 `camoufox fetch` 安装浏览器）：{exc}") from exc

    page = None
    try:
        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)

        if wait_for_close:
            import inspect
            ret = wait_for_close()
            if inspect.isawaitable(ret):
                await ret
        else:
            log("等待 60 秒后自动关闭浏览器...")
            await asyncio.sleep(60)

        token = await page.evaluate(
            """() => {
                for (const key of ['auth_token', 'access_token', 'token', 'jwt']) {
                    const value = localStorage.getItem(key) || sessionStorage.getItem(key) || '';
                    if (value && value.length > 20) return value;
                }
                return '';
            }"""
        )
        if not token:
            return {
                "ok": False,
                "message": "未在 localStorage/sessionStorage 中读取到 Sub2API auth_token，请确认已完成登录后再点击完成。",
                "state": "",
                "username": "",
                "access_token": "",
            }

        verify = await page.evaluate(
            """async ([baseUrl, token, timeoutMs]) => {
                let last = null;
                for (const path of ['/api/v1/user/profile', '/api/v1/auth/me', '/api/v1/usage?page=1&page_size=1&sort_by=created_at&sort_order=desc']) {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), timeoutMs);
                    try {
                        const r = await fetch(baseUrl + path, {
                            credentials: 'include',
                            headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                            signal: controller.signal,
                        });
                        const t = await r.text();
                        let body;
                        try { body = JSON.parse(t); } catch { body = t.slice(0, 200); }
                        const result = { ok: false, status: r.status, path, body };
                        if (r.ok) return { ok: true, status: r.status, path, body };
                        if (r.status === 401 || r.status === 403) return result;
                        last = result;
                    } catch (e) {
                        last = { ok: false, status: 0, path, body: String(e && e.name === 'AbortError' ? 'fetch timeout' : e) };
                    } finally {
                        clearTimeout(timer);
                    }
                }
                return last || { ok: false, status: 404, path: '', body: 'profile endpoints not found' };
            }""",
            [base_url.rstrip("/"), token, 15000],
        )
        ok = bool(isinstance(verify, dict) and verify.get("ok"))
        body = verify.get("body") if isinstance(verify, dict) else None
        data = body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else (body if isinstance(body, dict) else {})
        username = ""
        if isinstance(data, dict):
            user_data = data
            items = data.get("items")
            if isinstance(items, list) and items and isinstance(items[0], dict) and isinstance(items[0].get("user"), dict):
                user_data = items[0]["user"]
            username = str(user_data.get("username") or user_data.get("name") or user_data.get("email") or user_data.get("id") or "")
        if ok:
            log(f"Sub2API 登录态验证成功：{username or '已登录'}")
        else:
            status = verify.get("status") if isinstance(verify, dict) else "?"
            path = verify.get("path") if isinstance(verify, dict) else ""
            log(f"已读取 auth_token，但 {path or '/api/v1/user/profile'} 验证未成功（HTTP {status}）；仍保存登录态供后续刷新使用")

        storage_state_dict = await _safe_storage_state(context, log)
        encoded_state = state.encode_state(storage_state_dict)
        return {
            "ok": True,
            "message": f"Sub2API 登录态捕获成功，已读取 auth_token（{len(token)} 字符）" + ("" if ok else "；但标准用户接口未验证通过"),
            "state": encoded_state,
            "username": username,
            "access_token": token,
            "auth_verified": ok,
        }
    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise BrowserSessionError("浏览器驱动已关闭，Sub2API 登录态捕获中断；请重试，若反复出现请更新 camoufox/playwright。") from exc
        raise
    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


async def capture_sub2api_token(
    base_url: str,
    browser_state_text: str = "",
    proxy: str = "",
    log: LogFn = _noop,
    return_state: bool = False,
) -> str | dict[str, Any] | None:
    """用浏览器登录态打开 sub2api 站点，从 localStorage 提取最新 auth_token。

    sub2api 的 JWT（auth_token）会过期，但只要浏览器持有有效的 linux.do
    登录态，打开站点后前端会自动用 refresh 流程刷新出新的 auth_token。
    本函数加载登录态、打开站点、等待并读取 localStorage 的 auth_token。

    Args:
        base_url: sub2api 站点地址。
        browser_state_text: 登录态 base64 文本。
        proxy: 代理 URL（可选）。
        log: 日志回调。

    Returns:
        默认返回最新的 auth_token 字符串，失败返回 None；return_state=True 时返回包含 access_token/state 的 dict。
    """
    if not browser_state_text:
        log("未提供 browser_state，无法自动刷新 token")
        return None

    try:
        storage_state_dict = state.decode_state(browser_state_text)
        log(f"已解码登录态：{state.state_summary(storage_state_dict)}")
    except state.BrowserStateError as exc:
        raise BrowserSessionError(f"登录态解码失败：{exc}") from exc

    headless = _env_headless()
    log(f"Camoufox 运行模式：{_browser_mode_label(headless)}" + (" / proxy" if proxy else ""))
    try:
        browser, context = await bypass.launch_camoufox(
            headless=headless, humanize=False, geoip=True, proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败：{exc}") from exc

    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)
        await _wait_for_ready(page, timeout_ms=30000, log=log)

        async def _read_token() -> str:
            try:
                return await page.evaluate(
                    """() => {
                        for (const key of ['auth_token', 'access_token', 'token', 'jwt']) {
                            const value = localStorage.getItem(key) || sessionStorage.getItem(key) || '';
                            if (value && value.length > 20) return value;
                        }
                        return '';
                    }"""
                )
            except Exception:
                return ""

        async def _success(token_value: str) -> str | dict[str, Any]:
            if not return_state:
                return token_value
            storage_state = await _safe_storage_state(context, log)
            return {"access_token": token_value, "state": state.encode_state(storage_state)}

        async def _clear_cached_token() -> None:
            try:
                await page.evaluate(
                    """() => {
                        for (const key of ['auth_token', 'access_token', 'token', 'jwt']) {
                            try { localStorage.removeItem(key); } catch (_) {}
                            try { sessionStorage.removeItem(key); } catch (_) {}
                        }
                    }"""
                )
            except Exception:
                pass

        async def _verify_token(token_value: str) -> dict[str, Any]:
            if not token_value:
                return {"ok": False, "status": 0, "path": "", "body": "empty token"}
            try:
                verify = await page.evaluate(
                    """async ([baseUrl, token, timeoutMs]) => {
                        const paths = [
                            '/api/v1/user/profile',
                            '/api/v1/auth/me',
                            '/api/v1/usage?page=1&page_size=1&sort_by=created_at&sort_order=desc'
                        ];
                        let last = null;
                        for (const path of paths) {
                            const controller = new AbortController();
                            const timer = setTimeout(() => controller.abort(), timeoutMs);
                            try {
                                const r = await fetch(baseUrl + path, {
                                    credentials: 'include',
                                    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                                    signal: controller.signal,
                                });
                                const t = await r.text();
                                let body;
                                try { body = JSON.parse(t); } catch { body = t.slice(0, 200); }
                                const result = { ok: false, status: r.status, path, body };
                                if (r.ok) return { ok: true, status: r.status, path, body };
                                if (r.status === 401 || r.status === 403) return result;
                                last = result;
                            } catch (e) {
                                last = { ok: false, status: 0, path, body: String(e && e.name === 'AbortError' ? 'fetch timeout' : e) };
                            } finally {
                                clearTimeout(timer);
                            }
                        }
                        return last || { ok: false, status: 404, path: '', body: 'standard profile endpoints not found' };
                    }""",
                    [base_url.rstrip("/"), token_value, 15000],
                )
                return verify if isinstance(verify, dict) else {"ok": False, "status": 0, "path": "", "body": verify}
            except Exception as exc:
                return {"ok": False, "status": 0, "path": "", "body": str(exc)}

        async def _refresh_via_refresh_token() -> str:
            try:
                result = await page.evaluate(
                    """async ([baseUrl, timeoutMs]) => {
                        const refreshToken = localStorage.getItem('refresh_token') || sessionStorage.getItem('refresh_token') || '';
                        if (!refreshToken || refreshToken.length <= 20) return { ok: false, status: 0, message: 'refresh_token not found' };
                        const controller = new AbortController();
                        const timer = setTimeout(() => controller.abort(), timeoutMs);
                        try {
                            const r = await fetch(baseUrl + '/api/v1/auth/refresh', {
                                method: 'POST',
                                credentials: 'include',
                                headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                                body: JSON.stringify({ refresh_token: refreshToken }),
                                signal: controller.signal,
                            });
                            const t = await r.text();
                            let body;
                            try { body = JSON.parse(t); } catch { body = { message: t.slice(0, 200) }; }
                            const data = body && (body.data || body);
                            const access = data && data.access_token;
                            if (r.ok && access) {
                                localStorage.setItem('auth_token', access);
                                if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token);
                                if (data.expires_in) localStorage.setItem('token_expires_at', String(Date.now() + Number(data.expires_in) * 1000));
                                return { ok: true, status: r.status, access_token: access };
                            }
                            return { ok: false, status: r.status, message: body && (body.message || body.code || body.error || JSON.stringify(body).slice(0, 200)) };
                        } catch (e) {
                            return { ok: false, status: 0, message: String(e && e.name === 'AbortError' ? 'fetch timeout' : e) };
                        } finally {
                            clearTimeout(timer);
                        }
                    }""",
                    [base_url.rstrip("/"), 15000],
                )
                if isinstance(result, dict) and result.get("ok") and result.get("access_token"):
                    token_value = str(result.get("access_token") or "")
                    log(f"已通过 refresh_token 刷新 auth_token（{len(token_value)} 字符）")
                    return token_value
                if isinstance(result, dict) and result.get("message"):
                    log(f"refresh_token 刷新未成功：{result.get('message')}（HTTP {result.get('status')}）")
            except Exception as exc:
                log(f"refresh_token 刷新异常：{exc}")
            return ""

        async def _validated_token(label: str) -> str:
            token_value = await _read_token()
            if not token_value:
                token_value = await _refresh_via_refresh_token()
                if not token_value:
                    return ""
            verify = await _verify_token(token_value)
            if verify.get("ok"):
                log(f"已读取并验证 auth_token（{len(token_value)} 字符，{verify.get('path') or '/api/v1/user/profile'}）")
                return token_value
            status = verify.get("status")
            path = verify.get("path") or "/api/v1/user/profile"
            if status in (401, 403):
                log(f"{label} auth_token 已失效（{path} HTTP {status}），尝试用 refresh_token 刷新...")
                refreshed = await _refresh_via_refresh_token()
                if refreshed:
                    refreshed_verify = await _verify_token(refreshed)
                    if refreshed_verify.get("ok"):
                        log(f"refresh_token 刷新后的 auth_token 验证成功（{refreshed_verify.get('path') or '/api/v1/user/profile'}）")
                        return refreshed
                await _clear_cached_token()
                return ""
            if status == 404:
                log(f"未找到 Sub2API 标准验证接口，保留已读取 token 供兼容旧 fork 使用（{len(token_value)} 字符）")
                return token_value
            log(f"{label} auth_token 验证未成功（{path} HTTP {status}），准备触发前端登录刷新...")
            return ""

        # 给前端 token 刷新流程一点时间；旧 localStorage token 必须先经 /api/v1/* 验证，避免返回过期 JWT。
        await asyncio.sleep(3)
        token = await _validated_token("当前")
        if token:
            return await _success(token)

        # 只有第三方登录态、没有站点态时，前端可能不会自动刷新；打开登录页并点击 OAuth 登录按钮。
        log("未在 localStorage 中找到 auth_token，尝试触发 Sub2API 登录流程...")
        try:
            await _safe_goto(page, base_url.rstrip("/") + "/login", wait_until="domcontentloaded", timeout=30000, log=log)
            await _wait_for_ready(page, timeout_ms=20000, log=log)
            await asyncio.sleep(1)
        except Exception:
            pass
        closed = await popups.dismiss_popups(page)
        if closed:
            log(f"已关闭 {closed} 个遮挡弹窗")
            await asyncio.sleep(0.5)
        login_selectors = [
            "button:has-text('使用 Linux.do 登录')",
            "button:has-text('Continue with Linux.do')",
            "button:has-text('Linux.do')",
            "button:has-text('LinuxDO')",
            "button:has-text('LinuxDo')",
            "button:has-text('Linux')",
            "a:has-text('Linux.do')",
            "a:has-text('LinuxDO')",
            "[href*='oauth/linuxdo']",
            "[href*='oauth']",
            "a[href*='linux.do']",
        ]
        clicked = False
        for sel in login_selectors:
            try:
                loc = page.locator(sel).first
                if await loc.count() > 0:
                    log(f"点击 Sub2API 登录入口：{sel}")
                    await loc.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue

        if clicked:
            await asyncio.sleep(3)
            await bypass.solve_cloudflare(page, log=log)
            # 如果进入第三方授权页，尝试点击授权按钮；若已授权可能会自动跳回。
            for provider_key in oauth_providers.KNOWN_OAUTH_PROVIDERS:
                provider = oauth_providers.get_oauth_provider(provider_key)
                if not provider.matches_url(page.url):
                    continue
                for marker in provider.login_markers:
                    try:
                        if await page.query_selector(marker):
                            log(f"停在 {provider.key} 登录页：共享登录态失效")
                            return None
                    except Exception:
                        pass
                for sel in provider.approve_selectors:
                    try:
                        await page.wait_for_selector(sel, timeout=8000)
                        btn = await page.query_selector(sel)
                        if btn:
                            log(f"点击 {provider.key} 授权按钮：{sel}")
                            await btn.click()
                            await asyncio.sleep(3)
                            break
                    except Exception:
                        continue
                break
            try:
                await page.wait_for_url(lambda u: base_url in u, timeout=20000)
            except Exception:
                pass
            for _ in range(20):
                await asyncio.sleep(1)
                token = await _validated_token("刷新后")
                if token:
                    log(f"已刷新 auth_token（{len(token)} 字符）")
                    return await _success(token)

        log("未在 localStorage 中找到可验证的 auth_token")
        return None
    except Exception as exc:
        if not _is_driver_closed_error(exc):
            raise
        log(f"浏览器驱动已关闭：{exc}")
        return None
    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


async def verify_state(
    base_url: str,
    browser_state_text: str = "",
    fallback_uid: str = "",
    proxy: str = "",
    log: LogFn = _noop,
) -> dict[str, Any]:
    """无头验证登录态是否有效（async 版本）。

    Args:
        base_url: 站点地址。
        browser_state_text: 登录态 base64 文本（ACCOUNTS.json 的 browser_state 字段）。
        fallback_uid: 兜底用户 ID。
        proxy: 代理 URL（可选）。
        log: 日志回调。

    Returns:
        {"ok": bool, "message": str, "username": str, "quota": int}
    """
    log("启动 Camoufox 浏览器，验证登录态...")

    # 解码 storage_state
    storage_state_dict = None
    if browser_state_text:
        try:
            storage_state_dict = state.decode_state(browser_state_text)
            log(f"已解码登录态：{state.state_summary(storage_state_dict)}")
        except state.BrowserStateError as exc:
            raise BrowserSessionError(f"登录态解码失败：{exc}") from exc

    headless = _env_headless()
    log(f"Camoufox 运行模式：{_browser_mode_label(headless)}" + (" / proxy" if proxy else ""))
    try:
        browser, context = await bypass.launch_camoufox(
            headless=headless,
            humanize=False,  # 验证不需要人类化
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败：{exc}") from exc

    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)
        await _wait_for_ready(page, timeout_ms=30000, log=log)

        # 读取用户信息
        user_data = await read_user(page, base_url, fallback_uid, log)
        if not user_data:
            if _waf_is_blocked(page):
                return {
                    "ok": False,
                    "message": "站点阿里云 WAF 持续拦截当前出口 IP（数据中心/CI IP 信誉过低），无法通过 JS 挑战；登录态可能仍有效，请为该账号配置住宅代理或改用住宅 IP 环境验证。",
                    "username": "",
                    "quota": 0,
                    "waf_blocked": True,
                }
            return {"ok": False, "message": "登录态已失效或无法验证", "username": "", "quota": 0}

        username = user_data.get("username") or user_data.get("display_name") or "未知用户"
        quota = user_data.get("quota") or 0

        log(f"登录态有效：{username}，额度 {quota_to_usd(quota)}")
        return {
            "ok": True,
            "message": f"登录态有效（{username}）",
            "username": username,
            "quota": quota,
        }

    except Exception as exc:
        if not _is_driver_closed_error(exc):
            raise
        log(f"浏览器驱动已关闭：{exc}")
        return {"ok": False, "message": "浏览器驱动已关闭或页面脚本触发 Playwright Firefox 兼容问题，请重试。", "username": "", "quota": 0, "driver_crashed": True}

    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


def _site_cookie_string(cookies: list[dict[str, Any]], base_url: str) -> str:
    """从 context.cookies() 里挑出属于站点域的 cookie，拼成 "k=v; k2=v2"。

    仿 millylee：把浏览器过 WAF 后拿到的 acw_tc 等 WAF cookie 与站点 session
    cookie 一起导出，交给 HTTP 层复用。只保留站点域（含父域）cookie，避免把
    第三方 OAuth（linux.do/github）cookie 混入站点请求。
    """
    host = urlparse(base_url if base_url.startswith(("http://", "https://")) else "https://" + base_url).hostname or ""
    host = host.lower()
    pairs: dict[str, str] = {}
    for c in cookies or []:
        name = str(c.get("name") or "")
        if not name:
            continue
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if not dom:
            continue
        # 站点域或其父域下的 cookie 才带上（host == dom 或 host 是 dom 的子域）
        if host == dom or host.endswith("." + dom):
            pairs[name] = str(c.get("value") or "")
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


async def refresh_site_cookies(
    base_url: str,
    browser_state_text: str = "",
    fallback_uid: str = "",
    proxy: str = "",
    log: LogFn = _noop,
) -> dict[str, Any]:
    """用浏览器过 WAF，导出当前站点的 cookies（供 HTTP 层复用）。

    仿 millylee 混合式签到：浏览器只负责“过 WAF + 拿 cookie”。加载已保存的
    站点 storage_state（含 session cookie）后访问站点，让浏览器执行阿里云 WAF
    的 JS 挑战拿到 acw_tc 等 WAF cookie，再把「WAF cookie + 站点 session cookie」
    一起导出。真正的签到由 HTTP 层用这些 cookie 发轻量请求完成。

    Returns:
        {
          "ok": bool,            # 是否成功导出可用 cookie
          "message": str,
          "cookie": str,         # "k=v; k2=v2" 站点域 cookie（WAF + session）
          "new_api_user": str,   # 站点用户 ID（New-Api-User 头用）
          "state": str,          # 刷新后的 storage_state base64（可回写复用）
          "username": str,
          "quota": Any,
          "waf_blocked": bool,   # True 表示 IP 被 WAF 持续风控
          "driver_crashed": bool,
        }
    """
    log("启动 Camoufox 浏览器，过 WAF 并导出站点 cookie...")

    storage_state_dict = None
    if browser_state_text:
        try:
            storage_state_dict = state.decode_state(browser_state_text)
            log(f"已解码登录态：{state.state_summary(storage_state_dict)}")
        except state.BrowserStateError as exc:
            raise BrowserSessionError(f"登录态解码失败：{exc}") from exc

    headless = _env_headless()
    log(f"Camoufox 运行模式：{_browser_mode_label(headless)}" + (" / proxy" if proxy else ""))
    try:
        browser, context = await bypass.launch_camoufox(
            headless=headless,
            humanize=False,  # 拿 cookie 不需要人类化
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败：{exc}") from exc

    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = await context.new_page()
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)
        await _wait_for_ready(page, timeout_ms=30000, log=log)

        # read_user 会在命中 WAF 时用页面导航求解挑战，从而让浏览器拿到 acw_tc 等 cookie。
        # 即便 read_user 失败（登录态已过期），只要 WAF 通过，cookie 仍值得导出兜底。
        user_data = await read_user(page, base_url, fallback_uid, log)

        if _waf_is_blocked(page):
            return {
                "ok": False,
                "message": "站点阿里云 WAF 持续拦截当前出口 IP（数据中心/CI IP 信誉过低），无法通过 JS 挑战；请为该账号配置住宅代理或改用住宅 IP 环境运行。",
                "cookie": "",
                "new_api_user": "",
                "state": "",
                "username": "",
                "quota": None,
                "waf_blocked": True,
            }

        # 导出站点域 cookie（WAF + session 合并）
        try:
            all_cookies = await context.cookies()
        except Exception as exc:
            if _is_driver_closed_error(exc):
                raise
            all_cookies = []
        cookie_str = _site_cookie_string(all_cookies, base_url)

        new_api_user = str(fallback_uid or "")
        username = ""
        quota = None
        if isinstance(user_data, dict):
            uid = user_data.get("id") or user_data.get("user_id")
            if uid not in (None, ""):
                new_api_user = str(uid)
            username = user_data.get("username") or user_data.get("display_name") or ""
            quota = user_data.get("quota")

        # 刷新后的 storage_state（cookie 已更新，回写供下次复用）
        refreshed_state = ""
        try:
            refreshed_state = state.encode_state(await _safe_storage_state(context, log))
        except BrowserSessionError:
            refreshed_state = ""

        if not cookie_str:
            return {
                "ok": False,
                "message": "未能导出站点 cookie（登录态可能已失效或站点未设置 cookie），请重新捕获登录态。",
                "cookie": "",
                "new_api_user": new_api_user,
                "state": refreshed_state,
                "username": username,
                "quota": quota,
                "waf_blocked": False,
            }

        log(f"已导出站点 cookie（{len(cookie_str)} 字符），用户：{username or '未知'}")
        return {
            "ok": True,
            "message": f"已导出站点 cookie（{username or '未知用户'}）",
            "cookie": cookie_str,
            "new_api_user": new_api_user,
            "state": refreshed_state,
            "username": username,
            "quota": quota,
            "waf_blocked": False,
        }

    except Exception as exc:
        if not _is_driver_closed_error(exc):
            raise
        log(f"浏览器驱动已关闭：{exc}")
        return {
            "ok": False,
            "message": "浏览器驱动已关闭或页面脚本触发 Playwright Firefox 兼容问题，请重试。",
            "cookie": "",
            "new_api_user": "",
            "state": "",
            "username": "",
            "quota": None,
            "driver_crashed": True,
        }
    finally:
        await _safe_close_page(page)
        await _safe_close_browser(browser)


async def run_oauth_checkin(
    base_url: str,
    account_name: str = "",
    browser_state_text: str = "",
    oauth_provider: str = "linuxdo",
    fallback_uid: str = "",
    proxy: str = "",
    log: LogFn = _noop,
) -> dict[str, Any]:
    """无头自动 OAuth 重登触发发额度（async 版本，真正的签到）。

    Args:
        base_url: 站点地址。
        account_name: 账号名称（用于日志）。
        browser_state_text: 共享第三方登录态（linux.do/github）base64 文本。
        oauth_provider: 第三方 OAuth 提供商（linuxdo / github）。
        fallback_uid: 兜底用户 ID。
        proxy: 代理 URL（可选）。
        log: 日志回调。

    Returns:
        {status, message, quota_before, quota_after, delta, link}
    """
    log("启动 Camoufox 浏览器，开始 OAuth 重登...")

    # 解码 storage_state
    storage_state_dict = None
    if browser_state_text:
        try:
            storage_state_dict = state.decode_state(browser_state_text)
            log(f"已解码登录态：{state.state_summary(storage_state_dict)}")
        except state.BrowserStateError as exc:
            raise BrowserSessionError(f"登录态解码失败：{exc}") from exc

    headless = _env_headless()
    log(f"Camoufox 运行模式：{_browser_mode_label(headless)}" + (" / proxy" if proxy else ""))
    try:
        browser, context = await bypass.launch_camoufox(
            headless=headless,
            humanize=True,  # 签到需要人类化行为
            geoip=True,
            proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败：{exc}") from exc

    page = None
    error_collector: dict[str, Any] | None = None
    quota_before = None
    quota_after = None
    link: dict[str, Any] = {}

    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = await context.new_page()
        error_collector = _install_site_error_collector(page, base_url)
        await popups.setup_popup_guard(page, allowed_origin=_origin_from_url(base_url))
        await _safe_goto(page, base_url, wait_until="domcontentloaded", timeout=30000, log=log)
        # localStorage 已通过 init_script 注入；等待页面就绪（含 WAF）
        await _wait_for_ready(page, timeout_ms=30000, log=log)

        # 读取 OAuth 前额度
        user_data_before = await read_user(page, base_url, fallback_uid, log)
        if user_data_before:
            quota_before = user_data_before.get("quota")
            log(f"OAuth 前额度：{quota_to_usd(quota_before)}")

        # 触发 OAuth 登录（拼授权 URL 法）
        link = await _trigger_oauth(page, base_url, oauth_provider, log, error_collector)
        
        # 检查驱动是否崩溃
        if link.get("driver_crashed"):
            log("检测到 Playwright 驱动崩溃，终止签到流程")
            _attach_site_errors(link, await _site_error_messages(page, error_collector), log)
            return {
                "status": "error",
                "message": _message_with_site_error("浏览器驱动崩溃（Playwright 内部错误），请重试或更新依赖", link),
                "quota_before": quota_before,
                "quota_after": None,
                "delta": None,
                "link": link,
            }

        # OAuth 回跳后优先捕获瞬时 Toast/弹窗。AgentRouter 的每日奖励提示可能早于额度接口更新，
        # 也可能在固定等待结束前消失，因此不能只依赖 OAuth 前后额度差。
        oauth_ok = link.get("landed_back") and not link.get("cloudflare") and not link.get("need_human")
        if oauth_ok:
            success_message = await _wait_for_site_success_message(page, error_collector, link, timeout_ms=3000)
            if success_message:
                log(f"已捕获 OAuth 签到成功弹窗：{success_message}")
                await asyncio.sleep(0.5)
        else:
            await asyncio.sleep(3)

        # 读取 OAuth 后额度
        user_data_after = await read_user(page, base_url, fallback_uid, log)
        if user_data_after:
            quota_after = user_data_after.get("quota")
            log(f"OAuth 后额度：{quota_to_usd(quota_after)}")

        # 额度到账延迟兜底：OAuth 已顺畅回跳、但既没抢到成功弹窗、额度也还没增长时，
        # 站点很可能只是 quota 接口尚未刷新（发放异步）。再轮询重读几次，避免把「刚发放
        # 但接口滞后」误判成「今日已领取，额度无变化」。一旦额度增长或捕获到弹窗即停止。
        if (
            oauth_ok
            and not str(link.get("site_success_message") or "").strip()
            and isinstance(quota_before, (int, float))
            and isinstance(quota_after, (int, float))
            and quota_after <= quota_before
        ):
            for attempt in range(3):
                await asyncio.sleep(2)
                late_message = await _wait_for_site_success_message(page, error_collector, link, timeout_ms=500)
                if late_message:
                    log(f"延迟捕获 OAuth 签到成功弹窗：{late_message}")
                    break
                user_data_late = await read_user(page, base_url, fallback_uid, log)
                if not user_data_late:
                    continue
                quota_late = user_data_late.get("quota")
                if isinstance(quota_late, (int, float)):
                    quota_after = quota_late
                    if quota_late > quota_before:
                        log(f"额度延迟到账，重读后额度：{quota_to_usd(quota_late)}（重试 {attempt + 1}/3）")
                        break

        # 兜底同步 WAF 熔断状态到 link（read_user 触发熔断但未经 _trigger_oauth 早退时）
        if _waf_is_blocked(page):
            link["waf_blocked"] = True

    except Exception as exc:
        if not _is_driver_closed_error(exc):
            raise
        link["driver_crashed"] = True
        _add_site_error(error_collector, "exception", exc)
        log(f"浏览器驱动已关闭：{exc}")
        return {
            "status": "error",
            "message": _message_with_site_error("浏览器驱动已关闭或页面脚本触发 Playwright Firefox 兼容问题，请重试。", link),
            "quota_before": quota_before,
            "quota_after": quota_after,
            "delta": None,
            "link": link,
        }

    finally:
        if page:
            try:
                _attach_site_errors(link, await _site_error_messages(page, error_collector), log)
            except Exception:
                pass
        await _safe_close_page(page)
        await _safe_close_browser(browser)

    return _oauth_checkin_result(quota_before, quota_after, link)
