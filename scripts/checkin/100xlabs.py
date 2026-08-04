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
    """执行百倍签到；共享流程由 _sub2api_common 统一维护。"""
    return await common.run_checkin_flow(page, context, site, helpers, SPEC)
