#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""newapi + api 签到方式的 Cloudflare Turnstile 自动求解脚本。

适用场景：
  站点开启了 Cloudflare Turnstile 人机验证（/api/status 返回
  ``turnstile_check: true`` + ``turnstile_site_key``），纯 HTTP 无法
  自动获取令牌，导致 POST /api/user/checkin 被拒（"Turnstile token 为空"）。

工作原理（混合模型，与 millylee 模式同源）：
  1. 用 Camoufox 在站点 origin 下打开一个最小承载页（路由拦截返回空白 HTML，
     不下载站点 SPA bundle）；
  2. 在主世界（page.add_script_tag）注入 Turnstile widget，sitekey 来自
     /api/status；
  3. 挂载后先观察是否自动签发（managed/invisible 模式无需点击），
     未签发再用真实鼠标点击复选框；
  4. 令牌拿到后立即由 Python HTTP 层提交 /api/user/checkin?turnstile=…，
     不通过浏览器，复用 access_token 认证。

  全程自动，无需人工介入。只消费 Cloudflare 正常签发的令牌，不伪造、不绕过。
  令牌绑定（sitekey, hostname, 出口 IP），浏览器与 HTTP 层同机运行故 IP 一致。

使用方式：
  在管理界面把该站点的「脚本路径」填为
      scripts/newapi_turnstile.py
  签到方式保持 api（checkin_action=api）。
  若站点不要求 Turnstile，本脚本返回 None，由默认 HTTP 流程接管。

超时：启动浏览器需要时间，任务预算会自动升至 BROWSER_TASK（run__all_checkin.py
已通过「api+script」组合将此预算扩展到浏览器级别）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# 告知 run__all_checkin.py 本脚本需要浏览器级任务预算
NEEDS_BROWSER_BUDGET: bool = True

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from browser import bypass  # noqa: E402
from browser.runtime_loop import env_headless, run_sync, safe_goto  # noqa: E402
from providers.base import CheckinReward  # noqa: E402

# Turnstile widget 注入脚本（在页面主世界执行，不受 evaluate 隔离上下文影响）。
# 把签发的令牌写进 host 元素的 data-token 属性，供外部隔离上下文读取（DOM 共享）。
_WIDGET_BOOTSTRAP_JS = r"""
(() => {
  const SITEKEY = '__SITEKEY__';
  const host = document.createElement('div');
  host.id = 'ck-ts-host';
  host.setAttribute('data-state', 'init');
  host.style.cssText = 'position:fixed;left:24px;top:24px;width:320px;'
    + 'z-index:2147483647;background:#fff;padding:4px';
  const slot = document.createElement('div');
  slot.id = 'ck-ts-slot';
  host.appendChild(slot);
  document.body.appendChild(host);

  let widgetId = null;

  const render = () => {
    try {
      widgetId = window.turnstile.render(slot, {
        sitekey: SITEKEY,
        callback: (token) => {
          host.setAttribute('data-token', token);
          host.setAttribute('data-state', 'done');
        },
        'error-callback': (code) => {
          host.setAttribute('data-state', 'error');
          host.setAttribute('data-error', String(code || 'unknown'));
        },
        'timeout-callback': () => {
          host.setAttribute('data-state', 'timeout');
        },
      });
      host.setAttribute('data-state', 'rendered');
    } catch (e) {
      host.setAttribute('data-state', 'error');
      host.setAttribute('data-error', String((e && e.message) || e));
    }
  };

  // 隔离上下文（page.evaluate）拿不到页面的 window.turnstile，无法直接 reset。
  // 用 data-cmd 属性做命令通道：外部写入 reset，主世界这里执行后清回 rendered。
  // Cloudflare 文档把 600xxx 归为可重试错误，重试前必须 reset，否则 widget 会
  // 一直停在错误态，后续轮询只是空等。
  new MutationObserver(() => {
    if (host.getAttribute('data-cmd') !== 'reset') return;
    host.removeAttribute('data-cmd');
    try {
      host.removeAttribute('data-error');
      host.removeAttribute('data-token');
      host.setAttribute('data-state', 'rendered');
      if (widgetId !== null) { window.turnstile.reset(widgetId); }
      else { render(); }
    } catch (e) {
      host.setAttribute('data-state', 'error');
      host.setAttribute('data-error', 'reset failed: ' + String((e && e.message) || e));
    }
  }).observe(host, { attributes: true, attributeFilter: ['data-cmd'] });

  if (window.turnstile && window.turnstile.render) { render(); return; }
  const s = document.createElement('script');
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.async = true;
  s.onload = () => {
    let n = 0;
    const w = setInterval(() => {
      if (window.turnstile && window.turnstile.render) { clearInterval(w); render(); }
      else if (++n > 100) { clearInterval(w); host.setAttribute('data-state', 'no-global'); }
    }, 100);
  };
  s.onerror = () => {
    host.setAttribute('data-state', 'error');
    host.setAttribute('data-error', 'api.js load failed');
  };
  document.head.appendChild(s);
})();
"""

