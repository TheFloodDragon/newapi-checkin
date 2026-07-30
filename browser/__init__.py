#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser —— 浏览器自动化功能簇。

把「登录即发额度」类站点（如 AgentRouter）所需的浏览器逻辑集中到此子包：
- state        ：登录态编码（encode_state）/ 解码（decode_state），跨平台 storage_state（base64(gzip(json))）；
- session      ：浏览器会话共享层（capture_login / capture_oauth_state / verify_state / run_oauth_checkin），CLI 与 GUI 复用；
- script_loader：站点脚本的路径校验与加载（不依赖 playwright）；
- poc_oauth    ：命令行入口（setup / run），供本地首次登录与验证；
- collector.js ：F12 控制台凭据采集脚本（newapi / sub2api）。

外部用法：
    from browser import session, state
    from browser.session import BrowserSessionError

子模块**惰性导入**（PEP 562）：以前这里 eager `from . import popups, session, state`，
于是 `from browser import <任何东西>` 都会连带拉起 playwright 与 camoufox
（实测 1.4s、763 个模块）。纯 HTTP 签到路径也要用到本包里与浏览器无关的部分
（script_loader、turnstile 的常量），不该为此付这份代价。
"""

from __future__ import annotations

_LAZY = (
    "bypass",
    "oauth_providers",
    "popups",
    "script_helpers",
    "script_loader",
    "script_runner",
    "session",
    "state",
    "turnstile",
)

__all__ = list(_LAZY)


def __getattr__(name: str):
    """按需导入子模块，保持 `import browser; browser.session` 的写法可用。"""
    if name in _LAZY:
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
