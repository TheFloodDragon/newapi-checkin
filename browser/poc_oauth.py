#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器自动 OAuth 重登工具（CLI）。

核心逻辑已抽取到 browser/session.py（async），供本 CLI 与 manage_accounts.py GUI 复用。
本文件保留命令行入口（在 checkin/ 目录下运行）：

    # 1) 首次：有头浏览器人工登录第三方（Linux.do / GitHub 等），登录态持久化并打印 base64
    python browser/poc_oauth.py setup --oauth-account default

    # 2) 验证：无头浏览器自动发起一次 OAuth，观察额度变化 / 是否需要人工
    python browser/poc_oauth.py run

可选参数：
    --base-url https://agentrouter.org   目标站点（默认 agentrouter.org）
    --user-id 68124                      真实 user id，作为 New-Api-User 兜底
    --proxy http://user:pass@host:port   代理 URL
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# 以包方式导入 browser.session（session 内部用相对导入引用 state），
# 因此把 checkin/（即 browser 包的父目录）加入 sys.path。
CHECKIN_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CHECKIN_DIR))

from browser import session as browser_session  # noqa: E402  (需先 sys.path.insert)
from browser.session import BrowserSessionError  # noqa: E402


def _log(msg: str) -> None:
    print(f"  {msg}")


async def cmd_setup_async(args) -> int:
    """有头浏览器：人工登录第三方 OAuth，登录态持久化并打印 base64（异步）。"""
    print("即将打开有头浏览器。请在浏览器里完成以下操作：")
    print(f"  1. 登录第三方 OAuth 提供商：{args.oauth_provider}（账号名：{args.oauth_account}）")
    print("  2. 完成可能出现的 Cloudflare / 邮箱 / 2FA 验证")
    print("  3. 确认第三方站点已登录后，回到本终端按回车关闭浏览器")
    print()

    async def wait_for_close() -> None:
        print("浏览器已打开。完成登录后回到此终端按回车…")
        # asyncio 环境下用 run_in_executor 阻塞等待 input
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, input)

    try:
        res = await browser_session.capture_oauth_state(
            oauth_provider=args.oauth_provider,
            proxy=args.proxy,
            log=_log,
            wait_for_close=wait_for_close,
        )
    except BrowserSessionError as exc:
        print(f"\n[错误] {exc}")
        return 1

    if not res["ok"]:
        print(f"\n[警告] {res['message']}")
        return 0

    text = res["state"]
    size = len(text)
    pct = size / (64 * 1024) * 100
    print(f"\nsetup 完成且 {res['provider']} 登录态有效。")
    print(f"[导出] 登录态 base64 共 {size} 字符（GitHub Secret 上限 64KB，占 {pct:.0f}%）")
    print("\n" + "=" * 64)
    print(f"把下面这段填入 ACCOUNTS.json 顶层 oauth_states.{res['provider']}.accounts.{args.oauth_account}.state：")
    print("=" * 64)
    print(text)
    print("=" * 64)
    return 0


def cmd_setup(args) -> int:
    """同步桥接 setup。"""
    return browser_session.run_sync(cmd_setup_async(args))


async def cmd_run_async(args) -> int:
    """无头浏览器：复用登录态自动发起一次 OAuth，观察额度变化（异步）。"""
    try:
        res = await browser_session.run_oauth_checkin(
            args.base_url,
            browser_state_text=os.environ.get("CHECKIN_BROWSER_STATE", ""),
            oauth_provider=args.oauth_provider,
            fallback_uid=args.user_id,
            proxy=args.proxy,
            log=_log,
        )
    except BrowserSessionError as exc:
        print(f"[错误] {exc}")
        return 2

    print("\n" + "=" * 56)
    print("OAuth 重登结论：")
    print(f"  OAuth 前额度：{res['quota_before']}")
    print(f"  OAuth 后额度：{res['quota_after']}")
    icon = {"success": "✅", "already_done": "◻️", "need_login": "❌", "need_verification": "⚠️"}.get(res["status"], "·")
    print(f"  {icon} [{res['status']}] {res['message']}")
    print("=" * 56)
    return 0


def cmd_run(args) -> int:
    """同步桥接 run。"""
    return browser_session.run_sync(cmd_run_async(args))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="浏览器自动 OAuth 重登工具")
    parser.add_argument("mode", choices=["setup", "run"], help="setup=人工首次登录；run=自动验证")
    parser.add_argument("--base-url", default="https://agentrouter.org", help="目标站点（run 模式使用）")
    parser.add_argument("--oauth-provider", default="linuxdo", choices=["linuxdo", "github"], help="第三方 OAuth 提供商")
    parser.add_argument("--oauth-account", default="default", help="OAuth 账号名（同一 provider 下多账号，默认 default）")
    parser.add_argument("--user-id", default="68124", help="真实 user id，作为 New-Api-User 兜底（默认 AgentRouter 的 68124）")
    parser.add_argument("--proxy", default="", help="代理 URL（如 http://user:pass@host:port）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.base_url = args.base_url.rstrip("/")
    if args.mode == "setup":
        return cmd_setup(args)
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())