# 读取 widget 状态（在隔离上下文安全运行，只访问 DOM 属性）。
#
# 令牌有两个来源，必须都读：
# 1. data-token —— 我们注入的 callback 写入；
# 2. input[name=cf-turnstile-response] —— Turnstile 自己在 widget 内创建并填充。
# 只读 (1) 会漏掉「人工完成了验证但 callback 没触发」的情况，表现为「明明点过却不算数」。
# 页面可能同时存在站点自己的 widget 字段，因此取第一个非空值。
_STATE_JS = """() => {
  const host = document.getElementById('ck-ts-host');
  const slot = document.getElementById('ck-ts-slot');
  const r = slot ? slot.getBoundingClientRect() : null;
  let token = (host && host.getAttribute('data-token')) || '';
  if (!token) {
    for (const f of document.querySelectorAll(
      'input[name="cf-turnstile-response"], textarea[name="cf-turnstile-response"]'
    )) {
      const v = typeof f.value === 'string' ? f.value : String(f.textContent || '');
      if (v.trim()) { token = v.trim(); break; }
    }
  }
  return {
    state: (host && host.getAttribute('data-state')) || 'missing',
    error: (host && host.getAttribute('data-error')) || '',
    token: token,
    slot: r ? { x: r.x, y: r.y, w: r.width, h: r.height } : null,
  };
}"""

# 触发 widget reset 的命令（写入 host 的 data-cmd，由主世界的 MutationObserver 执行）
_RESET_JS = """() => {
  const host = document.getElementById('ck-ts-host');
  if (host) host.setAttribute('data-cmd', 'reset');
}"""

# Turnstile widget 的最小有效高度（未挂载时为 0，挂载后约 65–74px）
_MIN_WIDGET_HEIGHT = 50

# 轮询间隔（ms）。widget 挂载只需 1–2s，用秒级间隔会把「已就绪」的发现推迟近一秒，
# 点击也因此晚一轮；200ms 足够及时且开销可忽略（一次 evaluate 读几个 DOM 属性）。
_POLL_INTERVAL_MS = 200

# 点击后等待令牌的上限（ms）。实测正常签发在点击后 1–5s 内完成；等到 20s 一次都没
# 见过成功，只是把失败推迟。12s 覆盖慢网络后就该让位给 reset 重试。
_TOKEN_WAIT_MS = 12_000

# widget 挂载等待上限（ms）。api.js 下载 + render 通常 1–8s，但 Cloudflare 侧波动时
# 实测会超过 12s；提高上限不影响成功路径，只避免把慢挂载误判成失败。
_MOUNT_WAIT_MS = 20_000

# widget 报错后仍继续观察令牌的宽限期（ms）。600xxx 偶尔会先报错再自动恢复成签发，
# 留一小段观察是值当的；但等太久没有意义——reset 重试比干等更可能拿到令牌。
_ERROR_GRACE_MS = 1_500

# 承载 widget 的路径。用一个站点前端不会接管的路径，避免 SPA 路由抢走渲染。
_HOST_PATH = "/__checkin_turnstile__"



# 整体尝试次数。一次失败后 reset 再试一次足够；连续两次仍失败通常是当前 IP/指纹被
# Cloudflare 风控，立即第三次成功率很低，只会再多耗几十秒，应交给下次任务重试。
_MAX_ATTEMPTS = 2
# 重试前短暂冷却（ms），让 reset 完成并重新挂载。
_RETRY_COOLDOWN_MS = 2_000


def _fetch_status_data(client: Any) -> dict:
    """读取站点 /api/status（不要求登录态）。"""
    try:
        payload = client.request("GET", "/api/status")
        return (payload or {}).get("data") if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _log_fn(log: Any) -> Any:
    def _log(msg: str) -> None:
        if callable(log):
            try:
                log(msg)
            except Exception:
                pass
    return _log


async def _click_checkbox(page: Any, slot: dict, log: Any) -> None:
    """用真实鼠标事件点击 Turnstile 复选框。

    Cloudflare 校验 isTrusted，JS click 无效，必须走 page.mouse。
    Turnstile 用 closed shadow root，内部 iframe 定位不到，容器矩形是唯一可用几何：
    复选框在容器左侧约 30px、垂直居中。
    点击前做一小段移动轨迹降低自动化判定概率——但不做长时间停顿，那只会推迟签发。
    """
    cx = slot["x"] + 30
    cy = slot["y"] + slot["h"] / 2
    log(f"widget 已就绪，真实鼠标点击复选框 @({cx:.0f},{cy:.0f})")
    # Camoufox 的 humanize 提供 Cloudflare 能识别的人类化指针事件；完全关闭时实测
    # widget 保持 rendered 但静默不签发。steps 必须很小：Camoufox 会把每个 step
    # 都人类化，旧值 6/8 把 60px 移动拖到 13–25s；A/B 实测 2/2 是能签发的最小值。
    approach_x = max(cx + 60, 8.0)
    approach_y = max(cy + 40, 8.0)
    await page.mouse.move(approach_x, approach_y, steps=2)
    await page.mouse.move(cx, cy, steps=2)
    await page.mouse.click(cx, cy)


