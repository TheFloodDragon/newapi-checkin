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
from pathlib import Path
from typing import Any

from . import bypass, oauth_providers, popups, state
from . import storage_scope as _storage_scope
from .oauth_flow import (
    DEFAULT_LOGIN_SELECTORS as DEFAULT_LOGIN_SELECTORS,
    OAUTH_WAIT_SECONDS as OAUTH_WAIT_SECONDS,
    attach_oauth_completion_messages as _attach_oauth_completion_messages,
    oauth_checkin_result as _oauth_checkin_result,
    oauth_landed as _oauth_landed,
    trigger_oauth as _trigger_oauth,
)
from .runtime_loop import (
    BrowserResources,
    BrowserSessionError as BrowserSessionError,
    LogFn,
    browser_mode_label as _browser_mode_label,
    env_headless as _env_headless,
    is_driver_closed_error as _is_driver_closed_error,
    noop as _noop,
    run_sync as run_sync,
    safe_goto as _safe_goto,
    safe_storage_state as _safe_storage_state,
)
from .site_messages import (
    add_site_error as _add_site_error,
    attach_site_errors as _attach_site_errors,
    install_site_error_collector as _install_site_error_collector,
    message_with_site_error as _message_with_site_error,
    site_error_messages as _site_error_messages,
    site_success_message as _site_success_message_impl,
    wait_for_site_success_message as _wait_for_site_success_message,
)
from .storage_scope import (
    origin_of as _origin_from_url,
    site_cookie_string as _site_cookie_string,
    storage_access_token as storage_access_token,
    storage_item as storage_item,
    storage_refresh_token as storage_refresh_token,
)
from .waf import (
    WAF_BLOCK_THRESHOLD as WAF_BLOCK_THRESHOLD,
    WAF_RETRY as WAF_RETRY,
    read_user as read_user,
    waf_is_blocked as _waf_is_blocked,
    wait_for_ready as _wait_for_ready,
)

# 私有旧名也保留，避免已有调用方因模块拆分失效。
_same_origin = _storage_scope.same_origin
_site_success_message = _site_success_message_impl

SCRIPT_DIR = Path(__file__).resolve().parent.parent  # checkin/


def quota_to_usd(value: Any) -> str:
    """内部 quota → $ 展示（唯一实现在 providers.base；懒加载避免装载顺序耦合）。"""
    from providers.base import format_usd

    return format_usd(value, is_usd=False, fallback=str(value))


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

    resources = BrowserResources(browser=browser)
    page = None
    try:
        # 打开登录页
        page = resources.track_page(await context.new_page())
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
        await resources.close()


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

    resources = BrowserResources(browser=browser)
    page = None
    try:
        page = resources.track_page(await context.new_page())
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
        await resources.close()


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

    resources = BrowserResources(browser=browser)
    page = None
    try:
        page = resources.track_page(await context.new_page())
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
            # refresh_token 存进配置后，纯 HTTP 路径可自行续期短期 JWT，
            # 无需为「access_token 过期」这一常见情况再启动浏览器。
            "refresh_token": storage_refresh_token(storage_state_dict, base_url=base_url),
            "auth_verified": ok,
        }
    except Exception as exc:
        if _is_driver_closed_error(exc):
            raise BrowserSessionError("浏览器驱动已关闭，Sub2API 登录态捕获中断；请重试，若反复出现请更新 camoufox/playwright。") from exc
        raise
    finally:
        await resources.close()


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
        raise BrowserSessionError(f"登录态解码失败：{exc}", status="need_config") from exc

    headless = _env_headless()
    log(f"Camoufox 运行模式：{_browser_mode_label(headless)}" + (" / proxy" if proxy else ""))
    try:
        browser, context = await bypass.launch_camoufox(
            headless=headless, humanize=False, geoip=True, proxy=proxy or None,
        )
    except Exception as exc:
        raise BrowserSessionError(f"启动 Camoufox 失败：{exc}") from exc

    resources = BrowserResources(browser=browser)
    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = resources.track_page(await context.new_page())
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
            # 一并交出 refresh_token：它有效期远长于 access_token，存进配置后
            # 纯 HTTP 路径即可自行续期，无需为「JWT 过期」拉起浏览器。
            #
            # 不能只看导出的活 storage_state：access_token 过期时前端会在收到 401 后
            # 清空 localStorage 跳登录页（实测每 2 秒左右清一次，与 add_init_script
            # 的重注入来回竞争）。恰好在清空后导出就会丢掉一个仍然有效的
            # refresh_token。传入的登录态是解码后的静态快照，不受该竞争影响，
            # 因此活存储读不到时回落到它。
            refresh = storage_refresh_token(storage_state, base_url=base_url) or storage_refresh_token(
                storage_state_dict, base_url=base_url
            )
            return {
                "access_token": token_value,
                "refresh_token": refresh,
                "state": state.encode_state(storage_state),
            }

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
            # 从登录态快照兜底：token 过期时前端会清空 localStorage 再跳登录页，
            # 只读活存储会随机拿到空值并误报「refresh_token not found」。
            fallback_refresh = storage_refresh_token(storage_state_dict, base_url=base_url)
            try:
                result = await page.evaluate(
                    """async ([baseUrl, timeoutMs, fallbackRefresh]) => {
                        const refreshToken = localStorage.getItem('refresh_token')
                            || sessionStorage.getItem('refresh_token')
                            || fallbackRefresh
                            || '';
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
                    [base_url.rstrip("/"), 15000, fallback_refresh],
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
        await resources.close()


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
            raise BrowserSessionError(f"登录态解码失败：{exc}", status="need_config") from exc

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

    resources = BrowserResources(browser=browser)
    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = resources.track_page(await context.new_page())
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
        await resources.close()


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
            raise BrowserSessionError(f"登录态解码失败：{exc}", status="need_config") from exc

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

    resources = BrowserResources(browser=browser)
    page = None
    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = resources.track_page(await context.new_page())
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
        await resources.close()


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
            raise BrowserSessionError(f"登录态解码失败：{exc}", status="need_config") from exc

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

    resources = BrowserResources(browser=browser)
    page = None
    error_collector: dict[str, Any] | None = None
    quota_before = None
    quota_after = None
    link: dict[str, Any] = {}

    try:
        await state.restore_storage_state(context, storage_state_dict)

        page = resources.track_page(await context.new_page())
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
        oauth_ok = _oauth_landed(link)
        if oauth_ok:
            # 登录前读额度必然 401（当时确实未登录）。_trigger_oauth 已在成功回跳时
            # 清掉 link 上的历史错误，这里同步清空采集器，只留登录之后真正的错误。
            if error_collector is not None:
                error_collector["items"] = []
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
                _attach_oauth_completion_messages(
                    link, await _site_error_messages(page, error_collector), log
                )
            except Exception:
                pass
        await resources.close()

    return _oauth_checkin_result(quota_before, quota_after, link)
