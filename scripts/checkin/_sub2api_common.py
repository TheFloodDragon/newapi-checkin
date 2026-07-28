#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sub2API 系站点 browser_script 的共享实现。

100xLabs 与极速蹬（jisudeng）都是 Sub2API 站点，登录/签到链路完全同构，
此前两个脚本各自维护了约 90% 逐字相同的代码（合计约 1890 行），任何接口
变更都要改两遍、测两遍。这里把同构部分收敛为一份，站点脚本只声明差异：

- 签到端点（100xLabs=/api/v1/check-in，极速蹬=/api/v1/play/checkin）
- 站点显示名（错误文案里的「百倍」/「极速蹬」）
- localStorage sentinel 键名（避免两站互相干扰）
- 按钮/文案候选词与默认入口路径

安全约定（与原实现一致，不放松）：
- 凭据只从 script_args 或环境变量读取，绝不写入配置、结果或日志；
- 只消费 Cloudflare 正常签发的 Turnstile 令牌，不伪造、不绕过；
- 回传的诊断信息不含邮箱/密码/token。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any

from browser import turnstile

# Sub2API 前端统一用这些 localStorage 键存登录态。
AUTH_KEYS = ("auth_token", "refresh_token", "auth_user", "token_expires_at")

# 「关于本站的使用说明」模态框的 localStorage 标记（两站同源，键名一致）。
NOTICE_KEY = "sub2api_site_usage_notice_v1"


@dataclass(frozen=True)
class SiteSpec:
    """站点差异声明：共享逻辑靠它区分 100xLabs 与极速蹬。"""

    # 站点中文名，用于结果消息（「百倍…」/「极速蹬…」）。
    site_label: str
    # 该 fork 的签到端点（100xLabs=/api/v1/check-in，极速蹬=/api/v1/play/checkin）。
    checkin_path: str
    # localStorage sentinel 键名。两站同时启用时必须各自独立，否则一站登录成功会
    # 让另一站的 init script 提前停止清理 auth 键。
    login_reset_sentinel: str
    # 截图文件名前缀（<缓存目录>/browser_script/<prefix>-*.png）。
    screenshot_prefix: str
    default_start_path: str = "/check-in"
    email_env: str = ""
    password_env: str = ""
    checkin_texts: tuple[str, ...] = ()
    already_texts: tuple[str, ...] = ()
    success_texts: tuple[str, ...] = ()
    # 监听签到 POST 响应时匹配的 URL 片段。
    response_match: tuple[str, ...] = ()
    # 极速蹬的 /play/checkin/makeup 是补签接口，监听签到响应时必须排除。
    response_exclude: tuple[str, ...] = ()
    # 已签到判定过于宽泛的词（如 "today"）只在按钮被禁用时才作准。
    weak_already_texts: tuple[str, ...] = ("today",)
    success_message: str = "签到成功"
    # detail.completion_signal 的标签。两站历史取值不同（100xLabs 用
    # button_state/page_text，极速蹬用 already_state），这些值会进结果 JSON 与
    # 报表，抽取公共逻辑时必须保持各自原样，不能统一，否则等于变更对外契约。
    signal_already_control: str = "button_state"
    signal_already_text: str = "page_text"
    signal_post_click_text: str = "already_text"


@dataclass
class ScriptOptions:
    """从 script_args 解析出的运行参数。"""

    checkin_texts: list[str] = field(default_factory=list)
    already_texts: list[str] = field(default_factory=list)
    success_texts: list[str] = field(default_factory=list)
    goto_timeout: int = 60000
    ready_timeout: int = 10000
    click_timeout: int = 5000
    button_wait_ms: int = 25000
    completion_timeout_ms: int = 10000
    poll_interval_ms: int = 100
    login_timeout_ms: int = 60000
    login_fallback: bool = True
    email: str = ""
    password: str = ""
    email_env: str = ""
    password_env: str = ""
    start_target: str = ""
    wait_until: str = "commit"


def _as_list(value: Any, default: tuple[str, ...]) -> list[str]:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(default)
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return list(default)


def _as_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() not in {"0", "false", "no", "off"}
    return bool(value)