async def _poll_token(
    page: Any, deadline: float, log: Any, stage: str
) -> tuple[str, str]:
    """轮询令牌直到出现、widget 进入终态、或到达 deadline。

    Returns:
        (token, reason)。token 非空表示成功。
    """
    import time

    error_deadline: float | None = None
    while True:
        info = await page.evaluate(_STATE_JS)
        token: str = info.get("token") or ""
        if token:
            return token, ""

        state = info.get("state") or "missing"
        err = info.get("error") or ""
        now = time.monotonic()

        # 这两种状态重试也不会变好，立即上抛让调用方决策
        if state == "missing":
            return "", "widget 容器丢失（页面可能已跳转）"
        if state == "no-global":
            return "", "Turnstile api.js 未就绪"

        if state in {"error", "timeout"}:
            # 600xxx 偶尔先报错再自行恢复成签发，留一小段宽限；超过就别干等了，
            # reset 重试比继续等更可能拿到令牌。
            if error_deadline is None:
                error_deadline = now + _ERROR_GRACE_MS / 1000
            elif now >= error_deadline:
                return "", f"widget 错误 {err or state}"
        elif error_deadline is not None:
            error_deadline = None  # 已自行恢复，撤销宽限计时

        if now >= deadline:
            return "", f"{stage}超时"
        await page.wait_for_timeout(_POLL_INTERVAL_MS)


async def _one_attempt(page: Any, attempt: int, log: Any) -> tuple[str, str]:
    """单次全自动求解：等挂载 → 先看是否自动签发 → 必要时真实点击 → 轮询令牌。

    Returns:
        (token, reason)。token 非空表示成功；否则 reason 说明本次失败原因。
    """
    import time

    # 1) 等 widget 挂载（拿到可点几何）
    mount_deadline = time.monotonic() + _MOUNT_WAIT_MS / 1000
    slot: dict = {}
    while True:
        info = await page.evaluate(_STATE_JS)
        token: str = info.get("token") or ""
        if token:  # 极快的自动签发，连挂载轮询都没走完
            log(f"令牌已自动签发（{len(token)} 字符，无需点击）")
            return token, ""
        state = info.get("state") or "missing"
        if state == "missing":
            return "", "widget 容器丢失（页面可能已跳转）"
        if state == "no-global":
            return "", "Turnstile api.js 未就绪"
        slot = info.get("slot") or {}
        if slot.get("h", 0) >= _MIN_WIDGET_HEIGHT:
            break
        if time.monotonic() >= mount_deadline:
            # 带上 widget 自报状态：Cloudflare 拒绝渲染时容器高度会一直是 0，
            # 只说「挂载超时」无法区分「还没挂上」和「已被判定为自动化」。
            err = info.get("error") or ""
            detail = f"state={state}" + (f" err={err}" if err else "")
            return "", f"widget 挂载超时（{detail}，容器高度 {slot.get('h', 0)}）"
        await page.wait_for_timeout(_POLL_INTERVAL_MS)

    # 可见 widget 已挂载且仍无 token，说明需要交互：立即点击。
    # 非交互式 widget 若能自动签发，挂载循环已在每轮优先读取 token，不需要再白等窗口。
    await _click_checkbox(page, slot, log)

    # 3) 轮询令牌
    token, reason = await _poll_token(
        page, time.monotonic() + _TOKEN_WAIT_MS / 1000, log, "等待令牌"
    )
    if token:
        log(f"Turnstile 令牌已签发（{len(token)} 字符）")
    return token, reason


