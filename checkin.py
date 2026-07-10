#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一签到调度器。

按三个正交维度组装 provider（见 providers/）：
- site_profile  ：站点适配器，newapi / sub2api（接口路径/响应/额度换算）
- auth_method   ：登录方式，access_token / cookie / browser / oauth
- checkin_action：签到方式，api / relogin / visit

配置：
- ACCOUNTS.json：统一保存站点配置、启用状态与凭据（新三维字段；旧 type+checkin_mode 自动迁移）
- sites.json：旧版站点配置文件，仅作为向后兼容补全来源

配置示例：
[
  { "name": "某 New API 站", "base_url": "https://example.com",
    "site_profile": "newapi", "auth_method": "cookie", "checkin_action": "api" },
  { "name": "Sub2API", "base_url": "https://sub.100xlabs.space",
    "site_profile": "sub2api", "auth_method": "access_token", "checkin_action": "api" }
]

运行：
    py checkin.py                 # 读 ACCOUNTS.json（兼容 sites.json 补全）
    py checkin.py --base-url ...  # 临时签到单个站点
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import sys
from pathlib import Path

import accounts_store
import providers
from mask_utils import sanitize_data
from providers.base import CheckinResult, SiteConfig

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "sites.json"
OK_STATUSES = {"success", "already_done"}


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return value
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def run_site_checkin(site: SiteConfig, turnstile: str = "") -> CheckinResult:
    """按 site 三维配置路由到 provider 执行签到。"""
    try:
        return providers.run_checkin(site, turnstile)
    except Exception as exc:  # provider 内部未捕获的异常兜底
        return CheckinResult(site.name, site.base_url, "error", f"签到任务异常：{exc}")


def load_sites(config_path: Path) -> list[SiteConfig]:
    raw_sites = accounts_store.load_unified_accounts(sites_path=config_path)

    sites: list[SiteConfig] = []
    for item in raw_sites:
        if not isinstance(item, dict):
            continue
        base_url = normalize_base_url(str(item.get("base_url") or item.get("url") or ""))
        if not base_url:
            continue
        site = accounts_store.site_config_from_mapping(
            item,
            overrides={
                "name": str(item.get("name") or base_url),
                "base_url": base_url,
                "enabled": accounts_store.parse_enabled(item.get("enabled"), True),
                "proxy": str(item.get("proxy") or "").strip()
                or os.environ.get("CHECKIN_PROXY", "").strip(),
            },
        )
        sites.append(site)
    return sites


def run_sites(sites: list[SiteConfig], turnstile: str = "", workers: int = 0) -> list[CheckinResult]:
    enabled_sites = [site for site in sites if site.enabled]
    if not enabled_sites:
        return []

    max_workers = workers if workers > 0 else min(8, len(enabled_sites))
    results: list[CheckinResult | None] = [None] * len(enabled_sites)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(run_site_checkin, site, turnstile): index
            for index, site in enumerate(enabled_sites)
        }
        for future in concurrent.futures.as_completed(future_map):
            index = future_map[future]
            site = enabled_sites[index]
            try:
                results[index] = future.result()
            except Exception as exc:
                results[index] = CheckinResult(site.name, site.base_url, "error", f"签到任务异常：{exc}")

    return [result for result in results if result is not None]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统一签到调度器（profile × auth × action）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help=f"站点配置文件，默认：{DEFAULT_CONFIG_PATH}")
    parser.add_argument("--base-url", default="", help="临时签到单个站点地址，例如：https://example.com")
    parser.add_argument("--site-profile", default="newapi", help="站点适配器：newapi / sub2api（默认 newapi）")
    parser.add_argument("--auth-method", default="", help="登录方式：access_token / cookie / browser / oauth（留空自动推断）")
    parser.add_argument("--checkin-action", default="api", choices=["api", "relogin", "visit", "browser_script"], help="签到方式：api=调接口，relogin=浏览器重登，visit=访问保活，browser_script=自定义浏览器脚本")
    parser.add_argument("--script", default="", help="browser_script 的仓库内相对 Python 脚本路径")
    parser.add_argument("--script-args", default="{}", help="browser_script 的脚本参数 JSON 字符串")
    parser.add_argument("--script-timeout", type=int, default=120, help="browser_script 超时秒数，默认 120")
    parser.add_argument("--api-variant", default="auto", choices=["auto", "legacy"], help="newapi+api 接口变体偏好：auto=challenge 优先，legacy=旧接口优先")
    parser.add_argument("--token-file", default="", help="临时指定单站点凭证文件（newapi）：第一行 Cookie，第二行用户 ID，第三行 Access token")
    parser.add_argument("--cookie", default="", help="临时指定单站点 Cookie")
    parser.add_argument("--access-token", default="", help="临时指定单站点 Access token")
    parser.add_argument("--user-id", default="", help="临时指定单站点用户 ID（newapi 的 New-Api-User）")
    parser.add_argument("--name", default="", help="临时指定单站点名称")
    parser.add_argument("--browser-profile", default=".browser_profile", help="browser 登录方式的浏览器持久化登录态目录前缀")
    parser.add_argument("--login-selector", default="", help="旧兼容字段：OAuth 登录入口选择器（当前 relogin 不再使用）")
    parser.add_argument("--oauth-provider", default="linuxdo", choices=accounts_store.KNOWN_OAUTH_PROVIDERS, help="OAuth 提供商：linuxdo / github")
    parser.add_argument("--oauth-account", default=accounts_store.DEFAULT_OAUTH_ACCOUNT, help="OAuth 账号名（同一 provider 下多账号，默认 default）")
    parser.add_argument("--proxy", default="", help="代理 URL（HTTP API 支持 http/https；浏览器流程可使用 socks5）")
    parser.add_argument("--turnstile", default="", help="如站点要求 Turnstile，可临时传入验证值")
    parser.add_argument("--workers", type=int, default=0, help="同时执行的最大任务数，默认最多 8 个")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _infer_auth_method(args: argparse.Namespace) -> str:
    """命令行未显式指定 auth_method 时按规则推断。"""
    if args.auth_method:
        return providers.normalize_auth_method(args.auth_method)
    if args.checkin_action in {"relogin", "browser_script"}:
        return "oauth"
    if args.access_token or os.environ.get("CHECKIN_ACCESS_TOKEN", ""):
        return "access_token"
    return "cookie"