def _as_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def parse_options(spec: SiteSpec, script_args: Any) -> ScriptOptions:
    """把 script_args 规范化为 ScriptOptions；缺省值取自 SiteSpec。"""
    args = dict(script_args or {}) if isinstance(script_args, dict) else {}
    start = (
        str(args.get("start_url") or args.get("url") or "").strip()
        or str(args.get("start_path") or args.get("path") or "").strip()
        or spec.default_start_path
    )
    completion = args.get("completion_timeout_ms", args.get("after_click_wait_ms", 10000))
    return ScriptOptions(
        checkin_texts=_as_list(args.get("checkin_text"), spec.checkin_texts),
        already_texts=_as_list(args.get("already_text"), spec.already_texts),
        success_texts=_as_list(args.get("success_text"), spec.success_texts),
        goto_timeout=_as_int(args.get("goto_timeout", 60000), 60000, 1),
        ready_timeout=_as_int(args.get("ready_timeout", 10000), 10000, 1),
        click_timeout=_as_int(args.get("click_timeout", 5000), 5000, 1),
        button_wait_ms=_as_int(args.get("button_wait_ms", 25000), 25000),
        completion_timeout_ms=_as_int(completion, 10000),
        poll_interval_ms=max(20, _as_int(args.get("poll_interval_ms", 100), 100, 1)),
        login_timeout_ms=max(1000, _as_int(args.get("login_timeout_ms", 60000), 60000, 1)),
        login_fallback=_as_bool(args.get("login_fallback", True), True),
        email=str(args.get("email") or "").strip(),
        password=str(args.get("password") or ""),
        email_env=str(args.get("email_env") or spec.email_env).strip() or spec.email_env,
        password_env=str(args.get("password_env") or spec.password_env).strip() or spec.password_env,
        start_target=start,
        wait_until=str(args.get("wait_until") or "commit"),
    )


# ── 页面探测原语 ────────────────────────────────────────────────────────────

def log(helpers: Any, message: str) -> None:
    """输出一行进度日志；helpers 没有 log 方法时静默跳过。

    browser_script 会跑几十秒（等 SPA 渲染、过 Turnstile、账密登录、API 兜底），
    没有过程日志时失败只剩一行结论，无法定位卡在哪一步。
    """
    fn = getattr(helpers, "log", None)
    if callable(fn):
        try:
            fn(message)
        except Exception:
            pass


async def is_visible(locator: Any) -> bool:
    try:
        return bool(await locator.is_visible())
    except Exception:
        return False


async def is_disabled(locator: Any) -> bool:
    try:
        return bool(await locator.is_disabled())
    except Exception:
        return False


async def visible_text(page: Any, text: str) -> bool:
    try:
        return await is_visible(page.get_by_text(text, exact=False).first)
    except Exception:
        return False


async def on_login_page(page: Any) -> bool:
    """是否停在登录页。

    URL 含 /login 即判定；URL 未刷新时用登录页特有的密码输入框兜底。
    注意不能用「欢迎回来」这类文本判断——dashboard 概览页也含该文案。
    """
    url = str(getattr(page, "url", "") or "").casefold()
    if "/login" in url:
        return True
    try:
        return bool(await page.locator('input[type="password"]').first.is_visible())
    except Exception:
        return False


# ── 登录态验证 / 刷新 ───────────────────────────────────────────────────────

_AUTHENTICATED_JS = """async (baseUrl) => {
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
}"""


async def authenticated(page: Any, origin: str) -> bool:
    """确认登录态是否有效。

    与站点前端一致：先用 auth_token 调 /api/v1/auth/me；access_token 过期时，
    若 localStorage 存在 refresh_token 则先调 /api/v1/auth/refresh 刷新再重试。
    只有 refresh 也失败才判定未登录，避免把「仅 access_token 过期、会话仍有效」
    误判为需要账号密码重新登录。
    """
    try:
        return bool(await page.evaluate(_AUTHENTICATED_JS, origin))
    except Exception:
        return False


_DISMISS_NOTICE_JS = """() => {
    try { localStorage.setItem('%s', 'accepted'); } catch (_) {}
    const buttons = Array.from(document.querySelectorAll('button'));
    for (const btn of buttons) {
        const text = String(btn.textContent || '').trim();
        if (text === '确认' || text === '确定' || /^(confirm|ok|agree)$/i.test(text)) {
            btn.click();
            return true;
        }
    }
    return false;
}""" % NOTICE_KEY


