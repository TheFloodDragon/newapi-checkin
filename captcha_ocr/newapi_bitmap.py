#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`bitmap_code` 固定5位点阵字符验证码识别。

与 randomtool.cn 那套（见 generator.py）是**完全不同的验证码**，因此单独成篇：

| 项 | randomtool.cn | New API 签到验证码 |
|---|---|---|
| 画布 | 300×100 | 160×58 |
| 字数 | 4–12 | 固定 5 |
| 字体 | Arial Bold 40px（抗锯齿） | 6×9 点阵字体，2 倍放大，**无抗锯齿** |
| 变形 | 每字符 ±0.35 rad 旋转 | 无旋转、无缩放，仅位置抖动 |
| 分割 | 靠已知 x 坐标 | 靠颜色——每字符一个固定色 |
| 干扰 | 噪点 + 干扰线（浅色） | 折线 + 散点（浅蓝），另有**像素随机缺失** |

正因如此，randomtool 那套 Arial 模板在这里只有 58% 单字符 / 约 7% 整图准确率
（实测），不能复用。但也不需要训练：这套验证码有三个可利用的确定性结构。

## 一、颜色即分割

整图只有 8 种颜色，且每个字符占用固定的一种深色，从左到右顺序固定：

    #111827 近黑 → #1d4ed8 蓝 → #047857 绿 → #b45309 橙 → #be123c 品红

所以取色即分割，不需要投影切分或连通域分析，也完全不受干扰线跨字符影响。

## 二、字形是 2 倍放大的点阵，OR 降采样可完全修复噪声

字形本体是 6×9（部分 6×10、5×9）点阵字体按 2 倍整数放大，所以原图上每个
2×2 像素块要么全亮要么全暗。噪声表现为**随机抹掉单个像素**（只删不增，535 个
字形实测无一例外）。因此按 2×2 块做 OR 降采样，只要每块还剩 1 个像素就能
100% 还原点阵原貌——噪声被彻底消掉，不是「缓解」。

## 三、还原后是精确查表

还原出的 6×9 位图与字模逐位比对即可。实测 535 个字形全部零多余像素命中且
无并列，26 张已标注样本（130 字符）100% 正确。

字模表由「同字符多实例对齐取并集」得出，字符集是标准的无易混淆 32 字符
（数字 2-9 + 大写 A-Z 去掉 I/O）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

WIDTH = 160
HEIGHT = 58
CHAR_COUNT = 5
SCALE = 2  # 点阵字体的整数放大倍数

# 5 个字符各自的固定颜色，按从左到右的绘制顺序
CHAR_COLORS: tuple[tuple[int, int, int], ...] = (
    (0x11, 0x18, 0x27),  # 近黑
    (0x1D, 0x4E, 0xD8),  # 蓝
    (0x04, 0x78, 0x57),  # 绿
    (0xB4, 0x53, 0x09),  # 橙
    (0xBE, 0x12, 0x3C),  # 品红
)
BACKGROUND = (0xF8, 0xFA, 0xFC)
NOISE_COLORS = ((0x8E, 0xA4, 0xC5), (0xAA, 0xC5, 0xED))

