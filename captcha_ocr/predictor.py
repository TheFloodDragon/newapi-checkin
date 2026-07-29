#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对外统一入口：从图片到文本。

只暴露一层薄封装，真正的逻辑分别在 preprocess（切窗归一化）与 matcher（模板匹配）。
这样做的目的是让调用方无需了解「窗口锚点」「角度档位」这些内部约定。

运行期依赖仅 numpy：模板库是预构建的 .npz，不触碰 pillow。
读取 PNG 需要 pillow 或调用方自己解码后传 ndarray。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .generator import CHARSETS, HEIGHT, WIDTH
from .matcher import TEMPLATES_PATH, TemplateMatcher
from .preprocess import binarize, crop_windows, normalize, to_gray

# 站点长度上限：char_positions 在 n>8 时 x 会超出 300px 画布（见 generator）。
MAX_LENGTH = 8


@dataclass(frozen=True)
class Prediction:
    """识别结果。

    margins 是每个字符「最优与次优模板的相似度差」。它比原始相似度更能反映
    可信度：0.95 对 0.949 显然比 0.90 对 0.60 更危险。调用方可用 min_margin
    决定是否重试或转人工。
    """

    text: str
    margins: tuple[float, ...]
    seconds: tuple[str, ...]  # 每位的次优候选，便于排查混淆

    @property
    def min_margin(self) -> float:
        return min(self.margins) if self.margins else 0.0

    def __str__(self) -> str:  # pragma: no cover - 便捷输出
        return self.text


class Predictor:
    """验证码识别器。模板库只加载一次，可复用同一实例批量识别。"""

    def __init__(self, templates: Path | str = TEMPLATES_PATH):
        self._matcher = TemplateMatcher.load_or_build(templates)

    # ── 内部 ───────────────────────────────────────────────────────────────
    @staticmethod
    def _check_length(length: int) -> None:
        if not 1 <= length <= MAX_LENGTH:
            raise ValueError(
                f"length 必须在 1..{MAX_LENGTH}：站点在 n>8 时字符 x 超出 {WIDTH}px 画布"
            )

    def _predict_mask(self, mask: np.ndarray, length: int, charset: str | None) -> Prediction:
        wins = [normalize(w) for w in crop_windows(mask, length)]
        text, margins, seconds = self._matcher.predict_windows(wins, charset)
        return Prediction(text=text, margins=tuple(margins), seconds=tuple(seconds))

    # ── 对外 ───────────────────────────────────────────────────────────────
    def predict_mask(self, mask: np.ndarray, length: int, charset: str | None = None) -> Prediction:
        """已二值化的 {0,1} 前景掩码 → 结果（采集脚本导出的就是这种形态）。"""
        self._check_length(length)
        if mask.shape != (HEIGHT, WIDTH):
            raise ValueError(f"掩码尺寸应为 {(HEIGHT, WIDTH)}，收到 {mask.shape}")
        return self._predict_mask(mask.astype(np.uint8), length, charset)

    def predict_array(self, array: np.ndarray, length: int, charset: str | None = None) -> Prediction:
        """RGB/灰度 ndarray → 结果。"""
        self._check_length(length)
        return self._predict_mask(binarize(to_gray(array)), length, charset)

    def predict(self, image, length: int = 6, charset: str | None = None) -> Prediction:
        """PIL.Image / 文件路径 / ndarray → 结果。

        length 默认 6（站点默认值）。charset 传 number/alpha/mixed/complex 可限定
        候选集合，能显著降低混淆（如 number 场景下 5/S、2/Z 不再互相竞争）。
        """
        self._check_length(length)
        if charset is not None and charset not in CHARSETS:
            raise ValueError(f"未知字符集 {charset!r}，可选：{sorted(CHARSETS)}")
        if isinstance(image, np.ndarray):
            return self.predict_array(image, length, charset)
        if isinstance(image, (str, Path)):
            image = _open_image(image)
        return self._predict_mask(binarize(to_gray(image)), length, charset)

    def predict_bytes(self, data: bytes, length: int = 6, charset: str | None = None) -> Prediction:
        """PNG/JPEG 字节流 → 结果（需要 pillow）。"""
        import io

        return self.predict(_open_image(io.BytesIO(data)), length, charset)


def _open_image(source):
    """打开图片；pillow 缺失时给出可操作提示。"""
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "读取图片文件需要 pillow。请执行 uv sync --extra dev，"
            "或自行解码后调用 predict_array/predict_mask（仅需 numpy）。"
        ) from exc
    return Image.open(source)


_DEFAULT: Predictor | None = None


def _default() -> Predictor:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Predictor()
    return _DEFAULT


def predict(image, length: int = 6, charset: str | None = None) -> Prediction:
    """模块级便捷函数，复用同一个默认 Predictor（避免反复加载模板库）。"""
    return _default().predict(image, length, charset)


def predict_bytes(data: bytes, length: int = 6, charset: str | None = None) -> Prediction:
    return _default().predict_bytes(data, length, charset)


__all__ = ["MAX_LENGTH", "Prediction", "Predictor", "predict", "predict_bytes"]