async def dismiss_notice(page: Any) -> None:
    """关闭「关于本站的使用说明」模态框：它遮住登录表单和 Turnstile。

    该模态框由 localStorage 的 sub2api_site_usage_notice_v1 控制：未置为
    accepted 就每次进站弹出。init script 已在导航前预置该标记；这里再做运行时
    兜底——主动写标记，并点击可见的「确认」按钮关闭已弹出的模态框。
    """
    try:
        await page.evaluate(_DISMISS_NOTICE_JS)
    except Exception:
        pass


_FILL_LOGIN_JS = """([email, password]) => {
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
}"""


async def fill_login_form(page: Any, email: str, password: str) -> bool:
    """填写站点真实登录页的邮箱/密码受控输入框（触发前端框架的 input 事件）。"""
    try:
        return bool(await page.evaluate(_FILL_LOGIN_JS, [email, password]))
    except Exception:
        return False


_SUBMIT_LOGIN_JS = """async ([baseUrl, email, password, turnstileToken]) => {
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
}"""


async def submit_login(
    page: Any,
    origin: str,
    email: str,
    password: str,
    turnstile_token: str,
) -> dict[str, Any] | None:
    """调用站点公开登录接口，写入返回的 token，只回传非敏感诊断。"""
    try:
        result = await page.evaluate(_SUBMIT_LOGIN_JS, [origin, email, password, turnstile_token])
        return result if isinstance(result, dict) else None
    except Exception:
        return None


async def persist_state(context: Any, site: Any) -> None:
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
        from browser import state as browser_state
        from providers import token_cache
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
        # 写运行期缓存而非 ACCOUNTS.json：浏览器每次打开站点都会刷新 cookie /
        # localStorage，写回配置会让用户文件被后台任务反复改写（也会和 GUI 里的
        # 手工编辑抢锁）。登录态属运行期产物，缓存已 gitignore。
        token_cache.save_browser_state(site_name, site_base, encoded)
    except Exception:
        return


# ── init script（导航前注入，破解路由守卫互踢）──────────────────────────────

def preflight_init_script() -> str:
    """token 已过期时在 document_start 清空 auth 键，避免 /login↔/dashboard 互踢。

    根因：token 过期但 localStorage 残留 auth_user 时，/dashboard 守卫判「未登录」
    踢去 /login，/login 守卫判「已登录」又踢回 /dashboard，两个守卫互踢形成无限
    跳转，且跳转期间页面执行上下文反复销毁、evaluate 全部失效。对策是在 SPA 路由
    守卫读取 localStorage 之前，把登录态一致地归零，让页面干净停在 /login。
    token 未过期则完全不动，保住有效会话。
    """
    keys = ", ".join(f"'{key}'" for key in AUTH_KEYS)
    return f"""
        try {{
            const exp = Number(localStorage.getItem('token_expires_at') || '0');
            if (Number.isFinite(exp) && exp > 0 && Date.now() >= exp) {{
                for (const key of [{keys}]) {{
                    localStorage.removeItem(key);
                }}
                sessionStorage.removeItem('auth_expired');
            }}
            localStorage.setItem('{NOTICE_KEY}', 'accepted');
        }} catch (_) {{ /* ignore */ }}
    """


def login_reset_init_script(sentinel: str) -> str:
    """账密登录期间在 document_start 无条件清空 auth 键。

    用 page.evaluate 在导航「前」清 localStorage 赶不上——goto 后 SPA 的 auth
    store 会从持久化值重新写回 auth_user，导致 /login 又被弹回 dashboard。必须用
    add_init_script 在每次导航的 document_start（早于框架读取 localStorage）清理。
    sentinel 守护：登录成功后置为 'done' 即停止清理，避免把新拿到的 token 也清掉。
    """
    keys = ", ".join(f"'{key}'" for key in AUTH_KEYS)
    return f"""
        try {{
            if (localStorage.getItem('{sentinel}') !== 'done') {{
                for (const key of [{keys}]) {{
                    localStorage.removeItem(key);
                }}
                sessionStorage.removeItem('auth_expired');
            }}
            localStorage.setItem('{NOTICE_KEY}', 'accepted');
        }} catch (_) {{ /* ignore */ }}
    """


async def add_init_script(context: Any, script: str) -> None:
    if context is None:
        return
    try:
        await context.add_init_script(script)
    except Exception:
        pass


async def mark_login_done(page: Any, sentinel: str) -> None:
    """置位 sentinel，停止 init script 清理（否则会清掉刚登录拿到的 token）。"""
    try:
        await page.evaluate(f"() => localStorage.setItem('{sentinel}', 'done')")
    except Exception:
        pass


