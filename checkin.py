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
import contextlib
import json
import os
import sys
from pathlib import Path

import accounts_store
import providers
from checkin_core.batch import run_serial_groups
from checkin_core.enums import (
    API_VARIANT_VALUES,
    DEFAULT_API_VARIANT,
    OK_STATUSES,
    VERIFICATION_MODE_VALUES,
)
from config import Timeouts
from mask_utils import sanitize_data
from providers.base import CheckinResult, SiteConfig

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "sites.json"


# base_url 归一化唯一实现在 accounts_store；保留模块级别名兼容既有引用。
normalize_base_url = accounts_store.normalize_base_url


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
    """同站账号串行、不同站点并发执行，结果保持配置顺序。"""
    enabled_sites = [site for site in sites if site.enabled]
    return run_serial_groups(
        enabled_sites,
        key=lambda site: normalize_base_url(site.base_url),
        execute=lambda site: run_site_checkin(site, turnstile),
        on_error=lambda site, exc: CheckinResult(site.name, site.base_url, "error", f"签到任务异常：{exc}"),
        workers=workers,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="统一签到调度器（profile × auth × action）")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help=f"站点配置文件，默认：{DEFAULT_CONFIG_PATH}")
    parser.add_argument("--base-url", default="", help="临时签到单个站点地址，例如：https://example.com")
    parser.add_argument("--site-profile", default="newapi", help="站点适配器：newapi / sub2api（默认 newapi）")
    parser.add_argument("--auth-method", default="", help="登录方式：access_token / cookie / browser / oauth（留空自动推断）")
    parser.add_argument("--checkin-action", default="api", choices=["api", "relogin", "visit", "browser_script"], help="签到方式：api=调接口，relogin=浏览器重登，visit=访问保活，browser_script=自定义浏览器脚本")
    parser.add_argument("--script", default="", help="站点脚本的仓库内相对 Python 路径（api 可选，browser_script 必填）")
    # script_args 可能含账号密码等凭据，优先从环境变量 CHECKIN_SCRIPT_ARGS 读取，
    # 避免出现在进程命令行（argv 对同机其它用户可见）。此选项仅供手工调试使用。
    parser.add_argument("--script-args", default="", help="browser_script 的脚本参数 JSON 字符串（含凭据时请改用 CHECKIN_SCRIPT_ARGS 环境变量）")
    parser.add_argument("--script-timeout", type=int, default=Timeouts.BROWSER_SCRIPT_DEFAULT,
                        help=f"browser_script 超时秒数，默认 {Timeouts.BROWSER_SCRIPT_DEFAULT}")
    parser.add_argument(
        "--api-variant",
        default=DEFAULT_API_VARIANT,
        choices=API_VARIANT_VALUES,
        help=(
            "newapi+api 接口变体偏好：legacy=旧接口优先（默认，站点提示流程已升级时自动切 challenge），"
            "auto=challenge 优先（需 Node.js）"
        ),
    )
    parser.add_argument(
        "--verification-mode",
        default="auto",
        choices=VERIFICATION_MODE_VALUES,
        help="newapi+api 验证机制偏好；auto 自动探测，其它值优先尝试后在不适用时回落自动分流",
    )
    parser.add_argument("--token-file", default="", help="临时指定单站点凭证文件（newapi）：第一行 Cookie，第二行用户 ID，第三行 Access token")
    parser.add_argument("--cookie", default="", help="临时指定单站点 Cookie")
    parser.add_argument("--access-token", default="", help="临时指定单站点 Access token")
    parser.add_argument("--user-id", default="", help="临时指定单站点用户 ID（newapi 的 New-Api-User）")
    parser.add_argument("--name", default="", help="临时指定单站点名称")
    parser.add_argument("--browser-profile", default=".browser_profile", help="browser 登录方式的浏览器持久化登录态目录前缀")
    parser.add_argument("--login-selector", default="", help="旧兼容字段：OAuth 登录入口选择器（当前 relogin 不再使用）")
    parser.add_argument("--oauth-provider", default="linuxdo", choices=accounts_store.KNOWN_OAUTH_PROVIDERS, help="OAuth 提供商：linuxdo / github")
    parser.add_argument("--oauth-account", default=accounts_store.DEFAULT_OAUTH_ACCOUNT, help="OAuth 账号名（同一 provider 下多账号，默认 default）")
    parser.add_argument("--oauth-fallback-provider", default="", choices=("", *accounts_store.KNOWN_OAUTH_PROVIDERS), help=argparse.SUPPRESS)
    parser.add_argument("--oauth-fallback-account", default=accounts_store.DEFAULT_OAUTH_ACCOUNT, help=argparse.SUPPRESS)
    parser.add_argument("--proxy", default="", help="代理 URL（HTTP API 支持 http/https；浏览器流程可使用 socks5）")
    parser.add_argument("--referer-path", default="/profile", help="newapi 请求头 Referer 的路径部分，默认 /profile")
    parser.add_argument(
        "--no-verify-ssl",
        dest="verify_ssl",
        action="store_false",
        default=True,
        help="跳过 TLS 证书与主机名校验（仅用于证书过期/自签名站点的应急兜底，有中间人风险）",
    )
    parser.add_argument(
        "--no-auto-refresh-cookie",
        dest="auto_refresh_cookie",
        action="store_false",
        default=True,
        help="禁止把去重后的 Cookie 回写凭据文件（内存中仍会去重）",
    )
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


