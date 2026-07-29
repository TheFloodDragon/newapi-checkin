#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预处理：整图 → 单字符归一化位图。

L1（模板匹配）与 L2（CNN）共用这一条管线，保证训练与推理的输入形态一致 ——
两侧若各写一份，任何细微差异都会直接变成准确率损失，且极难定位。

管线：RGB → 灰度 → 阈值二值化 → 按 char_positions 切定长窗 → 定比降采样

两个关键设计（都是实测定下来的，不是拍的）：

1. 窗口必须覆盖字形的**真实极值**。字形相对锚点 (x, ANCHOR_Y) 的极值由
   全部 65 字符 × 15 角度实测得出：dx ∈ [-9, 40]，dy ∈ [-27, 30]。
   早先按 40×48 取窗会裁掉 W/@/$ 等宽高字形，单字符准确率直接从 100% 掉到 70%。
   注意 dx 上界 40 > 字符步长 35，**窗口必然与邻字符重叠**，这是站点算法本身
   的性质（最宽字形 37px > 步长 35px），无法靠调窗口消除。
   dy 相对锚点近似上下对称，因为锚点是 em 盒中心（textBaseline="middle"）而非基线。

2. 归一化**不做包围盒裁剪**，只做定比降采样。站点算法没有任何缩放，字号恒为
   40px，所以「大写高 29px / 小写 x-height 21px」这个绝对尺度是免费且强力的
   判别特征。裁包围盒再拉伸会把它抹掉，使 W/w、V/v、X/x、S/s 这些仅靠尺寸
   区分的字符对退化成同形 —— 实测该做法在孤立字符上就损失约 4%。

实测对照（合成集，mixed，仅换归一化方式）：
    包围盒拉伸 24×24    99.81%
    包围盒等比填充      99.62%
    定比降采样（采用）  99.94%

