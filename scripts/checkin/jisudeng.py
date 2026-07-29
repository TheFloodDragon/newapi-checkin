#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极速蹬（jisudeng.com）每日签到 browser_script。

该站点是 Sub2API 系（Vue SPA + Cloudflare Turnstile 登录），与百倍
（100xlabs）同构，因此共享逻辑集中在 scripts/checkin/_sub2api_common.py，
本文件只声明站点差异（签到端点、按钮文案、截图前缀等）并串起主流程。

站点特征：
- 可签到按钮：立即签到
- 已签到状态：今日已签到
- 签到接口：POST /api/v1/play/checkin（补签 /makeup 需排除）

登录态优先复用 browser_state，过期时用 localStorage 的 refresh_token 刷新；
refresh_token 也失效时，可用 script_args 或环境变量中的邮箱密码在真实登录页
完成登录（只消费 Cloudflare 正常签发的 Turnstile 令牌，不伪造、不绕过）。
凭据不会写入账号配置、脚本结果或日志。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# browser_script 运行器用 spec_from_file_location 加载本文件，父目录不在
# sys.path 上，因此显式加入后再导入同目录的共享模块。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _sub2api_common as common  # noqa: E402

SPEC = common.SiteSpec(
    site_label="极速蹬",
    checkin_path="/api/v1/play/checkin",
    status_path="/api/v1/play/checkin/status",
    login_reset_sentinel="__jsd_login_reset",
    screenshot_prefix="jisudeng",
    default_start_path="/check-in",
    email_env="JISUDENG_EMAIL",
    password_env="JISUDENG_PASSWORD",
    checkin_texts=("立即签到",),
    already_texts=("今日已签到", "已签到"),
    success_texts=("已到账", "签到成功"),
    response_match=("/play/checkin",),
    # /play/checkin/makeup 是补签接口，监听签到响应时必须排除。
    response_exclude=("/play/checkin/makeup",),
    # 极速蹬的已签到文案都足够具体，无需弱文案特例。
    weak_already_texts=(),
    success_message="极速蹬签到成功",
    # 极速蹬历史上统一用 already_state 表达「已签到」信号，保持不变。
    signal_already_control="already_state",
    signal_already_text="already_state",
    signal_post_click_text="already_state",
)


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """恢复登录态后执行极速蹬每日签到。"""
    opts = common.parse_options(SPEC, getattr(site, "script_args", {}))
    start_target = opts.start_target or SPEC.default_start_path
    resolved_url = helpers.resolve_url(start_target)
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
    # 反复销毁、脚本 evaluate 全部失效。对策：导航前注入 init script，在
    # document_start 检查 token_expires_at，已过期则清掉全部 auth 键，让登录态一致
    # 地落为「已登出」，页面干净停在 /login，再交给账密登录兜底。
    await common.add_init_script(context, common.preflight_init_script())

    await helpers.goto(start_target, timeout=opts.goto_timeout, wait_until=opts.wait_until)
    await common.settle_page(page, helpers, start_target, opts)

    # 登录闸门：只以页面是否真正落在 /login 为判据。不能主动用 localStorage 的
    # auth_token 探测 /auth/me——SPA 加载时会用 refresh_token 换出只存在内存的新
    # token，localStorage 里的旧 token 已被服务端失效，主动探测必得 401，会把
    # 「已正常登录」误判为未登录并触发不必要的账密兜底。
    login_attempted = False
    if await common.on_login_page(page):
        login_attempted = True
        login_result = await do_login()
        if login_result is not None:
            return login_result
        await helpers.goto(start_target, timeout=opts.goto_timeout, wait_until="commit")
        await common.settle_page(page, helpers, start_target, opts)
        if await common.on_login_page(page):
            return helpers.need_login(
                "极速蹬登录后仍停留在登录页，请检查凭据或稍后重试",
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
        # 登录态有效但 SPA 未渲染签到按钮（控制台常跳 /dashboard，/check-in 主区
        # 异步渲染滞后）。用已登录的 auth_token 直接调签到接口兜底，避免把
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
