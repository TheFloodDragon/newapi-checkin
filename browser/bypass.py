#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反检测与绕过引擎：Camoufox + Cloudflare + 阿里云 WAF + 滑块验证。

集成 Camoufox（反检测浏览器）+ playwright-captcha（验证码破解）+ 
阿里云 WAF cookie 获取（acw_tc/cdn_sec_tc/acw_sc__v2），绕过公益站
常见的反爬措施。

核心功能：
1. launch_camoufox：启动反检测浏览器，支持 headless/humanize/proxy/geo。
2. get_cf_clearance：自动破解 Cloudflare Interstitial 拿 cf_clearance。
3. get_waf_cookies：预加载页面获取阿里云 WAF 三件套（acw_tc/cdn_sec_tc/acw_sc__v2）。
4. aliyun_captcha_solver：阿里云滑块拖拽（mouse 模拟，带人类化延迟和抖动）。

依赖：
- camoufox[geoip]：Firefox 反检测浏览器，绕过 webdriver 检测。
- playwright-captcha：Cloudflare/reCAPTCHA 破解（ClickSolver/SyncSolver）。
"""

from __future__ import annotations

import asyncio
import random
from typing import Any

try:
    from camoufox.async_api import AsyncCamoufox
    from playwright.async_api import Page, Browser, BrowserContext
    from playwright_captcha import ClickSolver, CaptchaType, FrameworkType
    CAMOUFOX_AVAILABLE = True
except ImportError as e:
    CAMOUFOX_AVAILABLE = False
    IMPORT_ERROR = str(e)
    # 占位类型，避免类型检查错误
    Page = Any  # type: ignore
    Browser = Any  # type: ignore
    BrowserContext = Any  # type: ignore


def _check_camoufox() -> None:
    """检查 Camoufox 是否已安装，未安装则抛出友好错误提示。"""
    if not CAMOUFOX_AVAILABLE:
        raise RuntimeError(
            f"Camoufox 未安装或导入失败：{IMPORT_ERROR}\n\n"
            "请安装依赖：\n"
            "  pip install camoufox[geoip]>=0.4.11 curl-cffi>=0.7.3 playwright-captcha>=0.1.0\n"
            "  python -m camoufox fetch\n\n"
            "或使用 uv（推荐）：\n"
            "  cd checkin && uv sync && uv run python -m camoufox fetch"
        )


def _normalize_proxy(proxy: Any) -> dict[str, str] | None:
    """把代理配置规整为 Camoufox/Playwright 需要的 dict 格式。

    Camoufox 内部对 proxy 参数执行 ``**proxy``，因此必须是映射（dict），
    形如 ``{"server": "http://host:port", "username": ..., "password": ...}``。
    历史上误传字符串会触发 "argument after ** must be a mapping, not str"。

    支持输入：
    - None / 空字符串 -> None（不使用代理）。
    - dict -> 直接返回（去掉空值）。
    - URL 字符串（http/https/socks5://[user:pass@]host:port）-> 解析成 dict。

    URL 中的用户名/密码会拆到 username/password，server 只保留 scheme://host:port，
    避免凭据重复导致部分实现鉴权失败。
    """
    if not proxy:
        return None

    if isinstance(proxy, dict):
        cleaned = {k: v for k, v in proxy.items() if v not in (None, "")}
        return cleaned or None

    if not isinstance(proxy, str):
        return None

    raw = proxy.strip()
    if not raw:
        return None

    # 缺少 scheme 时补 http://，让 urlsplit 能正确解析 host:port
    if "://" not in raw:
        raw = "http://" + raw

    from urllib.parse import urlsplit

    parts = urlsplit(raw)
    if not parts.hostname:
        # 无法解析出主机名，退回原始字符串作为 server（尽量不丢配置）
        return {"server": proxy.strip()}

    scheme = parts.scheme or "http"
    host = parts.hostname
    server = f"{scheme}://{host}:{parts.port}" if parts.port else f"{scheme}://{host}"

    result: dict[str, str] = {"server": server}
    if parts.username:
        from urllib.parse import unquote

        result["username"] = unquote(parts.username)
    if parts.password:
        from urllib.parse import unquote

        result["password"] = unquote(parts.password)
    return result


# ────────────────────────────── Camoufox 启动 ──────────────────────────────
async def launch_camoufox(
    headless: bool = True,
    proxy: str | None = None,
    humanize: bool = True,
    geoip: bool = True,
    locale: str = "en-US",
    timeout: int = 30000,
    os_fingerprint: str = "macos",
    **kwargs: Any,
) -> tuple[Browser, BrowserContext]:
    """启动 Camoufox 反检测浏览器（基于 Firefox）。

    Args:
        headless: 无头模式（CI 用 True，本地调试用 False）。
        proxy: 代理 URL（如 "http://user:pass@host:port"）。
        humanize: 人类化行为模拟（随机延迟、鼠标轨迹）。
        geoip: 根据代理 IP 自动设置地理位置和时区。
        locale: 浏览器语言（默认 en-US，CF/linux.do 对其更友好）。
        timeout: 启动超时（毫秒）。
        os_fingerprint: 强制操作系统指纹（默认 macos，避免 CI Windows
            下 navigator.platform 与 UA 不一致被风控识破）。
        **kwargs: 传给 AsyncCamoufox().start() 的额外参数（addons、viewport 等）。

    Returns:
        (browser, context) 元组。context 已配置好反检测参数。

    Raises:
        RuntimeError: Camoufox 未安装。
        Exception: 启动失败（如 camoufox 未安装、网络问题等）。
    """
    _check_camoufox()

    launch_options: dict[str, Any] = {
        "headless": headless,
        "humanize": humanize,
        "geoip": geoip,
        "locale": locale,
        "timeout": timeout,
        # 强制 OS 指纹：CI Windows 下用 macos 指纹避免 platform/UA 不一致
        "os": os_fingerprint,
        # forceScopeAccess：playwright-captcha 需要访问页面 JS 作用域
        "config": {"forceScopeAccess": True},
        "addons": kwargs.pop("addons", []),
    }

    proxy_dict = _normalize_proxy(proxy)
    if proxy_dict:
        # Camoufox 内部对 proxy 做 **proxy，必须是 dict（server/username/password）
        launch_options["proxy"] = proxy_dict

    # 合并用户自定义参数
    launch_options.update(kwargs)

    # Camoufox 返回的是已启动的 browser，不需要 async with
    browser = await AsyncCamoufox(**launch_options).start()
    # 某些 Camoufox/Playwright 组合不会预创建 context；直接 browser.new_context()
    # 会发送默认 viewport.isMobile=false，而当前 Firefox 协议 schema 不接受该字段。
    context = browser.contexts[0] if browser.contexts else await browser.new_context(no_viewport=True)
    
    # 不注册 context/pageerror 监听：Playwright Firefox 驱动在部分页面错误缺少
    # location.url 时会在 Node 侧崩溃（Cannot read properties of undefined）。同时在页面
    # 早期屏蔽未处理错误的默认上报，避免 Firefox 把这类错误继续转给 Playwright。
    try:
        await context.add_init_script(
            """(() => {
                const swallow = event => {
                    try { event.preventDefault(); } catch (_) {}
                    try { event.stopImmediatePropagation(); } catch (_) {}
                };
                try { window.addEventListener('error', swallow, true); } catch (_) {}
                try { window.addEventListener('unhandledrejection', swallow, true); } catch (_) {}
                try { window.onerror = () => true; } catch (_) {}
                try { window.onunhandledrejection = event => { try { event.preventDefault(); } catch (_) {} return true; }; } catch (_) {}
            })();"""
        )
    except Exception:
        pass
    return browser, context


# ──────────────────────── Cloudflare 挑战求解 ───────────────────────────
async def solve_cloudflare(page, log=None, wait_seconds: int = 10) -> bool:
    """在当前页面用 playwright-captcha 的 ClickSolver 自动破解 Cloudflare Interstitial。

    检测页面是否为 CF 挑战页（"Just a moment" / "Checking your browser"），
    是则调用 ClickSolver 自动点击验证。用参考项目验证过的正确调用方式。

    Args:
        page: Camoufox/Playwright Page 对象。
        log: 可选日志回调。
        wait_seconds: 验证通过后的额外等待（秒）。

    Returns:
        True 表示无 CF 挑战或已破解，False 表示破解失败。
    """
    _check_camoufox()

    def _log(msg: str) -> None:
        if log:
            log(msg)

    try:
        title = (await page.title()) or ""
        content = (await page.content()) or ""
    except Exception:
        title, content = "", ""

    if "Just a moment" not in title and "Checking your browser" not in content:
        return True  # 无 CF 挑战

    _log("检测到 Cloudflare 挑战，ClickSolver 自动破解中...")
    try:
        async with ClickSolver(
            framework=FrameworkType.CAMOUFOX, page=page, max_attempts=5, attempt_delay=3
        ) as solver:
            await solver.solve_captcha(
                captcha_container=page,
                captcha_type=CaptchaType.CLOUDFLARE_INTERSTITIAL,
            )
        await page.wait_for_timeout(wait_seconds * 1000)
        _log("Cloudflare 挑战已破解")
        return True
    except Exception as exc:
        _log(f"Cloudflare 自动破解失败：{exc}")
        return False


async def get_cf_clearance(
    page: Page,
    url: str,
    wait_seconds: int = 10,
    max_attempts: int = 3,
) -> dict[str, str]:
    """破解 Cloudflare 挑战并返回包含 cf_clearance 的 cookies（兼容旧接口）。"""
    _check_camoufox()
    try:
        if page.url != url:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    await solve_cloudflare(page, wait_seconds=wait_seconds)
    try:
        cookies = await page.context.cookies()
        return {c["name"]: c["value"] for c in cookies}
    except Exception:
        return {}


# ───────────────────────── 阿里云 WAF cookies ────────────────────────────
async def get_waf_cookies(
    page: Page,
    url: str,
    wait_seconds: int = 5,
) -> dict[str, str]:
    """预加载页面获取阿里云 WAF cookies（acw_tc / cdn_sec_tc / acw_sc__v2）。

    阿里云 WAF 会在首次访问时通过 JavaScript 动态生成这些 cookies，后续请求
    必须携带才能通过。本函数用浏览器预加载页面，等待 cookies 生成后返回。

    Args:
        page: Playwright Page 对象。
        url: 目标 URL（通常是站点首页或登录页）。
        wait_seconds: 等待 cookies 生成的时间（秒）。

    Returns:
        包含 WAF cookies 的字典，如 {"acw_tc": "xxx", "cdn_sec_tc": "yyy", ...}。
        若未检测到 WAF 则返回空字典。

    Raises:
        Exception: 页面加载失败。
    """
    try:
        # 用 domcontentloaded（networkidle 在 WAF 挑战页会一直不空闲而超时）
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(wait_seconds)

        # 提取所有 cookies
        cookies = await page.context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}

        # 过滤出 WAF cookies（阿里云 WAF 三件套）
        waf_keys = {"acw_tc", "cdn_sec_tc", "acw_sc__v2"}
        waf_cookies = {k: v for k, v in cookie_dict.items() if k in waf_keys}

        return waf_cookies

    except Exception as exc:
        raise Exception(f"获取 WAF cookies 失败：{exc}") from exc


# ───────────────────────── 阿里云滑块拖拽 ────────────────────────────────
async def aliyun_captcha_solver(
    page: Page,
    wait_seconds: int = 15,
    log=None,
) -> bool:
    """阿里云滑块验证码自动拖拽（人类化鼠标轨迹）。

    检测阿里云验证码页（#traceid），定位滑块手柄（#nocaptcha .btn_slide）
    和轨道（#nocaptcha .nc_scale），用 mouse API 模拟人类拖动绕过行为检测。
    选择器参考 aceHubert/newapi-ai-check-in 的 aliyun_captcha_check。

    Args:
        page: Camoufox/Playwright Page 对象。
        wait_seconds: 拖拽后等待验证结果的时间（秒）。
        log: 可选日志回调。

    Returns:
        True 表示无验证码或拖拽成功，False 表示失败。
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    # 检测是否为阿里云验证码页（traceid）
    try:
        traceid = await page.evaluate(
            """() => {
                const el = document.getElementById('traceid');
                if (el) {
                    const t = el.innerText || el.textContent || '';
                    const m = t.match(/TraceID:\\s*([a-f0-9]+)/i);
                    return m ? m[1] : (t || null);
                }
                return null;
            }"""
        )
    except Exception:
        traceid = None

    if not traceid:
        return True  # 无阿里云验证码

    _log(f"检测到阿里云滑块验证码（traceid={traceid}），尝试自动拖拽...")
    try:
        await page.wait_for_selector("#nocaptcha", timeout=60000)
        scale = await page.query_selector("#nocaptcha .nc_scale")
        handle = await page.query_selector("#nocaptcha .btn_slide")
        if not scale or not handle:
            _log("未找到滑块轨道或手柄")
            return False

        track = await scale.bounding_box()
        grip = await handle.bounding_box()
        if not track or not grip:
            _log("滑块元素无边界框")
            return False

        start_x = grip["x"] + grip["width"] / 2
        start_y = grip["y"] + grip["height"] / 2
        # 拖到轨道末端（参考项目用 handle.x + scale.width）
        end_x = grip["x"] + track["width"]

        await page.mouse.move(start_x, start_y)
        await asyncio.sleep(random.uniform(0.1, 0.3))
        await page.mouse.down()
        await asyncio.sleep(random.uniform(0.05, 0.15))

        # 分段拖动（ease-in-out + 抖动）
        steps = random.randint(15, 25)
        for i in range(steps):
            progress = (i + 1) / steps
            easing = 0.5 - 0.5 * ((2 * progress - 1) ** 3)
            cx = start_x + (end_x - start_x) * easing
            await page.mouse.move(cx + random.uniform(-2, 2), start_y + random.uniform(-1, 1))
            await asyncio.sleep(random.uniform(0.01, 0.03))

        await asyncio.sleep(random.uniform(0.1, 0.2))
        await page.mouse.up()
        await asyncio.sleep(wait_seconds)

        # 成功判定：traceid 元素消失或验证码容器隐藏
        still = await page.query_selector("#nocaptcha .btn_slide")
        ok = still is None
        _log("滑块验证" + ("通过" if ok else "可能未通过"))
        return ok
    except Exception as exc:
        _log(f"滑块拖拽失败：{exc}")
        return False