至于邻字符侵入：实测**不做**连通域剥离反而更好（100% vs 93%）。因为模板同样
取自定长窗、目标字符始终占据窗口内固定位置，邻字符碎片是位置可预期的弱扰动；
而连通域归属判断一旦出错就会切掉目标字符自身的笔画，损失大得多。
"""

from __future__ import annotations

import numpy as np

from .generator import ANCHOR_Y, FG_RANGE, char_positions

# 灰度阈值取前景色上界：<= 该值算字符，> 该值算干扰或背景。
# 站点字符 RGB ∈ [0x30,0x95]，干扰噪点与线条 ∈ [0x96,0xdb]，两区间不重叠。
FG_THRESHOLD = FG_RANGE[1]

# 窗口边界（相对锚点 (x, ANCHOR_Y)），= 实测字形极值各留 1px 余量。
# 见模块 docstring 的实测说明；改动这些值需重新跑 measure_glyph_extents 并重建模板库。
WIN_LEFT = -9
WIN_RIGHT = 41
WIN_TOP = -31
WIN_BOTTOM = 28
WIN_W = WIN_RIGHT - WIN_LEFT + 1  # 51
WIN_H = WIN_BOTTOM - WIN_TOP + 1  # 60

# 降采样后的网格。约 2.4 倍下采样：保留 Arial Bold 40px 的笔画拓扑，
# 又把单字符维度压到 480（整图 30000 的 1/62）。
GRID_H = 24
GRID_W = 20
GRID_DIM = GRID_H * GRID_W


def to_gray(image) -> np.ndarray:
    """PIL.Image 或 ndarray → uint8 灰度数组。"""
    if isinstance(image, np.ndarray):
        arr = image
        if arr.ndim == 3:
            # ITU-R BT.601 亮度权重，与浏览器端采集脚本保持一致。
            # 必须先升位再乘：uint8 上直接乘 299 会溢出，numpy 2.x 直接抛
            # OverflowError（早期只走 PIL 路径，这条分支从未被触发过）。
            wide = arr[..., :3].astype(np.int32)
            arr = (wide[..., 0] * 299 + wide[..., 1] * 587 + wide[..., 2] * 114) // 1000
        return arr.astype(np.uint8)
    return np.asarray(image.convert("L"), dtype=np.uint8)


def binarize(gray: np.ndarray) -> np.ndarray:
    """灰度 → {0,1} 前景掩码（1 = 字符像素）。"""
    return (gray <= FG_THRESHOLD).astype(np.uint8)


def _extract(mask: np.ndarray, x: int, y: int = ANCHOR_Y) -> np.ndarray:
    """以 (x, y) 为锚点取定长窗；越界部分补 0。"""
    win = np.zeros((WIN_H, WIN_W), dtype=np.uint8)
    top, left = y + WIN_TOP, x + WIN_LEFT
    sy0, sy1 = max(0, top), min(mask.shape[0], top + WIN_H)
    sx0, sx1 = max(0, left), min(mask.shape[1], left + WIN_W)
    if sx1 > sx0 and sy1 > sy0:
        win[sy0 - top: sy1 - top, sx0 - left: sx1 - left] = mask[sy0:sy1, sx0:sx1]
    return win


def crop_windows(mask: np.ndarray, length: int) -> list[np.ndarray]:
    """按站点的确定性布局切出 length 个字符窗口。

    位置来自 char_positions()（已用录制值验证），因此无需投影分割或连通域分析
    —— 那些方法在字符粘连或干扰线跨越时都会失效，而这里坐标是已知量。
    """
    return [_extract(mask, x) for x in char_positions(length)]


def normalize(window: np.ndarray) -> np.ndarray:
    """定长窗 → GRID_H×GRID_W float32 {0,1}（定比降采样，保留绝对尺度）。

    用最近邻而非插值：二值图上插值会在笔画边缘产生灰阶，反而让余弦相似度变钝。
    """
    h, w = window.shape
    yi = np.minimum((np.arange(GRID_H) * h) // GRID_H, h - 1)
    xi = np.minimum((np.arange(GRID_W) * w) // GRID_W, w - 1)
    return window[np.ix_(yi, xi)].astype(np.float32)


def image_to_windows(image, length: int) -> list[np.ndarray]:
    """整图 → length 个归一化字符位图。这是对外的唯一入口。"""
    mask = binarize(to_gray(image))
    return [normalize(w) for w in crop_windows(mask, length)]


def _glyph_mask(ch: str, angle: float) -> tuple[np.ndarray, int, int]:
    """渲染单字符，返回 ({0,1} 掩码, 锚点 x, 锚点 y)。

    渲染一律走 generator.draw_glyph —— 站点的 textBaseline="middle" 偏移与
    绕锚点旋转都封在那里。此处若自己再写一遍绘制逻辑，模板与生成器就会分叉，
    而这种分叉不会报错、只会静默掉准确率（曾因漏掉 14px 偏移掉到 4.55%）。
    """
    from .generator import draw_glyph

    layer, (ax, ay) = draw_glyph(ch, angle, 255)
    return (np.asarray(layer, dtype=np.uint8) > 127).astype(np.uint8), ax, ay


def render_char_grid(ch: str, angle: float = 0.0) -> np.ndarray:
    """渲染单字符为归一化位图（构建模板库用，需要 pillow）。

    复用 _extract + normalize，确保模板与待识别窗口经过同一套变换 ——
    这条一致性是模板法准确率的前提。
    """
    arr, ax, ay = _glyph_mask(ch, angle)
    return normalize(_extract(arr, ax, ay))


def measure_glyph_extents(chars: str, angles) -> tuple[int, int, int, int]:
    """实测字形相对锚点的极值，返回 (dx_min, dx_max, dy_min, dy_max)。

    用于验证 WIN_* 常量是否仍然足够（站点换字体/字号后需重跑）。
    """
    lo_x = hi_x = lo_y = hi_y = 0
    for ch in chars:
        for ang in angles:
            arr, ax, ay = _glyph_mask(ch, ang)
            ys, xs = np.nonzero(arr)
            if not len(ys):
                continue
            lo_x = min(lo_x, int(xs.min()) - ax)
            hi_x = max(hi_x, int(xs.max()) - ax)
            lo_y = min(lo_y, int(ys.min()) - ay)
            hi_y = max(hi_y, int(ys.max()) - ay)
    return lo_x, hi_x, lo_y, hi_y


__all__ = [
    "FG_THRESHOLD",
    "GRID_DIM",
    "GRID_H",
    "GRID_W",
    "WIN_H",
    "WIN_W",
    "binarize",
    "crop_windows",
    "image_to_windows",
    "measure_glyph_extents",
    "normalize",
    "render_char_grid",
    "to_gray",
]
