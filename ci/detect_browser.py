#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检测启用账号是否存在运行时可能启动浏览器的路径（CI 用）。

stdout 只打印 ``true`` / ``false``。检测条件必须与 Provider 真实降级路径一致：即使
主认证是 access_token，只要配置 OAuth fallback，token/refresh 失败后仍可能启动
Camoufox，因此也必须预装浏览器依赖。
"""

from __future__ import annotations

import sys
from typing import Any, Iterable

import accounts_store


def account_needs_browser(account: dict[str, Any]) -> bool:
    if not accounts_store.parse_enabled(account.get("enabled"), True):
        return False
    auth_method = str(account.get("auth_method") or "").strip().lower()
    checkin_action = str(account.get("checkin_action") or "").strip().lower()
    old_mode = str(account.get("checkin_mode") or account.get("mode") or "").strip().lower()
    fallback = accounts_store.normalize_oauth_provider(account.get("oauth_fallback_provider"))
    return bool(
        auth_method in {"browser", "oauth"}
        or checkin_action in {"relogin", "browser_script"}
        or old_mode == "browser_oauth"
        or fallback
    )


def needs_browser(accounts: Iterable[dict[str, Any]]) -> bool:
    return any(account_needs_browser(account) for account in accounts if isinstance(account, dict))


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("true" if needs_browser(accounts_store.load_unified_accounts()) else "false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