async def keep_waf_cookies(context: Any) -> None:
    """只清会话 cookie，保留 Cloudflare 放行 cookie（cf_clearance 等）。

    整站清 cookie 会连 cf_clearance 一起删掉，Cloudflare 随即拦截，/login 渲染为
    纯空白页（既无 dashboard 也无登录表单），会被误判为「持续重定向」。
    """
    if context is None:
        return
    try:
        cookies = await context.cookies()
    except Exception:
        cookies = []
    keep = [c for c in cookies if str(c.get("name", "")).startswith(("cf_", "__cf"))]
    try:
        await context.clear_cookies()
    except Exception:
        pass
    if keep:
        try:
            await context.add_cookies(keep)
        except Exception:
            pass


# ── 账密登录兜底 ────────────────────────────────────────────────────────────

async def login_with_password(
    page: Any,
    context: Any,
    helpers: Any,
    spec: SiteSpec,
    opts: ScriptOptions,
    resolved_url: str,
    origin: str,
    login_detail: dict[str, Any],
) -> dict[str, Any] | None:
    """登录态失效时，用凭据在真实 /login 页完成一次自动登录。

    停留在站点真实登录页（不合成页面、不注入组件），只消费 Cloudflare 正常签发
    的 Turnstile 令牌；凭据只从 script_args/环境变量读取，不写入配置或日志。
    成功返回 None（继续签到主流程），失败返回结果 dict。
    """
    name = spec.site_label
    log(helpers, f"检测到未登录，开始{name}账密登录兜底")
    if not opts.login_fallback:
        return helpers.need_login(
            f"{name}登录态已失效，且账号密码登录兜底已禁用，请重新捕获 browser_state",
            {"target_url": resolved_url, "login_fallback": "disabled"},
        )

    email = opts.email or os.getenv(opts.email_env, "").strip()
    password = opts.password or os.getenv(opts.password_env, "")
    if not email or not password:
        return helpers.need_login(
            f"{name}登录态已失效；请在 script_args 填写 email/password（或配置环境变量），"
            "或重新捕获 browser_state",
            {
                "target_url": resolved_url,
                "login_fallback": "missing_credentials",
                "email_env": opts.email_env,
                "password_env": opts.password_env,
            },
        )

    await add_init_script(context, login_reset_init_script(spec.login_reset_sentinel))

    async def _open_login_and_confirm() -> bool:
        await keep_waf_cookies(context)
        await helpers.goto(
            f"/login?redirect={spec.default_start_path}",
            timeout=opts.goto_timeout,
            wait_until="commit",
        )
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=opts.ready_timeout)
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

    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(opts.login_timeout_ms, 30000) / 1000
    opened = False
    for _ in range(3):
        if await _open_login_and_confirm():
            opened = True
            break
        if loop.time() >= deadline:
            break
        await page.wait_for_timeout(min(max(opts.poll_interval_ms, 300), 800))

    if not opened:
        screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-login-form-unavailable.png")
        return helpers.need_config(
            f"{name}登录页持续被重定向，无法进入登录表单（登录态残留未清除）",
            {
                "target_url": resolved_url,
                "login_fallback": "login_page_unavailable",
                "screenshot": screenshot,
            },
        )

    # 轮询等待登录表单渲染（SPA 首次进入 /login 时密码框异步挂载）。
    # 每轮先关掉「使用说明」模态框，否则它遮住表单，填写会失败。
    form_deadline = loop.time() + min(opts.login_timeout_ms, 30000) / 1000
    form_filled = False
    while True:
        await dismiss_notice(page)
        if await fill_login_form(page, email, password):
            form_filled = True
            break
        if loop.time() >= form_deadline:
            break
        await page.wait_for_timeout(min(max(opts.poll_interval_ms, 300), 800))

    if not form_filled:
        screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-login-form-unavailable.png")
        return helpers.need_config(
            f"{name}登录页字段未就绪，无法自动填写邮箱和密码",
            {"target_url": resolved_url, "login_fallback": "form_unavailable", "screenshot": screenshot},
        )

    # 获取 Cloudflare Turnstile 令牌：交互式 widget 被动等待不签发，必须真实鼠标
    # 点击复选框（isTrusted 事件），逻辑封装在 browser.turnstile。
    await dismiss_notice(page)
    log(helpers, "等待 Cloudflare Turnstile 令牌（必要时真实点击复选框）...")
    token = await turnstile.solve(
        page, timeout_ms=opts.login_timeout_ms, poll_interval_ms=opts.poll_interval_ms
    )
    if not token:
        log(helpers, "Turnstile 未在等待时间内签发令牌")
        screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-turnstile-timeout.png")
        return helpers.need_verification(
            f"{name} Turnstile 未在等待时间内自动签发；该站点验证可能需要人工完成，"
            "请重新捕获 browser_state",
            {
                "target_url": resolved_url,
                "login_fallback": "turnstile_timeout",
                "login_timeout_ms": opts.login_timeout_ms,
                "screenshot": screenshot,
            },
        )

    result = await submit_login(page, origin, email, password, token)
    status = int((result or {}).get("status") or 0)
    if bool((result or {}).get("two_factor")):
        return helpers.need_login(
            f"{name}账号启用了两步验证，需先在浏览器中完成验证码登录后重新捕获 browser_state",
            {"target_url": resolved_url, "login_fallback": "two_factor", "response_status": status},
        )
    if not bool((result or {}).get("ok")):
        if status in {400, 403, 429}:
            return helpers.need_verification(
                f"{name}登录未通过验证（HTTP {status or 0}）",
                {"target_url": resolved_url, "login_fallback": "login_rejected", "response_status": status},
            )
        return helpers.need_login(
            f"{name}账号密码登录失败（HTTP {status or 0}）",
            {"target_url": resolved_url, "login_fallback": "login_failed", "response_status": status},
        )

    if not await authenticated(page, origin):
        return helpers.need_login(
            f"{name}登录接口成功但 /auth/me 验证未通过，请重试",
            {"target_url": resolved_url, "login_fallback": "auth_verification_failed"},
        )

    log(helpers, "账密登录成功，已验证登录态")
    await mark_login_done(page, spec.login_reset_sentinel)
    login_detail.update({"login_fallback": "password", "login_response_status": status})
    return None


