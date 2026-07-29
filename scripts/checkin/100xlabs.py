#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""百倍 / 100xLabs（Sub2API）每日签到 browser_script。

站点差异只声明在下面的 SPEC 里，登录兜底、Turnstile、API 兜底、点击确认等
共享流程都在 scripts/checkin/_sub2api_common.py（与极速蹬同源）。

登录态优先复用 browser_state，过期时用 localStorage 的 refresh_token 刷新；
refresh_token 也失效时，可用 script_args 的 email/password（或环境变量）在真实
登录页完成登录——只消费 Cloudflare 正常签发的 Turnstile 令牌，不伪造、不绕过。
凭据不会写入账号配置、脚本结果或日志。

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

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _sub2api_common as common  # noqa: E402

SPEC = common.SiteSpec(
    site_label="百倍",
    checkin_path="/api/v1/check-in",
    # 实测 GET 该端点稳定回 {"data":{"checked_in_today":true,"today_reward":5,"balance":897}}。
    status_path="/api/v1/check-in/status",
    login_reset_sentinel="__x100_login_reset",
    screenshot_prefix="100xlabs",
    default_start_path="/check-in",
    email_env="X100LABS_EMAIL",
    password_env="X100LABS_PASSWORD",
    checkin_texts=("签到", "每日签到", "立即签到", "领取", "今日领取", "Check in", "Claim", "now"),
    already_texts=("已签到", "今日已签到", "已领取", "今日已领取", "Already", "Checked", "today"),
    success_texts=("签到成功", "领取成功", "成功", "获得", "Success"),
    # 监听 POST 签到响应；百倍前端路径可能是 /check-in 或 /checkin。
    response_match=("check-in", "checkin"),
    # "today" 过于宽泛（页面标题/日期也含它），仅在按钮被禁用时才采信为已签到。
    weak_already_texts=("today",),
    success_message="签到成功",
)


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """恢复登录态后执行百倍每日签到。"""
    opts = common.parse_options(SPEC, getattr(site, "script_args", {}))
    resolved_url = helpers.resolve_url(opts.start_target)
    origin = helpers.resolve_url("/").rstrip("/")
    login_detail: dict[str, Any] = {}

    async def do_login() -> dict[str, Any] | None:
        return await common.login_with_password(
            page,
            context,
            helpers,
            SPEC,
            opts,
            resolved_url=resolved_url,
            origin=origin,
            login_detail=login_detail,
        )

    # 无限跳转根因修复（实测定位）：token 已过期但 localStorage 里 auth_user 残留时，
    # /dashboard 守卫判「未登录」踢去 /login，/login 守卫判「已登录」（auth_user 在）
    # 又踢回 /dashboard，两个路由守卫互踢形成无限跳转，且跳转期间页面执行上下文
    # 反复销毁、脚本 evaluate 全部失效（曾被误报为 login_page_unavailable）。对策：
    # 导航前注入 init script，在 document_start 检查 token_expires_at，已过期则清掉
    # 全部 auth 键，让登录态一致地落为「已登出」，页面干净停在 /login，再交给账密
    # 登录兜底。token 未过期则完全不动，保住有效会话。
    await common.add_init_script(context, common.preflight_init_script())

    await helpers.goto(opts.start_target, timeout=opts.goto_timeout, wait_until=opts.wait_until)
    await common.settle_page(page, helpers, opts.start_target, opts)

    # 登录闸门：只以「页面是否真正落在 /login」为判据。不能主动用 localStorage 的
    # auth_token 探测 /auth/me——SPA 加载时会用 refresh_token 换出只存在内存的新
    # token，localStorage 里的旧 token 已被服务端失效，主动探测必得 401，会把正常
    # 会话误判为未登录并触发不必要的账密登录。
    login_attempted = False
    if await common.on_login_page(page):
        login_attempted = True
        failure = await do_login()
        if failure is not None:
            return failure
        await helpers.goto(opts.start_target, timeout=opts.goto_timeout, wait_until="commit")
        await common.settle_page(page, helpers, opts.start_target, opts)
        if await common.on_login_page(page):
            return helpers.need_login(
                "百倍登录后仍停留在登录页，请检查凭据或稍后重试",
                {"target_url": resolved_url, "login_fallback": "redirect_failed", **login_detail},
            )

    # 登录闸门已通过。把浏览器当前 storage_state 回写 ACCOUNTS.json，让
    # refresh_token 滚动续期，缓解「登录态频繁失效」。
    await common.persist_state(context, site)

    # SPA 的签到按钮要等前端拉完签到数据才渲染；轮询等待已签到状态或可点按钮。
    control, early_result = await common.wait_for_checkin_control(
        page, helpers, SPEC, opts, resolved_url=resolved_url, login_detail=login_detail
    )
    if early_result is not None:
        return early_result

    if control is None:
        # 登录态有效但 SPA 未渲染签到按钮（/check-in 主区常渲染滞后甚至空白）。
        # 用已登录的 auth_token 直接调 POST /api/v1/check-in 兜底，避免把
        # 「页面没渲染」误报成需要人工签到。
        return await common.api_fallback(
            page,
            helpers,
            SPEC,
            opts,
            origin=origin,
            resolved_url=resolved_url,
            login_attempted=login_attempted,
            do_login=do_login,
        )

    return await common.click_and_confirm(
        page,
        helpers,
        SPEC,
        opts,
        control,
        resolved_url=resolved_url,
        extra_detail=login_detail,
    )
