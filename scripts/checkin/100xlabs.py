#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百倍/100xLabs（Sub2API）站点每日签到 browser_script。

登录态优先复用 browser_state，并在过期时用 localStorage 的 refresh_token 刷新。
当 refresh_token 也失效时，可从 script_args 的 email/password（或环境变量）读取
凭据，在真实登录页用 Cloudflare 正常签发的 Turnstile 令牌完成登录（不伪造、不绕过
验证）；凭据不会写入账号配置、脚本结果或日志。每次成功后把刷新出的最新登录态回写
ACCOUNTS.json，滚动续期以缓解登录态频繁失效。

配置示例：
{
  "checkin_action": "browser_script",
  "auth_method": "browser",
  "site_profile": "sub2api",
  "script": "scripts/checkin/100xlabs.py",
  "script_args": {"email": "you@example.com", "password": "******"}
}
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from browser import turnstile


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    args = dict(getattr(site, "script_args", {}) or {})

    def _list_arg(key: str, default: list[str]) -> list[str]:
        value = args.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        return default

    def _enabled(key: str, default: bool = True) -> bool:
        value = args.get(key, default)
        if isinstance(value, str):
            return value.strip().casefold() not in {"0", "false", "no", "off"}
        return bool(value)

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
    login_timeout_ms = max(1000, int(args.get("login_timeout_ms", 60000) or 60000))
    login_fallback_enabled = _enabled("login_fallback", True)
    # 凭据优先从 script_args 的 email/password 直接读取；未填时回退到环境变量。
    email_env = str(args.get("email_env") or "X100LABS_EMAIL").strip() or "X100LABS_EMAIL"
    password_env = str(args.get("password_env") or "X100LABS_PASSWORD").strip() or "X100LABS_PASSWORD"
    email_arg = str(args.get("email") or "").strip()
    password_arg = str(args.get("password") or "")
    start_url = str(args.get("start_url") or args.get("url") or "").strip()
    start_path = str(args.get("start_path") or args.get("path") or "/check-in").strip()
    target_url = start_url or start_path
    resolved_url = helpers.resolve_url(target_url)
    origin = helpers.resolve_url("/").rstrip("/")
    login_detail: dict[str, Any] = {}

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

    async def _click_checkin(
        initial: tuple[str, Any, str] | None = None,
    ) -> tuple[str, Any, str, str]:
        """多轮重新定位并点击，抵抗 SPA 重渲染、遮罩和元素抖动。"""
        for attempt in range(3):
            control = initial if attempt == 0 and initial is not None else await _find_checkin_control()
            if control is None:
                await page.wait_for_timeout(min(150, poll_interval_ms))
                continue
            text, locator, kind = control
            try:
                element = await locator.element_handle()
            except Exception:
                element = None

            try:
                await locator.scroll_into_view_if_needed(timeout=click_timeout)
            except Exception:
                pass

            click_attempts = (
                ("normal", lambda: locator.click(timeout=click_timeout)),
                ("force", lambda: locator.click(timeout=click_timeout, force=True)),
                ("dispatch", lambda: locator.dispatch_event("click")),
                ("dom", lambda: locator.evaluate("el => el.click()")),
            )
            for strategy, click in click_attempts:
                try:
                    await click()
                    return text, element or locator, kind, strategy
                except Exception:
                    continue

            # 元素可能在点击期间被 React 替换；下一轮重新创建 locator。
            await page.wait_for_timeout(min(200, max(50, poll_interval_ms)))
        return "", None, "", ""

    async def _login_page() -> bool:
        url = str(getattr(page, "url", "") or "").casefold()
        if "/login" in url:
            return True
        # 用登录页特有的密码输入框作为 URL 未刷新时的兜底。
        try:
            locator = page.locator('input[type="password"]').first
            return bool(await locator.is_visible())
        except Exception:
            return False

    async def _authenticated() -> bool:
        """确认登录态是否有效。

        与站点前端一致：先用 auth_token 调 /api/v1/auth/me；access_token 过期时，
        若 localStorage 存在 refresh_token 则先调 /api/v1/auth/refresh 刷新再重试。
        只有 refresh 也失败才判定未登录，避免把「仅 access_token 过期、会话仍有效」
        误判为需要账号密码重新登录。
        """
        try:
            result = await page.evaluate(
                """async (baseUrl) => {
                    const checkMe = async (token) => {
                        if (!token) return false;
                        try {
                            const response = await fetch(baseUrl + '/api/v1/auth/me', {
                                credentials: 'include',
                                headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
                            });
                            return Boolean(response.ok);
                        } catch (_) {
                            return false;
                        }
                    };
                    const refresh = async () => {
                        const rt = String(localStorage.getItem('refresh_token') || '').trim();
                        if (!rt) return '';
                        try {
                            const response = await fetch(baseUrl + '/api/v1/auth/refresh', {
                                method: 'POST',
                                credentials: 'include',
                                headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                                body: JSON.stringify({ refresh_token: rt }),
                            });
                            if (!response.ok) return '';
                            const text = await response.text();
                            let raw = null;
                            try { raw = JSON.parse(text); } catch (_) { /* 非 JSON */ }
                            const payload = raw && typeof raw.data === 'object' && raw.data ? raw.data : raw;
                            const access = payload && typeof payload.access_token === 'string'
                                ? payload.access_token.trim() : '';
                            if (access) {
                                localStorage.setItem('auth_token', access);
                                const newRefresh = payload && typeof payload.refresh_token === 'string'
                                    ? payload.refresh_token.trim() : '';
                                if (newRefresh) localStorage.setItem('refresh_token', newRefresh);
                                const expiresIn = Number(payload && payload.expires_in);
                                if (Number.isFinite(expiresIn) && expiresIn > 0) {
                                    localStorage.setItem('token_expires_at', String(Date.now() + expiresIn * 1000));
                                }
                            }
                            return access;
                        } catch (_) {
                            return '';
                        }
                    };
                    let token = String(localStorage.getItem('auth_token') || '').trim();
                    if (await checkMe(token)) return true;
                    const refreshed = await refresh();
                    if (refreshed) return await checkMe(refreshed);
                    return false;
                }""",
                origin,
            )
            return bool(result)
        except Exception:
            return False

    async def _dismiss_notice() -> None:
        """关闭「关于本站的使用说明」模态框：它遮住登录表单和 Turnstile。

        实测该模态框由 localStorage 的 sub2api_site_usage_notice_v1 控制：未置为
        accepted 就每次进站弹出。init script 已在导航前预置该标记；这里再做运行时
        兜底——主动写标记，并点击可见的「确认」按钮关闭已弹出的模态框。
        """
        try:
            await page.evaluate(
                """() => {
                    try { localStorage.setItem('sub2api_site_usage_notice_v1', 'accepted'); } catch (_) {}
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const btn of buttons) {
                        const text = String(btn.textContent || '').trim();
                        if (text === '确认' || text === '确定' || /^(confirm|ok|agree)$/i.test(text)) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        except Exception:
            pass

    async def _fill_login_form(email: str, password: str) -> bool:
        """填写站点真实登录页的邮箱/密码受控输入框（触发前端框架的 input 事件）。"""
        try:
            result = await page.evaluate(
                """([email, password]) => {
                    const assign = (selector, value) => {
                        const el = document.querySelector(selector);
                        if (!(el instanceof HTMLInputElement)) return false;
                        const desc = Object.getOwnPropertyDescriptor(
                            window.HTMLInputElement.prototype, 'value'
                        );
                        if (!desc || typeof desc.set !== 'function') return false;
                        desc.set.call(el, value);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    };
                    const okEmail = assign('#email', email) || assign('input[type="email"]', email);
                    const okPass = assign('#password', password) || assign('input[type="password"]', password);
                    return Boolean(okEmail && okPass);
                }""",
                [email, password],
            )
            return bool(result)
        except Exception:
            return False

    async def _submit_login(email: str, password: str, turnstile_token: str) -> dict[str, Any] | None:
        """调用站点公开登录接口，写入返回的 token，只回传非敏感诊断。"""
        try:
            result = await page.evaluate(
                """async ([baseUrl, email, password, turnstileToken]) => {
                    const shortMessage = (v) => String(v || '').replace(/[\\r\\n]/g, ' ').slice(0, 160);
                    try {
                        const response = await fetch(baseUrl + '/api/v1/auth/login', {
                            method: 'POST',
                            credentials: 'include',
                            headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                            body: JSON.stringify({ email, password, turnstile_token: turnstileToken }),
                        });
                        const text = await response.text();
                        let raw = null;
                        try { raw = JSON.parse(text); } catch (_) { /* 非 JSON */ }
                        const payload = raw && typeof raw.data === 'object' && raw.data ? raw.data : raw;
                        const accessToken = payload && typeof payload.access_token === 'string'
                            ? payload.access_token.trim() : '';
                        const refreshToken = payload && typeof payload.refresh_token === 'string'
                            ? payload.refresh_token.trim() : '';
                        const user = payload && payload.user && typeof payload.user === 'object'
                            ? payload.user : null;
                        if (response.ok && accessToken) {
                            localStorage.setItem('auth_token', accessToken);
                            if (refreshToken) localStorage.setItem('refresh_token', refreshToken);
                            if (user) localStorage.setItem('auth_user', JSON.stringify(user));
                            const expiresIn = Number(payload && payload.expires_in);
                            if (Number.isFinite(expiresIn) && expiresIn > 0) {
                                localStorage.setItem('token_expires_at', String(Date.now() + expiresIn * 1000));
                            }
                        }
                        const message = raw && typeof raw === 'object'
                            ? (raw.message || raw.detail || (payload && payload.message) || '') : '';
                        const twoFactor = Boolean(payload && (payload.temp_token || payload.two_factor_required));
                        return {
                            ok: Boolean(response.ok && accessToken),
                            status: response.status,
                            two_factor: twoFactor,
                            message: shortMessage(message),
                        };
                    } catch (error) {
                        return {
                            ok: false,
                            status: 0,
                            two_factor: false,
                            message: shortMessage(error && error.name === 'AbortError' ? 'fetch timeout' : error),
                        };
                    }
                }""",
                [origin, email, password, turnstile_token],
            )
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    async def _login_with_password() -> dict[str, Any] | None:
        """登录态失效时，用凭据在真实 /login 页完成一次自动登录。

        停留在站点真实登录页（不合成页面、不注入组件），只消费 Cloudflare 正常签发
        的 Turnstile 令牌；凭据只从 script_args/环境变量读取，不写入配置或日志。
        成功返回 None（继续签到主流程），失败返回结果 dict。
        """
        if not login_fallback_enabled:
            return helpers.need_login(
                "百倍登录态已失效，且账号密码登录兜底已禁用，请重新捕获 browser_state",
                {"target_url": resolved_url, "login_fallback": "disabled"},
            )

        # 凭据优先用 script_args 里直接填的 email/password；未填时回退环境变量。
        email = email_arg or os.getenv(email_env, "").strip()
        password = password_arg or os.getenv(password_env, "")
        if not email or not password:
            return helpers.need_login(
                "百倍登录态已失效；请在 script_args 填写 email/password（或配置环境变量），或重新捕获 browser_state",
                {
                    "target_url": resolved_url,
                    "login_fallback": "missing_credentials",
                    "email_env": email_env,
                    "password_env": password_env,
                },
            )

        # 打开干净登录页。根因：SPA 路由守卫看 localStorage 的 auth_user；仅删
        # localStorage 会被内存里的前端 auth store 在整页导航时从残留值重新写回，
        # 导致 /login 被弹回 dashboard（表现为频繁跳转）。因此每次导航后都验证
        # auth_user 已清空，未清空则再清一次并重试导航，消除清理与 store 初始化的竞争。
        async def _open_login_and_confirm() -> bool:
            # 清 auth 键的活由下方 init_script 在 document_start（导航前）无条件完成，
            # 这里不再用 page.evaluate 预清（那样赶不上 goto 后 store 的重新写回）。
            if context is not None:
                # 只清会话 cookie，务必保留 Cloudflare WAF 放行 cookie（cf_clearance
                # 等）。根因：整站清 cookie 会连 cf_clearance 一起删掉，Cloudflare
                # 随即拦截，/login 渲染为纯空白页（既无 dashboard 也无登录表单），
                # 被误判为「持续重定向」。保留 WAF cookie 才能正常打开登录页。
                try:
                    cookies = await context.cookies()
                except Exception:
                    cookies = []
                keep = [
                    c
                    for c in cookies
                    if str(c.get("name", "")).startswith(("cf_", "__cf"))
                ]
                try:
                    await context.clear_cookies()
                except Exception:
                    pass
                if keep:
                    try:
                        await context.add_cookies(keep)
                    except Exception:
                        pass
            await helpers.goto("/login?redirect=/check-in", timeout=goto_timeout, wait_until="commit")
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=ready_timeout)
            except Exception:
                pass
            # 整页导航后 store 从（应已空的）localStorage 初始化；确认 auth_user 已空。
            try:
                lingering = await page.evaluate(
                    "() => Boolean(String(localStorage.getItem('auth_user') || '').trim())"
                )
            except Exception:
                lingering = False
            return not bool(lingering)

        # 根因（实测定位）：/login 被 SPA 路由守卫弹回 dashboard，是因为 auth store
        # 在整页导航时从持久化（localStorage）重新读到 auth_user。用 page.evaluate 在
        # 导航「前」清 localStorage 赶不上——goto 后 store 又把 auth_user 写回。
        # 正解：用 add_init_script 在每次导航的 document_start（早于 Vue/Pinia 读取
        # localStorage）无条件清掉全部 auth 键，让 store 初始化时读到空、不再弹回；同时
        # 预置「使用说明」notice 标记避免模态框遮挡表单/Turnstile。sentinel 守护：登录
        # 成功后置位 __x100_login_reset='done' 即停止清理，避免把新登录的 token 也清掉。
        init_script = """
            try {
                if (localStorage.getItem('__x100_login_reset') !== 'done') {
                    for (const key of ['auth_token', 'refresh_token', 'auth_user', 'token_expires_at']) {
                        localStorage.removeItem(key);
                    }
                    sessionStorage.removeItem('auth_expired');
                }
                localStorage.setItem('sub2api_site_usage_notice_v1', 'accepted');
            } catch (_) { /* ignore */ }
        """
        if context is not None:
            try:
                await context.add_init_script(init_script)
            except Exception:
                pass

        loop = asyncio.get_running_loop()
        login_ready_deadline = loop.time() + min(login_timeout_ms, 30000) / 1000
        opened = False
        for _ in range(3):
            if await _open_login_and_confirm():
                opened = True
                break
            if loop.time() >= login_ready_deadline:
                break
            await page.wait_for_timeout(min(max(poll_interval_ms, 300), 800))

        if not opened:
            screenshot = await helpers.screenshot("100xlabs-login-form-unavailable.png")
            return helpers.need_config(
                "百倍登录页持续被重定向，无法进入登录表单（登录态残留未清除）",
                {"target_url": resolved_url, "login_fallback": "login_page_unavailable", "screenshot": screenshot},
            )

        # 轮询等待登录表单渲染（SPA 首次进入 /login 时密码框异步挂载）。
        # 每轮先关掉「使用说明」模态框，否则它遮住表单，填写会失败。
        form_deadline = loop.time() + min(login_timeout_ms, 30000) / 1000
        form_filled = False
        while True:
            await _dismiss_notice()
            if await _fill_login_form(email, password):
                form_filled = True
                break
            if loop.time() >= form_deadline:
                break
            await page.wait_for_timeout(min(max(poll_interval_ms, 300), 800))

        if not form_filled:
            screenshot = await helpers.screenshot("100xlabs-login-form-unavailable.png")
            return helpers.need_config(
                "百倍登录页字段未就绪，无法自动填写邮箱和密码",
                {"target_url": resolved_url, "login_fallback": "form_unavailable", "screenshot": screenshot},
            )

        # 获取 Cloudflare Turnstile 令牌（真实鼠标点击交互式 widget，见 browser.turnstile）。
        # 先再关一次「使用说明」模态框，避免它遮住 Turnstile widget。
        await _dismiss_notice()
        token = await turnstile.solve(page, timeout_ms=login_timeout_ms, poll_interval_ms=poll_interval_ms)

        if not token:
            screenshot = await helpers.screenshot("100xlabs-turnstile-timeout.png")
            return helpers.need_verification(
                "百倍 Turnstile 未在等待时间内自动签发；该站点验证可能需要人工完成，请重新捕获 browser_state",
                {
                    "target_url": resolved_url,
                    "login_fallback": "turnstile_timeout",
                    "login_timeout_ms": login_timeout_ms,
                    "screenshot": screenshot,
                },
            )

        login_result = await _submit_login(email, password, token)
        status = int((login_result or {}).get("status") or 0)
        if bool((login_result or {}).get("two_factor")):
            return helpers.need_login(
                "百倍账号启用了两步验证，需先在浏览器中完成验证码登录后重新捕获 browser_state",
                {"target_url": resolved_url, "login_fallback": "two_factor", "response_status": status},
            )
        if not bool((login_result or {}).get("ok")):
            if status in {400, 403, 429}:
                return helpers.need_verification(
                    f"百倍登录未通过验证（HTTP {status or 0}）",
                    {"target_url": resolved_url, "login_fallback": "login_rejected", "response_status": status},
                )
            return helpers.need_login(
                f"百倍账号密码登录失败（HTTP {status or 0}）",
                {"target_url": resolved_url, "login_fallback": "login_failed", "response_status": status},
            )

        if not await _authenticated():
            return helpers.need_login(
                "百倍登录接口成功但 /auth/me 验证未通过，请重试",
                {"target_url": resolved_url, "login_fallback": "auth_verification_failed"},
            )

        # 登录成功：置位 sentinel 为 'done'，停止 init script 清理，
        # 避免回签到页时把刚拿到的 token 也清掉。
        try:
            await page.evaluate("() => localStorage.setItem('__x100_login_reset', 'done')")
        except Exception:
            pass

        login_detail.update({"login_fallback": "password", "login_response_status": status})
        return None

    async def _persist_state() -> None:
        """把浏览器当前 storage_state 写回 ACCOUNTS.json，让 refresh_token 滚动续期。

        每次成功签到都续存最新登录态，从根本上缓解「登录态频繁失效」：只要脚本
        跑过一次，refresh_token 就会被刷新并存回，下次无需重新登录。任何异常都
        静默忽略（回写失败不影响本次签到结果）。
        """
        if context is None:
            return
        site_name = str(getattr(site, "name", "") or "").strip()
        site_base = str(getattr(site, "base_url", "") or "").strip()
        if not site_name and not site_base:
            return
        try:
            import accounts_store
            from browser import state as browser_state
        except Exception:
            return
        try:
            storage_state = await context.storage_state()
        except Exception:
            return
        try:
            encoded = browser_state.encode_state(storage_state)
        except Exception:
            return
        if not encoded:
            return
        try:
            accounts_store.update_account_auth_data(
                site_name, site_base, browser_state=encoded
            )
        except Exception:
            return

    async def _api_checkin() -> dict[str, Any] | None:
        """SPA 未渲染签到按钮时，用已登录的 auth_token 直接调用站点签到接口。

        百倍（Sub2API）的签到端点是 POST /api/v1/check-in（实测定位，与极速蹬的
        /api/v1/play/checkin 不同）。access_token 过期（HTTP 401）时复刻站点前端
        行为：用 refresh_token 调 /api/v1/auth/refresh 换新 token 后重试一次。只使用
        已存储凭据，不处理密码。返回 None 表示接口不可用（无 token / 请求异常）。
        """
        try:
            result = await page.evaluate(
                """async (baseUrl) => {
                    const parseBody = async (response) => {
                        const text = await response.text();
                        let raw = null;
                        try { raw = JSON.parse(text); } catch (_) { /* 非 JSON */ }
                        return raw;
                    };
                    const doCheckin = async (token) => {
                        const response = await fetch(baseUrl + '/api/v1/check-in', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                Authorization: `Bearer ${token}`,
                                Accept: 'application/json',
                                'Content-Type': 'application/json',
                            },
                            body: '{}',
                        });
                        const raw = await parseBody(response);
                        const payload = raw && typeof raw.data === 'object' && raw.data ? raw.data : raw;
                        const code = raw && typeof raw === 'object'
                            ? String(raw.code ?? (payload && payload.code) ?? '')
                            : '';
                        const message = raw && typeof raw === 'object'
                            ? String(raw.message || raw.detail || (payload && payload.message) || '')
                            : '';
                        const checkedFlag = Boolean(
                            payload && (payload.checked_in_today || payload.today_checked)
                        );
                        const already = response.status === 409
                            || checkedFlag
                            || /已签到|今日已|already/i.test(message + ' ' + code);
                        const businessOk = !raw || typeof raw !== 'object'
                            ? response.ok
                            : raw.success !== false && !(/^[1-9]\\d*$/.test(code));
                        return {
                            ok: Boolean(response.ok && businessOk && !already),
                            status: response.status,
                            already,
                            code: code.slice(0, 80),
                            message: message.replace(/[\\r\\n]/g, ' ').slice(0, 160),
                        };
                    };
                    const refreshToken = async () => {
                        const rt = String(localStorage.getItem('refresh_token') || '').trim();
                        if (!rt) return '';
                        try {
                            const response = await fetch(baseUrl + '/api/v1/auth/refresh', {
                                method: 'POST',
                                credentials: 'include',
                                headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
                                body: JSON.stringify({ refresh_token: rt }),
                            });
                            if (!response.ok) return '';
                            const raw = await parseBody(response);
                            const payload = raw && typeof raw.data === 'object' && raw.data ? raw.data : raw;
                            const access = payload && typeof payload.access_token === 'string'
                                ? payload.access_token.trim()
                                : '';
                            if (access) {
                                localStorage.setItem('auth_token', access);
                                const newRefresh = payload && typeof payload.refresh_token === 'string'
                                    ? payload.refresh_token.trim()
                                    : '';
                                if (newRefresh) localStorage.setItem('refresh_token', newRefresh);
                                const expiresIn = Number(payload && payload.expires_in);
                                if (Number.isFinite(expiresIn) && expiresIn > 0) {
                                    localStorage.setItem('token_expires_at', String(Date.now() + expiresIn * 1000));
                                }
                            }
                            return access;
                        } catch (_) {
                            return '';
                        }
                    };
                    let token = String(localStorage.getItem('auth_token') || '').trim();
                    if (!token) {
                        token = await refreshToken();
                        if (!token) {
                            return { ok: false, status: 401, already: false, code: 'NO_TOKEN', message: '' };
                        }
                    }
                    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
                    try {
                        let outcome = await doCheckin(token);
                        if (outcome.status === 401) {
                            const refreshed = await refreshToken();
                            if (refreshed) outcome = await doCheckin(refreshed);
                        }
                        // 502/503/504 等网关错误是服务端瞬时故障（非端点变更），重试至多两次。
                        for (let i = 0; i < 2 && outcome.status >= 502 && outcome.status <= 504; i++) {
                            await sleep(1500 * (i + 1));
                            outcome = await doCheckin(token);
                        }
                        return outcome;
                    } catch (_) {
                        return { ok: false, status: 0, already: false, code: 'FETCH_ERROR', message: '' };
                    }
                }""",
                origin,
            )
            return result if isinstance(result, dict) else None
        except Exception:
            return None

    # 无限跳转根因修复（实测定位）：token 已过期但 localStorage 里 auth_user 残留时，
    # /dashboard 守卫判「未登录」踢去 /login，/login 守卫判「已登录」（auth_user 在）
    # 又踢回 /dashboard，两个路由守卫互踢形成 /login↔/dashboard 无限跳转，且跳转期间
    # 页面执行上下文反复销毁，脚本 evaluate 全部失效、误判 login_page_unavailable。
    # 对策：在第一个 goto 之前注入 init script，于 document_start（早于 SPA 路由守卫
    # 读取 localStorage）检查 token_expires_at；已过期则清掉全部 auth 键，使登录态一致
    # 地落为「已登出」，页面干净停在 /login，把跳转打破在源头，再交给下方账密登录兜底。
    # token 未过期则完全不动，保住有效会话（实测有效 token 下页面本就稳定不跳）。
    preflight_init = """
        try {
            const exp = Number(localStorage.getItem('token_expires_at') || '0');
            if (Number.isFinite(exp) && exp > 0 && Date.now() >= exp) {
                for (const key of ['auth_token', 'refresh_token', 'auth_user', 'token_expires_at']) {
                    localStorage.removeItem(key);
                }
                sessionStorage.removeItem('auth_expired');
            }
            localStorage.setItem('sub2api_site_usage_notice_v1', 'accepted');
        } catch (_) { /* ignore */ }
    """
    if context is not None:
        try:
            await context.add_init_script(preflight_init)
        except Exception:
            pass

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

    # 登录闸门（实测定位后重写）：不能主动用 localStorage 的 auth_token 发 Bearer
    # 探测 /auth/me——Sub2API SPA 加载时会用 refresh_token 换出绑定当前会话的新 token
    # 且只留在内存、不写回 localStorage；localStorage 里的旧 token 已被服务端失效，
    # 主动探测必得 401，从而把「SPA 已正常登录、dashboard 正常渲染」的有效会话误判为
    # 未登录，错误触发账密兜底并引发 /login↔/dashboard 弹跳。
    #
    # 正确做法：只以页面是否真正落在 /login 作为判据。token 真过期时 preflight
    # init script 已在 document_start 清空 auth 键，配合上面的 domcontentloaded +
    # networkidle 等待，路由守卫早已把页面导到 /login；会话仍有效时 dashboard/签到页
    # 正常渲染，直接进入下方签到轮询（找到按钮或已签到状态即说明登录有效）。因此
    # 单次判断即可，不再空等——有效会话零额外等待直接进签到。
    login_attempted = False
    if await _login_page():
        login_attempted = True
        login_result = await _login_with_password()
        if login_result is not None:
            return login_result
        # 登录成功后回到签到页继续主流程。
        await helpers.goto(target_url, timeout=goto_timeout, wait_until="commit")
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
                "百倍登录后仍停留在登录页，请检查凭据或稍后重试",
                {"target_url": resolved_url, "login_fallback": "redirect_failed", **login_detail},
            )

    # 登录闸门已通过（复用/刷新/密码登录任一路径）。把浏览器当前 storage_state
    # 回写 ACCOUNTS.json，让 refresh_token 滚动续期，缓解「登录态频繁失效」。
    await _persist_state()

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
                {"matched_text": text, "completion_signal": "button_state", "target_url": resolved_url, **login_detail},
            )
        matched_already_text = await _find_already_text()
        if matched_already_text:
            return helpers.already_done(
                "今日已签到",
                {"matched_text": matched_already_text, "completion_signal": "page_text", "target_url": resolved_url, **login_detail},
            )

        checkin_control = await _find_checkin_control()
        if checkin_control is not None:
            break

        if loop.time() >= button_deadline:
            break
        remaining_ms = max(1, int((button_deadline - loop.time()) * 1000))
        await page.wait_for_timeout(min(poll_interval_ms * 3, remaining_ms))

    if checkin_control is None:
        # 登录态有效但 SPA 未渲染签到按钮（/check-in 主区常渲染滞后甚至空白）。
        # 用已登录的 auth_token 直接调用站点正式签到接口 POST /api/v1/check-in 兜底，
        # 避免把「页面没渲染」误报成需要人工签到。
        api_result = await _api_checkin()
        api_status = int((api_result or {}).get("status") or 0)
        api_detail = {
            "target_url": resolved_url,
            "completion_signal": "api_fallback",
            "response_status": api_status,
        }
        if bool((api_result or {}).get("already")):
            return helpers.already_done("今日已签到", api_detail)
        if bool((api_result or {}).get("ok")):
            return helpers.success("签到成功", api_detail)
        if (api_status in {401, 403} or api_status == 0) and not login_attempted:
            # token 与 refresh 均失效（会话确实无效）或页面空白 fetch 发不出：
            # 此前登录闸门未落到 /login、跳过了账密登录，现在触发账密登录并重试一次。
            login_attempted = True
            login_result = await _login_with_password()
            if login_result is not None:
                return login_result
            api_retry = await _api_checkin()
            retry_status = int((api_retry or {}).get("status") or 0)
            retry_detail = {
                "target_url": resolved_url,
                "completion_signal": "api_fallback_after_login",
                "response_status": retry_status,
            }
            if bool((api_retry or {}).get("already")):
                return helpers.already_done("今日已签到", retry_detail)
            if bool((api_retry or {}).get("ok")):
                return helpers.success("签到成功", retry_detail)
            api_status = retry_status
            api_detail = retry_detail

        if api_status in {401, 403}:
            return helpers.need_login("百倍签到登录态已失效，请重新捕获 browser_state", api_detail)

        screenshot = await helpers.screenshot("100xlabs-no-checkin-button.png")
        return helpers.need_config(
            f"未找到签到按钮文本：{', '.join(checkin_texts)}",
            {
                "checkin_texts": checkin_texts,
                "target_url": resolved_url,
                "button_wait_ms": button_wait_ms,
                "response_status": api_status,
                "screenshot": screenshot,
                **login_detail,
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
        # SPA 可能在“发现按钮”和“点击按钮”之间替换 DOM；点击器会重新定位并逐级降级。
        clicked_text, clicked_locator, clicked_kind, click_strategy = await _click_checkin(checkin_control)

        if not clicked_text:
            screenshot = await helpers.screenshot("100xlabs-click-failed.png")
            return helpers.error(
                "定位到签到按钮但点击失败，请稍后重试",
                {"checkin_texts": checkin_texts, "target_url": resolved_url, "screenshot": screenshot, **login_detail},
            )

        base_detail = {
            "clicked_text": clicked_text,
            "clicked_kind": clicked_kind,
            "click_strategy": click_strategy,
            "target_url": resolved_url,
            **login_detail,
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
