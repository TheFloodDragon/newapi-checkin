# -*- coding: utf-8 -*-
"""预处理回归：窗口几何与归一化方式是准确率的两个命门。

这两处出错都不会抛异常，只会静默掉准确率：
- 窗口裁掉字形极值 → 合成集从 100% 掉到 70%
- 归一化裁包围盒再拉伸 → 抹掉绝对尺度，W/w、V/v、S/s 退化成同形
所以把它们的关键性质固化成断言。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

pytest.importorskip("PIL", reason="渲染字形需要 pillow（dev 依赖）")

from captcha_ocr import generator as G  # noqa: E402
from captcha_ocr import preprocess as P  # noqa: E402
from captcha_ocr.matcher import ALL_CHARS, DEFAULT_ANGLES  # noqa: E402


# ── 窗口必须覆盖字形真实极值 ──────────────────────────────────────────────────
def test_window_covers_measured_glyph_extents() -> None:
    """全字符 × 全角度实测极值必须落在取窗范围内。

    这是最重要的一条：一旦不满足，宽字形（W/@/$）会被裁掉，
    识别结果会大量落到 h/H/n 这类窄字形上。
    """
    lo_x, hi_x, lo_y, hi_y = P.measure_glyph_extents(ALL_CHARS, DEFAULT_ANGLES)
    assert P.WIN_LEFT <= lo_x, f"左边界不足：需要 <= {lo_x}"
    assert hi_x <= P.WIN_RIGHT, f"右边界不足：需要 >= {hi_x}"
    assert P.WIN_TOP <= lo_y, f"上边界不足：需要 <= {lo_y}"
    assert hi_y <= P.WIN_BOTTOM, f"下边界不足：需要 >= {hi_y}"


def test_window_is_wider_than_char_step() -> None:
    """窗口必然比步长宽 —— 最宽字形 37px > 步长 35px，邻字符重叠不可避免。

    这不是缺陷：模板同样取自定长窗，邻字符碎片位置可预期。实测剥离连通域
    反而更差（100% → 93%），所以不要「优化」掉这个重叠。
    """
    assert P.WIN_W > G.CHAR_STEP


# ── 归一化保留绝对尺度 ───────────────────────────────────────────────────────
def test_normalize_preserves_absolute_scale() -> None:
    """大写与小写经归一化后前景像素数应显著不同（站点无缩放，尺度是判别特征）。"""
    upper = P.render_char_grid("W")
    lower = P.render_char_grid("w")
    assert upper.shape == lower.shape == (P.GRID_H, P.GRID_W)
    # 若做包围盒拉伸，两者会被归一到同样大小、像素数接近；定比降采样下差异明显
    assert abs(int(upper.sum()) - int(lower.sum())) > 20


def test_normalize_output_is_binary_float() -> None:
    grid = P.render_char_grid("A", 0.1)
    assert grid.dtype == np.float32
    assert set(np.unique(grid)).issubset({0.0, 1.0})


def test_normalize_handles_empty_window() -> None:
    empty = np.zeros((P.WIN_H, P.WIN_W), dtype=np.uint8)
    out = P.normalize(empty)
    assert out.shape == (P.GRID_H, P.GRID_W)
    assert out.sum() == 0


# ── 切窗 ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("length", [4, 5, 6, 7, 8])
def test_crop_windows_count_and_shape(length: int) -> None:
    mask = P.binarize(P.to_gray(G.sample(length, "mixed", random.Random(2)).image))
    wins = P.crop_windows(mask, length)
    assert len(wins) == length
    for w in wins:
        assert w.shape == (P.WIN_H, P.WIN_W)


@pytest.mark.parametrize("length", [4, 6, 8])
def test_no_empty_windows(length: int) -> None:
    """每个窗口都必须含前景像素 —— 空窗意味着切分位置错了。"""
    rng = random.Random(11)
    for _ in range(12):
        mask = P.binarize(P.to_gray(G.sample(length, "complex", rng).image))
        for i, w in enumerate(P.crop_windows(mask, length)):
            assert w.sum() > 0, f"第 {i} 个窗口为空"


def test_binarize_threshold_matches_foreground_range() -> None:
    """阈值必须取字符色上界，否则会把干扰色也算成前景。"""
    assert P.FG_THRESHOLD == G.FG_RANGE[1]
    gray = np.array([[G.FG_RANGE[0], G.FG_RANGE[1], G.BG_RANGE[0], 255]], dtype=np.uint8)
    assert P.binarize(gray).tolist() == [[1, 1, 0, 0]]


def test_to_gray_accepts_rgb_array_and_pil_image() -> None:
    img = G.sample(6, "mixed", random.Random(5)).image
    from_pil = P.to_gray(img)
    from_arr = P.to_gray(np.asarray(img))
    assert from_pil.shape == from_arr.shape == (G.HEIGHT, G.WIDTH)
    # 两条路径的灰度权重一致，允许 1 灰阶的取整差异
    assert int(np.abs(from_pil.astype(int) - from_arr.astype(int)).max()) <= 1


def test_extract_pads_when_window_exceeds_canvas() -> None:
    """窗口越界时应补 0 而不是抛异常（n=8 时末字符窗口会贴到右边界）。"""
    mask = np.ones((G.HEIGHT, G.WIDTH), dtype=np.uint8)
    win = P._extract(mask, G.WIDTH - 5)
    assert win.shape == (P.WIN_H, P.WIN_W)
    assert win.sum() < P.WIN_H * P.WIN_W  # 越界部分为 0


def test_image_to_windows_is_normalized() -> None:
    s = G.sample(6, "mixed", random.Random(8))
    wins = P.image_to_windows(s.image, 6)
    assert len(wins) == 6
    for w in wins:
        assert w.shape == (P.GRID_H, P.GRID_W)
        assert w.dtype == np.float32