async def _open_widget_host(page: Any, base_url: str, log: Any) -> None:
    """打开一个位于站点 origin 下的最小页面，用于承载 Turnstile widget。

    Turnstile 只校验 (sitekey, hostname)，不关心页面内容。而站点首页是 React SPA，
    要下载数 MB bundle 并执行整套前端（实测 ~3s，且站点脚本可能干扰注入）。
    这里用路由拦截，在同一 origin 下直接返回一个空白 HTML：hostname 不变，
    令牌照样有效，却省掉 bundle 下载与 SPA 启动。

    拦截失败时回落到真实导航，保证功能不因优化而丢失。
    """
    target = base_url.rstrip("/") + _HOST_PATH
    try:
        await page.route(
            target,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body="<!doctype html><html><head><title>checkin</title></head><body></body></html>",
            ),
        )
        await safe_goto(page, target, wait_until="domcontentloaded", timeout=20000, log=log)
        host = await page.evaluate("() => location.hostname")
        if host and str(host) in base_url:
            log(f"已在 {host} 下打开最小承载页（跳过 SPA 加载）")
            return
        log("承载页 hostname 校验未通过，回落真实导航")
    except Exception as exc:
        log(f"最小承载页不可用（{type(exc).__name__}: {exc}），回落真实导航")

    await safe_goto(page, base_url, wait_until="domcontentloaded", timeout=45000, log=log)
    await bypass.solve_cloudflare(page, log=log, wait_seconds=15)


async def _solve_turnstile(
    base_url: str,
    sitekey: str,
    proxy: str,
    log_fn: Any,
) -> str:
    """启动 Camoufox，注入 widget，求解并返回令牌。失败返回空字符串。"""

    def _log(msg: str) -> None:
        log_fn(msg)

    headless = env_headless()
    _log(f"启动 Camoufox（headless={headless}）获取 Turnstile 令牌...")
    browser, context = await bypass.launch_camoufox(
        headless=headless,
        # 必须启用：关闭后即使 isTrusted 点击正确，Cloudflare 也会静默不签发。
        # 用数值限制单次人类化移动的目标时长；真正的总耗时还由 steps 数决定，
        # 因此 _click_checkbox 固定使用 A/B 验证过的最小 steps=2/2。
        humanize=0.6,
        geoip=True,
        proxy=proxy or None,
    )
    try:
        page = await context.new_page()
        await _open_widget_host(page, base_url, _log)
        _log("注入 Turnstile widget（主世界）...")
        await page.add_script_tag(
            content=_WIDGET_BOOTSTRAP_JS.replace("__SITEKEY__", sitekey)
        )

        reason = ""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            token, reason = await _one_attempt(page, attempt, _log)
            if token:
                return token
            if attempt >= _MAX_ATTEMPTS:
                break
            _log(f"第 {attempt} 次失败（{reason}），reset widget 后重试")
            await page.evaluate(_RESET_JS)
            await page.wait_for_timeout(_RETRY_COOLDOWN_MS)

        _log(f"未能拿到令牌（最后原因：{reason}）")
        return ""
    finally:
        try:
            await browser.close()
        except Exception:
            pass


def turnstile_checkin(
    client: Any,
    status_data: dict[str, Any] | None = None,
    log: Any = None,
) -> CheckinReward | None:
    """仅执行 Turnstile 机制；不适用时返回 None，不委派其它验证方式。"""
    from providers.base import ApiError  # 延迟导入，避免循环

    _log = _log_fn(log)
    options = status_data if isinstance(status_data, dict) else _fetch_status_data(client)
    options = options if isinstance(options, dict) else {}
    turnstile_check = bool(options.get("turnstile_check"))
    sitekey = str(options.get("turnstile_site_key") or "").strip()
    if not turnstile_check or not sitekey:
        _log(
            f"Turnstile 不适用（turnstile_check={turnstile_check}，sitekey={sitekey!r}）"
        )
        return None

    _log(f"站点启用 Turnstile（sitekey={sitekey[:20]}...），开始浏览器求解")
    base_url = str(getattr(client, "base_url", "") or "").rstrip("/")
    proxy = str(getattr(client.site, "proxy", "") or "")
    token = run_sync(_solve_turnstile(base_url, sitekey, proxy, _log))
    if not token:
        raise ApiError(
            None,
            None,
            "Turnstile 令牌求解失败（浏览器超时或被 Cloudflare 风控）；"
            "可尝试为该站点配置住宅代理，或等待下次任务自动重试。",
            transient=True,
        )

    _log("令牌已获取，提交 legacy 签到接口...")
    saved_variant = getattr(client.site, "api_variant", "auto")
    try:
        client.site.api_variant = "legacy"
        data = client._legacy_checkin(token)
    finally:
        client.site.api_variant = saved_variant
    _log(f"签到完成，原始返回：{str(data)[:200]}")
    return client._reward_from(data)


def do_checkin(client: Any, log: Any = None) -> CheckinReward | None:
    """旧脚本兼容入口：Turnstile 优先，不适用时委派统一验证路由。"""
    _log = _log_fn(log)
    _log("Turnstile 兼容脚本已接入，读取站点配置...")
    status_data = _fetch_status_data(client)
    reward = turnstile_checkin(client, status_data=status_data, log=log)
    if reward is not None:
        return reward
    from scripts import newapi_verification

    return newapi_verification.do_checkin(
        client, log=log, status_data=status_data, preferred="auto"
    )
