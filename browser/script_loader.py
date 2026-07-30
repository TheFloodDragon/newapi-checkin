#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库内站点脚本（如 ``scripts/*.py``、``scripts/checkin/*.py``）的路径校验与模块加载。

与同包的 ``script_runner`` 分开：后者要 import playwright/camoufox（实测 +1.4s、
+600 个模块），而纯 HTTP 签到路径也需要加载站点脚本里的附加日常任务（如极速蹬的
每日答题），不该为此付浏览器依赖的代价。本模块只用标准库，``browser/__init__`` 又是
惰性导入，因此 ``from browser import script_loader`` 的成本可以忽略。

安全约束（两条路径共用同一份实现，避免只在一处收紧）：
- 只接受仓库内相对路径的 ``.py`` 文件；
- 拒绝 URL、绝对路径、``..``，并在解析后复核仍位于仓库目录内。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[1]


class ScriptLoadError(Exception):
    """脚本路径非法或模块加载失败。"""


def resolve_script_path(script_path: str) -> Path:
    """校验并解析仓库内相对脚本路径。"""
    raw = (script_path or "").strip().replace("\\", "/")
    if not raw:
        raise ScriptLoadError("未配置 browser_script 脚本路径")
    parsed = urlparse(raw)
    if parsed.scheme or raw.startswith("//"):
        raise ScriptLoadError("脚本路径必须是仓库内相对路径，不能是 URL 或绝对路径")
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts:
        raise ScriptLoadError("脚本路径必须是仓库内相对路径，不能使用绝对路径或 ..")
    resolved = (REPO_ROOT / path).resolve()
    try:
        resolved.relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise ScriptLoadError("脚本路径超出仓库目录") from exc
    if not resolved.exists() or not resolved.is_file():
        raise ScriptLoadError(f"脚本文件不存在：{raw}")
    if resolved.suffix.lower() != ".py":
        raise ScriptLoadError("browser_script 只支持 Python 脚本文件（.py）")
    return resolved


def relative_script_path(script_file: Path) -> str:
    """仓库内相对路径（正斜杠），用于写进结果 detail。"""
    return str(Path(script_file).relative_to(REPO_ROOT)).replace("\\", "/")


def load_script_module(script_file: Path) -> ModuleType:
    """加载脚本模块。

    每次都重新执行、不复用 ``sys.modules`` 缓存：脚本文件在两次运行之间可能被修改，
    命中旧缓存会静默执行过期代码。
    """
    module_name = f"checkin_site_script_{abs(hash(str(script_file)))}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, script_file)
    if spec is None or spec.loader is None:
        raise ScriptLoadError(f"无法加载脚本：{script_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # 加载失败时清除半初始化模块，避免污染后续加载。
        sys.modules.pop(module_name, None)
        raise
    return module


def load_site_script(script_path: str) -> ModuleType:
    """校验路径并加载站点脚本模块。"""
    return load_script_module(resolve_script_path(script_path))


__all__ = [
    "REPO_ROOT",
    "ScriptLoadError",
    "load_script_module",
    "load_site_script",
    "relative_script_path",
    "resolve_script_path",
]