# ── API 签到兜底 ────────────────────────────────────────────────────────────

def _api_checkin_js(checkin_path: str) -> str:
    """生成调用站点签到接口的 JS。各 fork 端点不同，由 SiteSpec 指定。"""
    return """async (baseUrl) => {
    const parseBody = async (response) => {
        const text = await response.text();
        let raw = null;
        try { raw = JSON.parse(text); } catch (_) { /* 非 JSON */ }
        return raw;
    };
    const doCheckin = async (token) => {
        const response = await fetch(baseUrl + '%s', {
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
        // 站点签到响应通常带 reward_amount / balance（实测极速蹬回
        // {"reward_amount":0.5,"balance_added":0.5,"streak_count":2}）。
        // 回传它们，让脚本结果能直接携带额度，无需再多打一次查询接口。
        const pickNum = (v) => (typeof v === 'number' && isFinite(v)) ? v : null;
        const reward = pickNum(payload && (payload.reward_amount ?? payload.balance_added ?? payload.today_reward));
        const balance = pickNum(payload && (payload.balance ?? payload.remaining ?? payload.current_balance));
        return {
            ok: Boolean(response.ok && businessOk && !already),
            status: response.status,
            reward,
            balance,
            already,
            code: code.slice(0, 80),
            message: message.replace(/[\\r\\n]/g, ' ').slice(0, 160),
        };
    };
    // 复刻站点前端 axios 拦截器：401 时用 refresh_token 刷新 access_token。
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
}""" % checkin_path


async def api_checkin(page: Any, spec: SiteSpec, origin: str) -> dict[str, Any] | None:
    """SPA 未渲染签到按钮时，用已登录的 auth_token 直接调用站点签到接口。

    只使用浏览器中已有的登录态（localStorage 的 auth_token）；access_token 过期
    （HTTP 401）时复刻站点前端行为，用 refresh_token 换新 token 后重试一次。
    不处理密码。返回 None 表示接口不可用（无 token / 请求异常）。
    """
    try:
        result = await page.evaluate(_api_checkin_js(spec.checkin_path), origin)
        return result if isinstance(result, dict) else None
    except Exception:
        return None


