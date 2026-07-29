#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 模板匹配识别器（零训练，运行期仅需 numpy）。

为什么这里用模板法而不是 CNN：站点字体唯一（`bold 40px Arial`）、无缩放、无
形变，唯一的变化维度是 ±0.35 rad 旋转。这种「变化可穷举」的场景下，按角度档位
预生成模板再取最大余弦相似度，本身就是最优解 —— CNN 只会用更多资源去学同一件事。

模板库构建期需要 pillow（渲染字形），构建完序列化为 .npz 随包分发；
运行期只做一次矩阵乘法，不触碰 pillow。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .generator import CHARSET_COMPLEX, CHARSET_NUMBER, CHARSETS, MAX_ROTATION
from .preprocess import GRID_DIM, image_to_windows

# 模板库覆盖全部可能字符：complex(63) 与 number 的并集。
# number 含 '0'/'1'，而 alpha/mixed/complex 为避免混淆已剔除它们，故需并集。
ALL_CHARS = "".join(sorted(set(CHARSET_COMPLEX) | set(CHARSET_NUMBER)))

# 角度档位：站点旋转范围 ±0.35 rad，0.05 步进 → 15 档。
# 档位间距 0.05 rad ≈ 2.9°，在 24×24 网格上引起的像素位移不足 1px，
# 因此不必更密；若真实数据显示不足，细分到 0.025 是第一手段（见计划风险项）。
ANGLE_STEP = 0.05
# 用 round 而非 int：2*0.35/0.05 的浮点值是 13.999...，int() 截断会少一档，
# 导致 +0.30~+0.35 这段（约旋转范围的 7%）没有任何模板可匹配。
DEFAULT_ANGLES = tuple(
    round(-MAX_ROTATION + ANGLE_STEP * i, 4)
    for i in range(round(2 * MAX_ROTATION / ANGLE_STEP) + 1)
)

TEMPLATES_PATH = Path(__file__).resolve().parent / "templates.npz"


def _unit_rows(mat: np.ndarray) -> np.ndarray:
    """按行做 L2 归一化，零行保持为零（避免 0/0）。"""
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return mat / norms


class TemplateMatcher:
    """字符 × 角度 模板库 + 余弦相似度最近邻。"""

    def __init__(self, chars: str, angles: tuple[float, ...], templates: np.ndarray):
        self.chars = chars
        self.angles = angles
        # templates: (n_char, n_angle, GRID*GRID)，行已单位化
        self.templates = templates
        self._char_index = {ch: i for i, ch in enumerate(chars)}
        n_char, n_angle, dim = templates.shape
        # 展平成 (n_char*n_angle, dim) 供单次矩阵乘法
        self._flat = templates.reshape(n_char * n_angle, dim)
        self._n_angle = n_angle

    # ── 构建与序列化 ────────────────────────────────────────────────────────
    @classmethod
    def build(cls, chars: str = ALL_CHARS, angles: tuple[float, ...] = DEFAULT_ANGLES) -> TemplateMatcher:
        """渲染模板库（需要 pillow）。"""
        from .preprocess import render_char_grid

        dim = GRID_DIM
        out = np.zeros((len(chars), len(angles), dim), dtype=np.float32)
        for ci, ch in enumerate(chars):
            for ai, ang in enumerate(angles):
                out[ci, ai] = render_char_grid(ch, ang).reshape(-1)
        # 逐行单位化后存盘：运行期直接点积即为余弦相似度，省掉每次归一化
        flat = _unit_rows(out.reshape(-1, dim))
        return cls(chars, tuple(angles), flat.reshape(len(chars), len(angles), dim))

    def save(self, path: Path | str = TEMPLATES_PATH) -> Path:
        path = Path(path)
        np.savez_compressed(
            path,
            chars=np.array(list(self.chars)),
            angles=np.array(self.angles, dtype=np.float32),
            templates=self.templates,
        )
        return path

    @classmethod
    def load(cls, path: Path | str = TEMPLATES_PATH) -> TemplateMatcher:
        with np.load(path, allow_pickle=False) as z:
            chars = "".join(z["chars"].tolist())
            angles = tuple(float(a) for a in z["angles"])
            templates = z["templates"].astype(np.float32)
        return cls(chars, angles, templates)

    @classmethod
    def load_or_build(cls, path: Path | str = TEMPLATES_PATH) -> TemplateMatcher:
        path = Path(path)
        if path.exists():
            return cls.load(path)
        matcher = cls.build()
        matcher.save(path)
        return matcher

    # ── 识别 ───────────────────────────────────────────────────────────────
    def _allowed_mask(self, charset: str | None) -> np.ndarray | None:
        """把字符集约束转成字符维掩码。

        限定字符集能显著降低混淆：例如 number 场景下不必与 'S'/'Z' 竞争，
        '5'/'2' 的判定立刻变得无歧义。
        """
        if not charset:
            return None
        allowed = CHARSETS.get(charset, charset)
        mask = np.zeros(len(self.chars), dtype=bool)
        for ch in allowed:
            idx = self._char_index.get(ch)
            if idx is not None:
                mask[idx] = True
        return mask if mask.any() else None

    def predict_window(self, window: np.ndarray, charset: str | None = None) -> tuple[str, float, str]:
        """单字符窗口 → (字符, 置信度, 次优字符)。

        置信度取最优与次优的相似度之差（margin），比原始相似度更能反映
        「是否可信」：相似度 0.95 但次优 0.949 显然比 0.90 对 0.60 更危险。
        """
        vec = window.reshape(-1).astype(np.float32)
        n = float(np.linalg.norm(vec))
        if n == 0:
            return "", 0.0, ""
        scores = self._flat @ (vec / n)  # (n_char*n_angle,)
        per_char = scores.reshape(len(self.chars), self._n_angle).max(axis=1)
        mask = self._allowed_mask(charset)
        if mask is not None:
            per_char = np.where(mask, per_char, -np.inf)
        order = np.argsort(per_char)[::-1]
        best, second = int(order[0]), int(order[1])
        margin = float(per_char[best] - per_char[second])
        return self.chars[best], margin, self.chars[second]

    def predict_windows(
        self, windows: list[np.ndarray], charset: str | None = None
    ) -> tuple[str, list[float], list[str]]:
        chars, margins, seconds = [], [], []
        for win in windows:
            ch, margin, second = self.predict_window(win, charset)
            chars.append(ch)
            margins.append(margin)
            seconds.append(second)
        return "".join(chars), margins, seconds

    def predict_image(
        self, image, length: int, charset: str | None = None
    ) -> tuple[str, list[float], list[str]]:
        return self.predict_windows(image_to_windows(image, length), charset)


def build_and_save(path: Path | str = TEMPLATES_PATH) -> Path:
    """CLI/测试用的便捷入口。"""
    return TemplateMatcher.build().save(path)


__all__ = [
    "ALL_CHARS",
    "DEFAULT_ANGLES",
    "TEMPLATES_PATH",
    "TemplateMatcher",
    "build_and_save",
]