# 字模表：每字符一组行位串（'1' = 点亮）。由真实样本对齐取并集得出。
GLYPHS: dict[str, tuple[str, ...]] = {
    "2": ("011110", "100001", "100001", "000001", "000010", "001100", "010000", "100000", "111111"),
    "3": ("111111", "000001", "000010", "000100", "001110", "000001", "000001", "100001", "011110"),
    "4": ("000010", "000110", "001010", "010010", "100010", "100010", "111111", "000010", "000010"),
    "5": ("111111", "100000", "100000", "101110", "110001", "000001", "000001", "100001", "011110"),
    "6": ("001110", "010000", "100000", "100000", "101110", "110001", "100001", "100001", "011110"),
    "7": ("111111", "000001", "000010", "000100", "000100", "001000", "001000", "010000", "010000"),
    "8": ("011110", "100001", "100001", "100001", "011110", "100001", "100001", "100001", "011110"),
    "9": ("011110", "100001", "100001", "100011", "011101", "000001", "000001", "000010", "011100"),
    "A": ("001100", "010010", "100001", "100001", "100001", "111111", "100001", "100001", "100001"),
    "B": ("111110", "110001", "110001", "110001", "011110", "110001", "110001", "110001", "111110"),
    "C": ("011110", "100001", "100000", "100000", "100000", "100000", "100000", "100001", "011110"),
    "D": ("111110", "010001", "010001", "010001", "010001", "010001", "010001", "010001", "111110"),
    "E": ("111111", "100000", "100000", "100000", "111100", "100000", "100000", "100000", "111111"),
    "F": ("111111", "100000", "100000", "100000", "111100", "100000", "100000", "100000", "100000"),
    "G": ("011110", "100001", "100000", "100000", "100000", "100111", "100001", "100011", "011101"),
    "H": ("100001", "100001", "100001", "100001", "111111", "100001", "100001", "100001", "100001"),
    "J": ("000111", "000010", "000010", "000010", "000010", "000010", "000010", "100010", "011100"),
    "K": ("100001", "100010", "100100", "101000", "110000", "101000", "100100", "100010", "100001"),
    "L": ("100000", "100000", "100000", "100000", "100000", "100000", "100000", "100000", "111110", "000001"),
    "M": ("100001", "110011", "110011", "101101", "101101", "100001", "100001", "100001", "100001"),
    "N": ("100001", "100001", "110001", "101001", "100101", "100011", "100001", "100001", "100001"),
    "P": ("111110", "100001", "100001", "100001", "111110", "100000", "100000", "100000", "100000"),
    "Q": ("011110", "100001", "100001", "100001", "100001", "100001", "101001", "100101", "011110", "000001"),
    "R": ("111110", "100001", "100001", "100001", "111110", "101000", "100100", "100010", "100001"),
    "S": ("011110", "100001", "100000", "100000", "011110", "000001", "000001", "100001", "011110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("100001", "100001", "100001", "100001", "100001", "100001", "100001", "100001", "011110"),
    "V": ("100001", "100001", "100001", "010010", "010010", "010010", "001100", "001100", "001100"),
    "W": ("100001", "100001", "100001", "100001", "101101", "101101", "110011", "110011", "100001"),
    "X": ("100001", "100001", "010010", "010010", "001100", "010010", "010010", "100001", "100001"),
    "Y": ("10001", "10001", "01010", "01010", "00100", "00100", "00100", "00100", "00100"),
    "Z": ("111111", "000001", "000010", "000100", "001100", "001000", "010000", "100000", "111111"),
}

CHARSET = "".join(sorted(GLYPHS))

# 位移容忍范围：还原后的位图靠内容左上角定位，边缘整块被抹掉时会偏 1 格。
_SHIFTS = tuple((dy, dx) for dy in (-2, -1, 0, 1, 2) for dx in (-2, -1, 0, 1, 2))
_PAD_H, _PAD_W = 14, 10  # 容纳 6×10 位图 + 位移余量


def _to_canvas(bits: np.ndarray) -> np.ndarray:
    """把小位图放进固定画布（内容左上角对齐），便于统一做位移比较。"""
    out = np.zeros((_PAD_H, _PAD_W), dtype=bool)
    h, w = min(bits.shape[0], _PAD_H - 2), min(bits.shape[1], _PAD_W - 2)
    out[1:1 + h, 1:1 + w] = bits[:h, :w]
    return out


def _parse_glyphs() -> dict[str, np.ndarray]:
    table = {}
    for ch, rows in GLYPHS.items():
        arr = np.array([[c == "1" for c in row] for row in rows], dtype=bool)
        table[ch] = _to_canvas(arr)
    return table


_TABLE: dict[str, np.ndarray] | None = None


def _table() -> dict[str, np.ndarray]:
    global _TABLE
    if _TABLE is None:
        _TABLE = _parse_glyphs()
    return _TABLE


def _shift(grid: np.ndarray, dy: int, dx: int) -> np.ndarray:
    out = np.zeros_like(grid)
    a0, a1 = max(0, dy), min(_PAD_H, _PAD_H + dy)
    b0, b1 = max(0, dx), min(_PAD_W, _PAD_W + dx)
    out[a0:a1, b0:b1] = grid[a0 - dy:a1 - dy, b0 - dx:b1 - dx]
    return out


def _cost(cell: np.ndarray, tpl: np.ndarray) -> tuple[int, int]:
    """最优位移下的 (多余像素数, 缺失像素数)。

    非对称是刻意的：噪声只会抹掉像素、绝不会增加，所以「候选有而字模无」的
    像素基本等价于「不是这个字」。缺失像素只用来在多余像素同为 0 时决胜。
    """
    best: tuple[int, int] | None = None
    for dy, dx in _SHIFTS:
        moved = _shift(cell, dy, dx)
        pair = (int((moved & ~tpl).sum()), int((tpl & ~moved).sum()))
        if best is None or pair < best:
            best = pair
    return best  # type: ignore[return-value]


def to_gray_free_mask(image: np.ndarray, color: tuple[int, int, int]) -> np.ndarray:
    """按精确颜色取掩码。

    刻意不做灰度化/阈值：这套验证码无抗锯齿、调色板只有 8 色，精确取色比
    阈值更稳，且天然把干扰线排除在外（干扰色与字符色不同）。
    """
    return np.all(image[..., :3] == np.array(color, dtype=np.uint8), axis=-1)


def restore_cell(mask: np.ndarray) -> np.ndarray | None:
    """2×2 块 OR 降采样，修复随机像素缺失并还原点阵原貌。

    对齐要枚举 4 种奇偶：字形边缘整块被抹掉时，观测到的包围盒会偏移 1 像素。
    以「块内像素数只可能是 0 或 4」的比例作为对齐得分——错位时会出现大量
    2/4 块，得分显著下降。
    """
    ys, xs = np.nonzero(mask)
    if len(ys) == 0:
        return None
    best_score, best_bits = -1.0, None
    for py in (0, 1):
        for px in (0, 1):
            y0, x0 = int(ys.min()) - py, int(xs.min()) - px
            h = -(-(int(ys.max()) - y0 + 1) // SCALE) * SCALE
            w = -(-(int(xs.max()) - x0 + 1) // SCALE) * SCALE
            sub = np.zeros((h, w), dtype=bool)
            a0, a1 = max(0, y0), min(mask.shape[0], y0 + h)
            b0, b1 = max(0, x0), min(mask.shape[1], x0 + w)
            sub[a0 - y0:a1 - y0, b0 - x0:b1 - x0] = mask[a0:a1, b0:b1]
            blocks = sub.reshape(h // SCALE, SCALE, w // SCALE, SCALE).sum(axis=(1, 3))
            score = float(np.isin(blocks, (0, SCALE * SCALE)).mean())
            if score > best_score:
                best_score, best_bits = score, blocks > 0
    return _to_canvas(best_bits) if best_bits is not None else None


@dataclass(frozen=True)
class CaptchaResult:
    """识别结果。

    exact 为 True 表示 5 个字符全部零多余像素命中且无并列 —— 在这套验证码上
    等价于「确定正确」。为 False 时调用方应换一张验证码重试，而不是硬提交。
    """

    text: str
    exact: bool
    detail: tuple[tuple[str, int, int], ...]  # 每位 (字符, 多余像素, 缺失像素)


def solve_array(image: np.ndarray) -> CaptchaResult:
    """RGB ndarray → 识别结果。"""
    table = _table()
    chars: list[str] = []
    detail: list[tuple[str, int, int]] = []
    exact = True
    for color in CHAR_COLORS:
        cell = restore_cell(to_gray_free_mask(image, color))
        if cell is None:
            chars.append("")
            detail.append(("", 99, 99))
            exact = False
            continue
        ranked = sorted((_cost(cell, tpl), ch) for ch, tpl in table.items())
        (extra, miss), ch = ranked[0]
        # 并列意味着两个字模同样合理，不能当作确定结果
        tied = len(ranked) > 1 and ranked[1][0] == ranked[0][0]
        if extra > 0 or tied:
            exact = False
        chars.append(ch)
        detail.append((ch, extra, miss))
    return CaptchaResult(text="".join(chars), exact=exact, detail=tuple(detail))


def solve_bytes(data: bytes) -> CaptchaResult:
    """PNG 字节流 → 识别结果（需要 pillow 解码）。"""
    import io

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "解码验证码 PNG 需要 pillow。请执行 uv sync --extra dev，"
            "或自行解码后调用 solve_array（仅需 numpy）。"
        ) from exc
    with Image.open(io.BytesIO(data)) as im:
        return solve_array(np.asarray(im.convert("RGB")))


def solve_data_url(data_url: str) -> CaptchaResult:
    """`data:image/png;base64,...` → 识别结果（接口直接返回这种形态）。"""
    import base64

    payload = data_url.split(",", 1)[1] if "," in data_url else data_url
    return solve_bytes(base64.b64decode(payload))


__all__ = [
    "CHARSET",
    "CHAR_COLORS",
    "CHAR_COUNT",
    "GLYPHS",
    "HEIGHT",
    "WIDTH",
    "CaptchaResult",
    "restore_cell",
    "solve_array",
    "solve_bytes",
    "solve_data_url",
]
