#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检测 ACCOUNTS.json 是否存在启用的浏览器/OAuth 登录态任务（CI 用）。

向 stdout 打印 "true" 或 "false"，供 GitHub Actions 步骤捕获。
"""

from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import accounts_store

need = False
for acc in accounts_store.load_unified_accounts():
    enabled = accounts_store.parse_enabled(acc.get("enabled"), True)
    if not enabled:
        continue
    auth_method = str(acc.get("auth_method") or "").strip().lower()
    checkin_action = str(acc.get("checkin_action") or "").strip().lower()
    old_mode = str(acc.get("checkin_mode") or acc.get("mode") or "").strip().lower()
    if auth_method in {"browser", "oauth"} or checkin_action in {"relogin", "browser_script"} or old_mode == "browser_oauth":
        need = True
        break

print("true" if need else "false")
