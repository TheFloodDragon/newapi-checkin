#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离屏渲染新 GUI 的开发预览：生成 <缓存目录>/ui-preview-{dark,light}.png。

使用临时目录中的演示数据，不读写真实 ACCOUNTS.json / 运行期缓存。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import accounts_store  # noqa: E402

# ── 演示数据（写入临时目录并劫持 store 路径）─────────────────────────────────
tmp = Path(tempfile.mkdtemp(prefix="gui-demo-"))
(tmp / accounts_store.RESULTS_DIR_NAME).mkdir()
demo = {
    "accounts": [
        {
            "name": "AnyRouter",
            "base_url": "https://anyrouter.top",
            "site_profile": "newapi",
            "auth_method": "oauth",
            "checkin_action": "relogin",
            "oauth_provider": "linuxdo",
            "oauth_account": "default",
            "enabled": True,
        },
        {
            "name": "示例中转站 A",
            "base_url": "https://relay-a.example.com",
            "site_profile": "newapi",
            "auth_method": "cookie",
            "checkin_action": "api",
            "cookie": "session=demo",
            "user_id": "1024",
            "enabled": True,
        },
        {
            "name": "Sub2API 演示",
            "base_url": "https://sub2.example.com",
            "site_profile": "sub2api",
            "auth_method": "access_token",
            "checkin_action": "api",
            "access_token": "demo-token",
            "oauth_fallback_provider": "linuxdo",
            "oauth_fallback_account": "default",
            "enabled": True,
        },
        {
            "name": "100xLabs 脚本站",
            "base_url": "https://100x.example.com",
            "site_profile": "newapi",
            "auth_method": "browser",
            "checkin_action": "browser_script",
            "script": "scripts/checkin/100xlabs.py",
            "script_args": {"checkin_text": "签到"},
            "enabled": True,
        },
        {
            "name": "保活监控站",
            "base_url": "https://keepalive.example.com",
            "site_profile": "newapi",
            "auth_method": "cookie",
            "checkin_action": "visit",
            "enabled": False,
        },
    ],
    "oauth_states": {"linuxdo": {"accounts": {"default": {"state": "x" * 2048, "username": "demo-user"}}}},
}
(tmp / "ACCOUNTS.json").write_text(json.dumps(demo, ensure_ascii=False, indent=2), encoding="utf-8")
(tmp / accounts_store.RESULTS_DIR_NAME / "checkin_result.json").write_text(
    json.dumps(
        {
            "generated_at": "2026-07-27T08:00:00",
            "results": [
                {"site": "AnyRouter", "base_url": "https://anyrouter.top", "status": "success", "current_quota": "$246.1"},
                {"site": "示例中转站 A", "base_url": "https://relay-a.example.com", "status": "success", "current_quota": "$12.5"},
                {"site": "Sub2API 演示", "base_url": "https://sub2.example.com", "status": "need_login", "current_quota": "$9.9"},
                {"site": "100xLabs 脚本站", "base_url": "https://100x.example.com", "status": "already_done", "current_quota": "$3.2"},
            ],
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)

accounts_store.SCRIPT_DIR = tmp
accounts_store.ACCOUNTS_PATH = tmp / "ACCOUNTS.json"
accounts_store.SITES_CONFIG_PATH = tmp / "sites.json"

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import theme  # noqa: E402
from gui.app import App  # noqa: E402

theme.save_theme("dark")
theme.save_pref("log_visible", True)

app = QApplication(sys.argv)
win = App()
win.resize(1280, 840)
win.show()
QTest.qWait(300)

out_dir = REPO / accounts_store.RESULTS_DIR_NAME
out_dir.mkdir(exist_ok=True)

# 冒烟断言
assert len(win.rows) == 5, f"rows={len(win.rows)}"
assert win.chip_sites.value.text() == "4/5", win.chip_sites.value.text()
assert win.chip_done.value.text() == "3", win.chip_done.value.text()
assert win.chip_failed.value.text() == "1", win.chip_failed.value.text()

# 选中 relogin 行：OAuth 区可见、凭据禁用
win._select_real(0)
QTest.qWait(100)
assert win.oauth_provider_wrap.isVisible() and not win.token_edit.isEnabled()

win.grab().save(str(out_dir / "ui-preview-dark.png"))

# sub2api + token：可选 OAuth 兜底可见
win._select_real(2)
QTest.qWait(100)
assert win.oauth_fallback_wrap.isVisible() and win.token_edit.isEnabled()

# 搜索过滤
win.search_edit.setText("sub2")
QTest.qWait(300)
assert win.count.text() == "1/5", win.count.text()
win.search_edit.clear()
QTest.qWait(300)

# 浅色主题
win._toggle_theme()
win._select_real(3)  # browser_script：脚本字段可见
QTest.qWait(200)
assert win.script_wrap.isVisible()
win.grab().save(str(out_dir / "ui-preview-light.png"))

# 还原偏好，不污染真实使用
theme.save_theme("dark")
theme.save_pref("log_visible", False)
theme._settings().remove("geometry")

print("PREVIEW OK:", out_dir / "ui-preview-dark.png", out_dir / "ui-preview-light.png")
app.quit()
os._exit(0)
