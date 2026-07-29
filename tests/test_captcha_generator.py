# -*- coding: utf-8 -*-
"""生成器回归：本地复刻必须与站点录制值逐项吻合。

复刻一旦偏离站点，模板库就会静默失准 —— 不报错、只掉准确率，且极难定位
（实测漏掉 textBaseline="middle" 的 11px 偏移时，真实样本准确率从 100% 掉到 4.55%）。
因此这里把所有录制来的常量都钉死。
"""

from __future__ import annotations

import random

import numpy as np
import pytest

pytest.importorskip("PIL", reason="生成器需要 pillow（dev 依赖）")

from captcha_ocr import generator as G  # noqa: E402
from captcha_ocr.preprocess import binarize, to_gray  # noqa: E402


# ── 录制值：改动这些断言前必须重新在浏览器里录制 ──────────────────────────────
def test_canvas_and_font_constants_match_recording() -> None:
    assert (G.WIDTH, G.HEIGHT) == (300, 100)
    assert G.ANCHOR_Y == 50, "站点 translate 的 y 值"
    assert G.CHAR_STEP == 35
    assert G.FONT_SIZE == 40
    assert G.MAX_ROTATION == 0.35
    assert G.NOISE_DOTS == 50
    assert G.NOISE_LINES == 7


def test_middle_baseline_offset_is_browser_measured() -> None:
    """textBaseline="middle" 的锚点→基线距离 = 11px（浏览器实测）。

    公式 (fontAscent-fontDescent)/2 会算出 14，比实际大 3px。这个差值不是
    可以忽略的舍入 —— 真实样本准确率会从 100% 掉到 93.9%。
    """
    assert G.MIDDLE_TO_BASELINE == 11
    assert G.middle_to_baseline() == 11


@pytest.mark.parametrize(
    "length,expected",
    [
        (4, [80, 115, 150, 185]),
        (5, [62, 97, 132, 167, 202]),
        (6, [45, 80, 115, 150, 185, 220]),
        (8, [20, 55, 90, 125, 160, 195, 230, 265]),
    ],
)
def test_char_positions_match_recorded_layout(length: int, expected: list[int]) -> None:
    assert G.char_positions(length) == expected


def test_char_positions_overflow_beyond_eight_is_site_bug() -> None:
    """n>8 时站点自己会把字符画到画布外，这是站点的 bug，不是我们的。

    记录在测试里，防止后来者「修正」布局公式而与站点不一致。
    """
    assert G.char_positions(12)[-1] == 405 > G.WIDTH


# ── 字符集 ───────────────────────────────────────────────────────────────────
def test_charsets_exclude_confusable_characters() -> None:
    """站点刻意剔除 0/O/o、1/l/I；number 是唯一保留 0 和 1 的字符集。"""
    for name in ("alpha", "mixed", "complex"):
        cs = G.CHARSETS[name]
        for ch in "0Oo1lI":
            assert ch not in cs, f"{name} 不应含易混淆字符 {ch!r}"
    assert set("01") <= set(G.CHARSETS["number"])


def test_charset_sizes_match_site() -> None:
    assert len(G.CHARSETS["number"]) == 10
    assert len(G.CHARSETS["alpha"]) == 47
    assert len(G.CHARSETS["mixed"]) == 55
    assert len(G.CHARSETS["complex"]) == 63


# ── 渲染 ─────────────────────────────────────────────────────────────────────
def test_render_produces_expected_canvas() -> None:
    img = G.render("Ab3xY9", random.Random(1))
    assert img.size == (G.WIDTH, G.HEIGHT)
    assert img.mode == "RGB"


def test_foreground_and_noise_color_ranges_do_not_overlap() -> None:
    """阈值分离的前提：字符色区间与干扰色区间不重叠。整条管线都依赖它。"""
    assert G.FG_RANGE[1] < G.BG_RANGE[0]


def test_noise_is_removed_by_threshold() -> None:
    """只画噪点不画字符时，二值化后应当什么都不剩。"""
    rng = random.Random(7)
    blank = G.render("", rng)  # 无字符，仅噪点与干扰线
    assert binarize(to_gray(blank)).sum() == 0


def test_seeded_generation_is_reproducible() -> None:
    a = [s.text for s in G.iter_samples(5, seed=99)]
    b = [s.text for s in G.iter_samples(5, seed=99)]
    assert a == b
    assert [s.text for s in G.iter_samples(5, seed=100)] != a


def test_glyphs_stay_within_canvas_for_supported_lengths() -> None:
    """4..8 长度下字符墨迹不应被画布边界裁掉。"""
    rng = random.Random(3)
    for length in (4, 5, 6, 7, 8):
        mask = binarize(to_gray(G.sample(length, "complex", rng).image))
        ys, xs = np.nonzero(mask)
        assert len(ys) > 0
        assert 0 < xs.min() and xs.max() < G.WIDTH - 1
        assert 0 < ys.min() and ys.max() < G.HEIGHT - 1