def _load_script_args(args: argparse.Namespace) -> dict[str, object]:
    """解析 browser_script 参数；优先环境变量，避免凭据出现在命令行。

    script_args 可能含站点账号密码（见 scripts/checkin/*.py 的登录兜底），
    放进 argv 会泄露到同机进程列表，因此批量调度改用 CHECKIN_SCRIPT_ARGS 传递。
    --script-args 仅作为人工调试的兼容入口。
    """
    raw = os.environ.get("CHECKIN_SCRIPT_ARGS", "").strip() or (args.script_args or "{}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("script_args 必须是 JSON 对象")
    return parsed


def _explicit_credential_fields(args: argparse.Namespace) -> set[str]:
    """判断单站入口中哪些凭据是调用方显式提供的（空值也算）。"""
    fields: set[str] = set()
    if "--access-token" in sys.argv or "CHECKIN_ACCESS_TOKEN" in os.environ:
        fields.add("access_token")
    if "CHECKIN_REFRESH_TOKEN" in os.environ:
        fields.add("refresh_token")
    if "CHECKIN_BROWSER_STATE" in os.environ:
        fields.add("browser_state")
    return fields


def _stabilize_oauth_state_basis(site: SiteConfig) -> None:
    """让 OAuth 浏览器脚本站点的 state 缓存 basis 保持稳定，缓存才能被复用。

    OAuth 站点的 browser_state 是运行期产物：父进程注入共享 provider 登录态或上次
    缓存的本站会话，脚本结束后又把新的整体快照写回缓存。若 basis 仍按「本次注入值」
    计算，每轮的 basis 都不同，下一轮 resolve_cached_credentials 会判为过期缓存而
    忽略，表现为「刚续存的登录态永远用不上、每次都要重跑整段 OAuth」。

    这里把 state basis 固定为「该站点配置本身」（配置里通常为空），使
    脚本续存与下次读取使用同一基线；配置真的填了 browser_state 时行为不变。

    只有 ``CHECKIN_CONFIGURED_BROWSER_STATE`` 确实存在时才改写 basis：该变量由父进程
    按站点配置导出，是「配置值」的唯一可信来源。直接用 CLI 跑单站时它不存在，此时
    site.browser_state 就是用户显式传入的配置值，构造期算出的 basis 已经正确；若把
    缺失当成空串强行改写，写出的 basis 会与读取侧永不相等，反而制造出本函数要防的
    「续存的登录态永远用不上」。
    """
    from providers import token_cache

    if (site.auth_method or "").strip().lower() != "oauth":
        return
    if (site.checkin_action or "").strip().lower() != "browser_script":
        return
    context = getattr(site, "runtime_credentials", None)
    if context is None:
        return
    configured_state = os.environ.get("CHECKIN_CONFIGURED_BROWSER_STATE")
    if configured_state is None:
        return
    context.state_basis = token_cache.credential_basis(browser_state=configured_state, group="state")


def _execute(args: argparse.Namespace) -> tuple[dict[str, object] | list[dict[str, object]], int]:
    try:
        script_args = _load_script_args(args)
    except Exception as exc:
        result = CheckinResult("checkin", "", "need_config", f"解析 script_args 失败：{exc}")
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
            "verification_mode": args.verification_mode,
            "cookie": args.cookie or os.environ.get("CHECKIN_COOKIE", ""),
            "user_id": args.user_id or os.environ.get("CHECKIN_USER_ID", ""),
            "access_token": args.access_token or os.environ.get("CHECKIN_ACCESS_TOKEN", ""),
            # refresh_token 只从环境变量读（属凭据，不设命令行选项）：sub2api 用它
            # 在 access_token 过期时纯 HTTP 续期，避免为此拉起浏览器。
            "refresh_token": os.environ.get("CHECKIN_REFRESH_TOKEN", ""),
            "cookie_file": args.token_file,
            "browser_profile": args.browser_profile,
            "login_selector": args.login_selector,
            "oauth_provider": args.oauth_provider,
            "oauth_account": args.oauth_account,
            "oauth_fallback_provider": args.oauth_fallback_provider,
            "oauth_fallback_account": args.oauth_fallback_account,
            "browser_state": os.environ.get("CHECKIN_BROWSER_STATE", ""),
            "proxy": args.proxy or os.environ.get("CHECKIN_PROXY", ""),
            # 以下三项此前只在读配置文件的路径生效；worker 模式（--base-url）缺失会
            # 静默回落默认值，导致同一份 ACCOUNTS.json 在 GUI 与 CI 表现不一致。
            "verify_ssl": args.verify_ssl,
            "referer_path": args.referer_path,
            "auto_refresh_cookie": args.auto_refresh_cookie,
        }
        sites = [
            accounts_store.runtime_site_from_mapping(
                raw_site,
                explicit_fields=_explicit_credential_fields(args),
                cache_policy=os.environ.get("CHECKIN_CACHE_POLICY", "compatible"),
            )
        ]
        _stabilize_oauth_state_basis(sites[0])
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
