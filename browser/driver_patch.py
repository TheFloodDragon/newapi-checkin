#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""修补 Playwright Firefox 驱动对缺失 pageError.location 的空指针崩溃。

问题（实测 playwright 1.61.0 + Camoufox 0.4.11，CI 与本机均可复现）：
Firefox 上报的未捕获页面错误并不总是带 location 字段（例如脚本被 CSP 阻断、
worker/module 内抛错、或错误来自已销毁的 frame）。驱动侧 coreBundle.js 直接读
``pageError.location.url``，于是在 Node 进程里抛出

    TypeError: Cannot read properties of undefined (reading 'url')
        at FFBrowserContext.<anonymous> (.../coreBundle.js:52908:39)

这是**驱动进程整体退出**，不是可捕获的 Python 异常。Python 侧只会看到后续
``Page.goto: Connection closed while reading from the driver``，浏览器流程直接
报废。实测把 AgentRouter 的 OAuth 重登打成 error。

为什么必须补驱动、而不是在 Python 侧兜住：
1. 崩溃发生在 Node 驱动进程，Python 端 try/except 无法拦；
2. 该上报路径由 Firefox 的 ``_onUncaughtError`` 主动触发，与我们是否注册
   ``pageerror`` 监听无关 —— 不监听同样崩；
3. 页面内 ``window.onerror`` 之类的吞错脚本晚于驱动上报，拦不住。

因此在启动浏览器前把 ``pageError.location.X`` 改写为 ``(pageError.location||{}).X``
的安全形式。补丁是幂等的纯文本替换，只在检测到未修补内容时写入。
"""

from __future__ import annotations

import re
from pathlib import Path

# 只替换这三个字段的读取；它们都在 pageError 上报路径上（tracing 与 dispatcher 各一处）。
_FIELD_DEFAULTS = {
    "url": '""',
    "lineNumber": "0",
    "columnNumber": "0",
}
_UNSAFE_RE = re.compile(r"pageError\.location\.(url|lineNumber|columnNumber)\b")


def driver_bundle_path() -> Path | None:
    """返回当前环境 Playwright 驱动的 coreBundle.js 路径；找不到返回 None。"""
    try:
        import playwright
    except Exception:
        return None
    root = Path(playwright.__file__).resolve().parent
    candidate = root / "driver" / "package" / "lib" / "coreBundle.js"
    return candidate if candidate.is_file() else None


def patch_firefox_page_error(bundle: Path | None = None) -> str:
    """就地修补驱动，返回结果状态。

    返回值：
    - ``"patched"``   ：本次完成修补；
    - ``"already"``   ：此前已修补，无需改动；
    - ``"unavailable"``：找不到驱动文件；
    - ``"failed"``    ：读写失败（权限/只读挂载等），调用方不应因此中断流程。
    """
    target = bundle or driver_bundle_path()
    if target is None or not target.is_file():
        return "unavailable"
    try:
        source = target.read_text(encoding="utf-8")
    except Exception:
        return "failed"

    if not _UNSAFE_RE.search(source):
        return "already"

    def _replace(match: re.Match[str]) -> str:
        field = match.group(1)
        return f"(pageError.location||{{}}).{field}||{_FIELD_DEFAULTS[field]}"

    # 包一层括号，避免 ``a||b`` 与外层表达式（如三元、逗号）结合出错。
    patched = _UNSAFE_RE.sub(lambda m: f"({_replace(m)})", source)
    try:
        target.write_text(patched, encoding="utf-8")
    except Exception:
        return "failed"
    return "patched"


__all__ = ["driver_bundle_path", "patch_firefox_page_error"]
