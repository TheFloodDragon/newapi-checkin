#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图形验证码识别。目前收录两套彼此独立的方案：

| 模块 | 目标站点 | 形态 | 是否接入签到链路 |
|---|---|---|---|
| `generator` + `preprocess` + `matcher` + `predictor` | randomtool.cn | 300×100，Arial Bold 40px，逐字符旋转 | 否，离线工具 |
| `newapi_bitmap` | New API fork 的签到验证码（jianzhile 系） | 160×58，6×9 点阵 2 倍放大，按色分割 | 是，见 providers/profiles/newapi.py |

两套刻意不共用模板：字体、尺寸、变形维度完全不同，实测把 randomtool 的 Arial
模板用在 New API 验证码上只有 58% 单字符 / 约 7% 整图准确率。

与 `browser/turnstile.py` 处理的 Cloudflare Turnstile（交互式行为验证）也是
两类不同问题，不共用代码。

算法逆向记录见 docs/captcha_algorithm.md。

运行期依赖只有 numpy；pillow 仅在生成样本/构建模板库/解码 PNG 时需要（dev 依赖）。
"""

from __future__ import annotations

__all__ = ["CHARSETS", "Predictor", "newapi_bitmap", "predict", "predict_bytes"]

# 字符集常量从 generator 复用，但不在导入期拉起 pillow：generator 内部对
# pillow 做了延迟导入，模块级只有纯常量与布局计算。
from .generator import CHARSETS, char_positions  # noqa: F401


def __getattr__(name: str):
    """延迟导出：避免 import captcha_ocr 时就加载模板库或点阵字模表。

    子模块必须走 importlib.import_module：写 `from . import newapi_bitmap` 会再次
    命中本函数（`from pkg import sub` 先试 getattr），造成无限递归。
    """
    import importlib

    if name in ("Predictor", "predict", "predict_bytes"):
        return getattr(importlib.import_module(".predictor", __name__), name)
    if name in ("newapi_bitmap", "collect", "generator", "matcher", "preprocess", "predictor"):
        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