# ── 签到控件定位 ────────────────────────────────────────────────────────────
async def find_already_control(page: Any, spec: SiteSpec, opts: ScriptOptions) -> tuple[str, Any] | None:
    """找到表示「今日已签到」的控件；未命中返回 None。

    weak_already_texts 里的词（如 "today"）过于宽泛，只有在按钮被禁用时才采信，
    否则页面标题/日期里的 today 会被误判成已签到。
    """
    weak = {text.strip().casefold() for text in spec.weak_already_texts}
    for text in opts.already_texts:
        try:
            locator = page.get_by_role("button", name=text, exact=False).first
        except Exception:
            continue
        if not await is_visible(locator):
            continue
        if text.strip().casefold() not in weak or await is_disabled(locator):
            return text, locator
    return None


async def find_already_text(page: Any, spec: SiteSpec, opts: ScriptOptions) -> str:
    """在页面可见文本里找「已签到」提示；宽泛词不参与，避免误判。"""
    weak = {text.strip().casefold() for text in spec.weak_already_texts}
    for text in opts.already_texts:
        if text.strip().casefold() in weak:
            continue
        if await visible_text(page, text):
            return text
    return ""


async def find_checkin_control(page: Any, opts: ScriptOptions) -> tuple[str, Any, str] | None:
    """找到可见且未禁用的签到控件（不点击）；返回 (文案, locator, 控件类型)。

    按 button → link → 纯文本顺序尝试：先扫语义化控件，避免宽松文本候选点到
    页面标题；纯文本兜底兼容没有语义化标签的旧页面。
    """
    for role in ("button", "link"):
        for text in opts.checkin_texts:
            try:
                locator = page.get_by_role(role, name=text, exact=False).first
            except Exception:
                continue
            if not await is_visible(locator) or await is_disabled(locator):
                continue
            return text, locator, role
    for text in opts.checkin_texts:
        try:
            locator = page.get_by_text(text, exact=False).first
        except Exception:
            continue
        if not await is_visible(locator):
            continue
        return text, locator, "text"
    return None


async def click_checkin(
    page: Any,
    opts: ScriptOptions,
    initial: tuple[str, Any, str] | None = None,
) -> tuple[str, Any, str, str]:
    """多轮重新定位并点击，抵抗 SPA 重渲染、遮罩与元素抖动。

    逐级降级：普通点击 → force → dispatch_event → DOM click。元素可能在点击
    期间被前端替换，因此每轮都重新定位。返回 (文案, 元素, 类型, 生效策略)；
    全部失败返回空文案。
    """
    for attempt in range(3):
        control = initial if attempt == 0 and initial is not None else await find_checkin_control(page, opts)
        if control is None:
            await page.wait_for_timeout(min(150, opts.poll_interval_ms))
            continue
        text, locator, kind = control
        try:
            element = await locator.element_handle()
        except Exception:
            element = None
        try:
            await locator.scroll_into_view_if_needed(timeout=opts.click_timeout)
        except Exception:
            pass
        strategies = (
            ("normal", lambda: locator.click(timeout=opts.click_timeout)),
            ("force", lambda: locator.click(timeout=opts.click_timeout, force=True)),
            ("dispatch", lambda: locator.dispatch_event("click")),
            ("dom", lambda: locator.evaluate("el => el.click()")),
        )
        for strategy, do_click in strategies:
            try:
                await do_click()
                return text, element or locator, kind, strategy
            except Exception:
                continue
        await page.wait_for_timeout(min(200, max(50, opts.poll_interval_ms)))
    return "", None, "", ""


# ── 页面就绪 ────────────────────────────────────────────────────────────────
async def settle_page(page: Any, helpers: Any, target: str, opts: ScriptOptions) -> None:
    """导航到目标页并尽力等待 SPA 首屏数据落地。

    先等 domcontentloaded，再尽力等一次 networkidle：签到按钮要等前端 XHR 拉完
    数据才渲染，等待能显著降低「按钮刚要渲染、轮询窗口就到点」的竞态。两者
    超时都不致命，后续仍有 button_wait_ms 轮询兜底。
    """
    await helpers.goto(target, timeout=opts.goto_timeout, wait_until=opts.wait_until)
    for state, timeout in (
        ("domcontentloaded", opts.ready_timeout),
        ("networkidle", min(opts.ready_timeout, 8000)),
    ):
        try:
            await page.wait_for_load_state(state, timeout=timeout)
        except Exception:
            pass


