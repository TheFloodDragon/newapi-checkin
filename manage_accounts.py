#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公益站 & 账号管理 GUI 入口（实现已迁入 gui/ 包，见 docs/OPTIMIZATION.md §三）。"""

from __future__ import annotations

import sys


def main() -> int:
    try:
        from gui.app import main as run
    except ModuleNotFoundError as exc:  # pragma: no cover - 运行期依赖提示
        if getattr(exc, "name", "").startswith("PySide6"):
            print("PySide6 is not installed. Install it with: uv sync --extra gui", file=sys.stderr)
            raise SystemExit(1) from exc
        raise
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