def _result_payload(result: CheckinResult) -> dict[str, object]:
    return sanitize_data(result.__dict__)


def _execute(args: argparse.Namespace) -> tuple[dict[str, object] | list[dict[str, object]], int]:
    try:
        script_args = json.loads(args.script_args or "{}")
        if not isinstance(script_args, dict):
            raise ValueError("--script-args 必须是 JSON 对象")
    except Exception as exc:
        result = CheckinResult("checkin", "", "need_config", f"解析 --script-args 失败：{exc}")
        return _result_payload(result), 2

    if args.base_url:
        raw_site = {
            "name": args.name or args.base_url,
            "base_url": args.base_url,
            "site_profile": providers.normalize_profile(args.site_profile),
            "auth_method": _infer_auth_method(args),
            "checkin_action": providers.normalize_action(args.checkin_action),
            "script": args.script,
            "script_args": script_args,
            "script_timeout": args.script_timeout,
            "api_variant": args.api_variant,
            "cookie": args.cookie or os.environ.get("CHECKIN_COOKIE", ""),
            "user_id": args.user_id or os.environ.get("CHECKIN_USER_ID", ""),
            "access_token": args.access_token or os.environ.get("CHECKIN_ACCESS_TOKEN", ""),
            "cookie_file": args.token_file,
            "browser_profile": args.browser_profile,
            "login_selector": args.login_selector,
            "oauth_provider": args.oauth_provider,
            "oauth_account": args.oauth_account,
            "browser_state": os.environ.get("CHECKIN_BROWSER_STATE", ""),
            "proxy": args.proxy or os.environ.get("CHECKIN_PROXY", ""),
        }
        sites = [accounts_store.site_config_from_mapping(raw_site)]
    else:
        config_path = Path(args.config).resolve()
        try:
            sites = load_sites(config_path)
        except Exception as exc:
            result = CheckinResult("checkin", "", "error", f"读取配置失败：{exc}")
            return _result_payload(result), 2
        if not sites:
            result = CheckinResult("checkin", "", "need_config", f"未找到站点配置，请创建 {config_path}")
            return _result_payload(result), 0

    results = run_sites(sites, args.turnstile, args.workers)
    payloads = [_result_payload(result) for result in results]
    code = 0 if all(result.status in OK_STATUSES for result in results) else 2
    if args.worker:
        if len(payloads) != 1:
            result = CheckinResult("checkin", "", "error", f"worker 模式要求且仅允许一个站点，实际为 {len(payloads)} 个")
            return _result_payload(result), 2
        return payloads[0], code
    return payloads, code


def main() -> int:
    args = parse_args()
    # worker stdout 是机器协议通道；所有诊断输出都重定向到 stderr。
    stream = contextlib.redirect_stdout(sys.stderr) if args.worker else contextlib.nullcontext()
    with stream:
        payload, code = _execute(args)
    if args.worker:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