# ── 纯 API 兜底 ─────────────────────────────────────────────────────────────
async def api_fallback(
    page: Any,
    helpers: Any,
    spec: SiteSpec,
    opts: ScriptOptions,
    origin: str,
    resolved_url: str,
    login_attempted: bool,
    do_login: Any,
) -> dict[str, Any]:
    """SPA 未渲染签到按钮时的接口兜底（含一次账密登录重试）。

    页面没渲染 ≠ 需要人工签到：登录态有效时直接打站点签到接口。401/403 或
    HTTP 0（页面空白导致 fetch 发不出）说明会话确实无效，此时若还没试过账密
    登录就登录一次并重试；login_attempted 守卫确保只重试一次，不会死循环。
    """
    log(helpers, f"页面未渲染签到按钮，改用接口兜底 POST {spec.checkin_path}")
    result = await api_checkin(page, spec, origin)
    status = int((result or {}).get("status") or 0)
    log(helpers, f"签到接口返回 HTTP {status or 0}")
    detail = {
        "target_url": resolved_url,
        "completion_signal": "api_fallback",
        "response_status": status,
    }
    # 签到接口通常回传 balance / reward_amount，透出去让 GUI 与汇总直接显示额度，
    # 免得「签到成功」却看不到到账多少（此前这些数字被丢弃）。
    quota = (result or {}).get("balance")
    awarded = (result or {}).get("reward")
    if bool((result or {}).get("already")):
        return helpers.already_done("今日已签到", detail, quota=quota)
    if bool((result or {}).get("ok")):
        return helpers.success(spec.success_message, detail, quota=quota, awarded=awarded)

    if (status in {401, 403} or status == 0) and not login_attempted:
        login_result = await do_login()
        if login_result is not None:
            return login_result
        retry = await api_checkin(page, spec, origin)
        status = int((retry or {}).get("status") or 0)
        detail = {
            "target_url": resolved_url,
            "completion_signal": "api_fallback_after_login",
            "response_status": status,
        }
        retry_quota = (retry or {}).get("balance")
        retry_awarded = (retry or {}).get("reward")
        if bool((retry or {}).get("already")):
            return helpers.already_done("今日已签到", detail, quota=retry_quota)
        if bool((retry or {}).get("ok")):
            return helpers.success(spec.success_message, detail, quota=retry_quota, awarded=retry_awarded)

    if status in {401, 403}:
        return helpers.need_login(f"{spec.site_label}签到登录态已失效，请重新捕获 browser_state", detail)

    screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-no-checkin-button.png")
    return helpers.need_config(
        f"{spec.site_label}页面未渲染签到按钮，且签到接口不可用（HTTP {status or 0}）",
        {
            "checkin_texts": opts.checkin_texts,
            "target_url": resolved_url,
            "button_wait_ms": opts.button_wait_ms,
            "response_status": status,
            "screenshot": screenshot,
        },
    )


