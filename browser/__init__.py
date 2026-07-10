#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser —— 浏览器自动化功能簇。

把「登录即发额度」类站点（如 AgentRouter）所需的浏览器逻辑集中到此子包：
- state    ：登录态编码（encode_state）/ 解码（decode_state），跨平台 storage_state（base64(gzip(json))）；
- session  ：浏览器会话共享层（capture_login / capture_oauth_state / verify_state / run_oauth_checkin），CLI 与 GUI 复用；
- poc_oauth：命令行入口（setup / run），供本地首次登录与验证；
- collector.js：F12 控制台凭据采集脚本（newapi / sub2api）。

外部用法：
    from browser import session, state
    from browser.session import BrowserSessionError
"""

from __future__ import annotations

from . import popups, session, state

__all__ = ["popups", "session", "state"]