async def click_and_confirm(
    page: Any,
    helpers: Any,
    spec: SiteSpec,
    opts: ScriptOptions,
    control: tuple[str, Any, str],
    *,
    resolved_url: str,
    extra_detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """点击签到控件并轮询确认完成信号。

    点击与确认必须放在一起：SPA 会在「发现按钮」和「点击按钮」之间重渲染 DOM，
    点击器需重新定位并逐级降级；确认阶段也要多路取证，只认可信信号，避免把
    「按钮文案变成 Loading」这类中间态误判为签到成功。

    完成信号（按可信度排序）：
    1. 签到接口响应状态码（最可信，直接来自服务端）；
    2. 页面出现成功文案；
    3. 按钮切换为「已签到」状态或已签文案出现；
    4. 按钮消失。
    全部拿不到则返回 error 并截图，不谎报成功。
    """
    base_extra = dict(extra_detail or {})
    response: dict[str, Any] = {}

    def _capture(item: Any) -> None:
        try:
            request = getattr(item, "request", None)
            if str(getattr(request, "method", "") or "").upper() != "POST":
                return
            url = str(getattr(item, "url", "") or "")
            lowered = url.casefold()
            if not any(marker in lowered for marker in spec.response_match):
                return
            if any(bad in lowered for bad in spec.response_exclude):
                return
            response.update({"status": int(getattr(item, "status", 0) or 0), "url": url})
        except Exception:
            return

    listener_registered = False
    try:
        page.on("response", _capture)
        listener_registered = True
    except Exception:
        pass

    try:
        clicked_text, clicked_locator, clicked_kind, strategy = await click_checkin(
            page, opts, initial=control
        )
        if clicked_text:
            log(helpers, f"已点击签到控件「{clicked_text}」（{clicked_kind} / {strategy}），等待完成信号...")
        if not clicked_text:
            screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-click-failed.png")
            return helpers.error(
                "定位到签到按钮但点击失败，请稍后重试",
                {
                    "checkin_texts": opts.checkin_texts,
                    "target_url": resolved_url,
                    "screenshot": screenshot,
                    **base_extra,
                },
            )

        base_detail = {
            "clicked_text": clicked_text,
            "clicked_kind": clicked_kind,
            "click_strategy": strategy,
            "target_url": resolved_url,
            **base_extra,
        }

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, opts.completion_timeout_ms) / 1000
        while True:
            status = int(response.get("status", 0) or 0)
            if 200 <= status < 300:
                return helpers.success(
                    spec.success_message,
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
                    f"签到接口返回错误（HTTP {status}）",
                    {
                        **base_detail,
                        "completion_signal": "checkin_response",
                        "response_status": status,
                        "response_url": response.get("url", ""),
                    },
                )

            for text in opts.success_texts:
                if await visible_text(page, text):
                    return helpers.success(
                        spec.success_message,
                        {**base_detail, "completion_signal": "success_text", "matched_text": text},
                    )

            already_control = await find_already_control(page, spec, opts)
            if already_control:
                text, _locator = already_control
                return helpers.success(
                    spec.success_message,
                    {**base_detail, "completion_signal": spec.signal_already_control, "matched_text": text},
                )

            matched_already = await find_already_text(page, spec, opts)
            if matched_already:
                return helpers.success(
                    spec.success_message,
                    {**base_detail, "completion_signal": spec.signal_post_click_text, "matched_text": matched_already},
                )

            if clicked_locator is not None and not await is_visible(clicked_locator):
                return helpers.success(
                    spec.success_message,
                    {**base_detail, "completion_signal": "button_hidden"},
                )

            if loop.time() >= deadline:
                break
            remaining_ms = max(1, int((deadline - loop.time()) * 1000))
            await page.wait_for_timeout(min(opts.poll_interval_ms, remaining_ms))

        screenshot = await helpers.screenshot(f"{spec.screenshot_prefix}-after-click.png")
        return helpers.error(
            "已点击签到按钮，但未检测到签到完成信号",
            {
                **base_detail,
                "completion_timeout_ms": opts.completion_timeout_ms,
                "screenshot": screenshot,
            },
        )
    finally:
        if listener_registered:
            try:
                page.remove_listener("response", _capture)
            except Exception:
                pass


async def wait_for_checkin_control(
    page: Any,
    helpers: Any,
    spec: SiteSpec,
    opts: ScriptOptions,
    *,
    resolved_url: str,
    login_detail: dict[str, Any],
) -> tuple[tuple[str, Any, str] | None, dict[str, Any] | None]:
    """轮询等待「已签到状态」或「可点击的签到按钮」出现。

    SPA 的签到按钮要等前端拉完签到数据后才渲染，goto 完成时通常还没出现，
    立即扫描会扑空。返回 (签到控件, 提前结束的结果)：
    - 命中已签到状态 → (None, already_done 结果)，调用方直接返回；
    - 找到可点击按钮 → (控件, None)，调用方继续点击；
    - 超时都没等到 → (None, None)，调用方走 API 兜底。
    """
    log(helpers, f"等待签到按钮渲染（最多 {opts.button_wait_ms}ms）...")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, opts.button_wait_ms) / 1000
    while True:
        already = await find_already_control(page, spec, opts)
        if already:
            text, _locator = already
            return None, helpers.already_done(
                "今日已签到",
                {
                    "matched_text": text,
                    "completion_signal": spec.signal_already_control,
                    "target_url": resolved_url,
                    **login_detail,
                },
            )
        matched_text = await find_already_text(page, spec, opts)
        if matched_text:
            return None, helpers.already_done(
                "今日已签到",
                {
                    "matched_text": matched_text,
                    "completion_signal": spec.signal_already_text,
                    "target_url": resolved_url,
                    **login_detail,
                },
            )

        control = await find_checkin_control(page, opts)
        if control is not None:
            log(helpers, f"发现可点击签到控件「{control[0]}」")
            return control, None

        if loop.time() >= deadline:
            return None, None
        remaining_ms = max(1, int((deadline - loop.time()) * 1000))
        await page.wait_for_timeout(min(opts.poll_interval_ms * 3, remaining_ms))
