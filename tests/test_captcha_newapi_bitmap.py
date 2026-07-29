# -*- coding: utf-8 -*-
"""New API 签到图形验证码识别回归。

真值来自站点真实输出（jianzhile.vip），以 base64 内嵌 3 张作为固定样本；
另用「字模表反向合成」做往返测试，覆盖全部 32 个字符与随机像素缺失。

不重新训练也不依赖外部样本文件：字模表就在源码里，合成器直接复用它。
"""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import numpy as np
import pytest

from captcha_ocr import newapi_bitmap as NB

# ── 真实样本（站点原始 PNG，标签由 6x9 位图逐位裁决而非肉眼）────────────────────
# 刻意放在独立 JSON 而非源码字面量：base64 手抄过一次就损坏了一张图。
SAMPLE_PATH = Path(__file__).parent / "data" / "newapi_captcha_samples.json"
_PAYLOAD = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
REAL_SAMPLES: tuple[tuple[str, str], ...] = tuple(
    (s["label"], s["png_base64"]) for s in _PAYLOAD["samples"]
)


def _decode(b64: str) -> np.ndarray:
    pytest.importorskip("PIL", reason="解码 PNG 需要 pillow（dev 依赖）")
    import io

    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        return np.asarray(im.convert("RGB"))


# ── 真实样本 ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("label,b64", REAL_SAMPLES, ids=[s[0] for s in REAL_SAMPLES])
def test_real_captcha_is_solved_exactly(label: str, b64: str) -> None:
    result = NB.solve_array(_decode(b64))
    assert result.text == label
    # exact 表示每位都零多余像素且无并列候选 —— 这套验证码上等价于「确定正确」
    assert result.exact, result.detail


def test_real_captcha_dimensions_match_site() -> None:
    img = _decode(REAL_SAMPLES[0][1])
    assert img.shape[:2] == (NB.HEIGHT, NB.WIDTH) == (58, 160)


# ── 字模表自身的性质 ─────────────────────────────────────────────────────────
def test_charset_is_confusable_free_32() -> None:
    """站点用标准无易混淆集：数字 2-9 + 大写去 I/O，共 32 个。"""
    assert NB.CHARSET == "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    for ch in "01IO":
        assert ch not in NB.CHARSET


def test_glyphs_are_pairwise_distinct() -> None:
    """任意两个字模都不能相同，否则识别必然出现并列。"""
    seen: dict[bytes, str] = {}
    for ch, rows in NB.GLYPHS.items():
        key = "\n".join(rows).encode()
        assert key not in seen, f"{ch} 与 {seen.get(key)} 字模相同"
        seen[key] = ch


def test_subset_glyph_pairs_are_resolved_by_asymmetric_cost() -> None:
    """站点字体里存在「小字模是大字模子集」的字符对，这是实测事实而非缺陷。

    实测 6 对：8⊂B、S⊂8、S⊂B、C⊂Q、F⊂E、P⊂R。
    噪声只删不增，所以这类对无法靠「零多余像素」单独区分，必须靠代价的非对称性：

      · 小字实例：对小/大字模都是 extra=0，用「缺失更少」决胜 → 判为小字（正确）
      · 大字实例：对小字模 extra>0 直接排除 → 判为大字（正确）

    残余风险：大字的差异块若全部 4 个像素都被抹掉，就真的退化成小字。
    以实测缺失率（每像素约 5%~15%）算，8 个差异块同时全灭的概率可忽略。
    """
    from captcha_ocr.newapi_bitmap import _cost, _table

    table = _table()
    subset_pairs = {(a, b) for a in table for b in table
                    if a != b and _cost(table[a], table[b])[0] == 0}
    assert subset_pairs == {("8", "B"), ("S", "8"), ("S", "B"),
                            ("C", "Q"), ("F", "E"), ("P", "R")}, subset_pairs

    for small, big in subset_pairs:
        # 小字实例必须判成小字（靠缺失像素更少胜出）
        ranked = sorted((_cost(table[small], t), ch) for ch, t in table.items())
        assert ranked[0][1] == small, f"{small} 被判成 {ranked[0][1]}"
        # 大字实例必须判成大字（小字模因 extra>0 被排除）
        ranked = sorted((_cost(table[big], t), ch) for ch, t in table.items())
        assert ranked[0][1] == big, f"{big} 被判成 {ranked[0][1]}"


def test_every_glyph_matches_itself_uniquely() -> None:
    """每个字模用自己去匹配都必须唯一命中，不能出现并列首位。"""
    from captcha_ocr.newapi_bitmap import _cost, _table

    table = _table()
    for ch, grid in table.items():
        ranked = sorted((_cost(grid, t), name) for name, t in table.items())
        assert ranked[0][1] == ch
        assert ranked[0][0] < ranked[1][0], f"{ch} 与 {ranked[1][1]} 并列"


# ── 合成往返：覆盖全部 32 字符与随机像素缺失 ──────────────────────────────────
def _render(text: str, rng: random.Random, dropout: float = 0.0) -> np.ndarray:
    """按站点算法反向合成：2 倍放大点阵 + 每字符固定色 + 随机像素缺失 + 干扰线。"""
    img = np.full((NB.HEIGHT, NB.WIDTH, 3), NB.BACKGROUND, dtype=np.uint8)
    # 干扰线（浅色，不与字符色重叠，因此不该影响识别）
    for _ in range(6):
        y = rng.randrange(NB.HEIGHT)
        img[y, :] = NB.NOISE_COLORS[0]
    for i, ch in enumerate(text):
        rows = NB.GLYPHS[ch]
        bits = np.array([[c == "1" for c in row] for row in rows], dtype=bool)
        big = np.repeat(np.repeat(bits, NB.SCALE, axis=0), NB.SCALE, axis=1)
        if dropout:
            drop = np.array([[rng.random() < dropout for _ in range(big.shape[1])]
                             for _ in range(big.shape[0])])
            # 只抹掉，绝不新增 —— 与站点噪声性质一致
            big = big & ~drop
        y0 = 20 + rng.randint(-2, 2)
        x0 = 16 + i * 26 + rng.randint(-2, 2)
        ys, xs = np.nonzero(big)
        for y, x in zip(ys, xs):
            yy, xx = y0 + y, x0 + x
            if 0 <= yy < NB.HEIGHT and 0 <= xx < NB.WIDTH:
                img[yy, xx] = NB.CHAR_COLORS[i]
    return img


def test_synthetic_roundtrip_covers_whole_charset() -> None:
    """把 32 个字符轮流放到 5 个位置合成再识别，无缺失时必须全对。"""
    rng = random.Random(20260729)
    chars = NB.CHARSET
    for offset in range(0, len(chars), NB.CHAR_COUNT):
        text = (chars + chars)[offset:offset + NB.CHAR_COUNT]
        result = NB.solve_array(_render(text, rng))
        assert result.text == text, f"合成 {text} → {result.text} {result.detail}"
        assert result.exact


@pytest.mark.parametrize("dropout", [0.05, 0.15, 0.25])
def test_synthetic_roundtrip_tolerates_pixel_dropout(dropout: float) -> None:
    """随机抹像素后仍应全对：OR 降采样只要每 2×2 块剩 1 个像素即可复原。"""
    rng = random.Random(7)
    ok = total = 0
    for _ in range(24):
        text = "".join(rng.choice(NB.CHARSET) for _ in range(NB.CHAR_COUNT))
        result = NB.solve_array(_render(text, rng, dropout=dropout))
        total += 1
        ok += result.text == text
    assert ok == total, f"dropout={dropout} 下 {ok}/{total}"


def test_restore_cell_repairs_dropout() -> None:
    """直接验证核心机制：抹掉每块 3/4 像素后仍还原出同一张点阵。"""
    rows = NB.GLYPHS["N"]
    bits = np.array([[c == "1" for c in row] for row in rows], dtype=bool)
    big = np.repeat(np.repeat(bits, 2, axis=0), 2, axis=1)
    holed = big.copy()
    holed[0::2, 0::2] = False   # 每个 2x2 块只留 3 个
    holed[1::2, 0::2] = False   # 每个 2x2 块只留 2 个
    holed[1::2, 1::2] = False   # 每个 2x2 块只留 1 个
    restored = NB.restore_cell(holed)
    expected = NB.restore_cell(big)
    assert restored is not None and expected is not None
    assert np.array_equal(restored, expected)


def test_blank_image_reports_not_exact() -> None:
    blank = np.full((NB.HEIGHT, NB.WIDTH, 3), NB.BACKGROUND, dtype=np.uint8)
    result = NB.solve_array(blank)
    assert result.text == ""
    assert not result.exact


# ── 浏览器脚本 helper 接线 ───────────────────────────────────────────────────
def _helpers():
    """构造一个最小 ScriptHelpers（不需要真实 page，solve_captcha 不碰浏览器）。"""
    from browser.script_helpers import ScriptHelpers

    # page/context 传 None：solve_captcha 是纯计算，不触碰浏览器
    return ScriptHelpers(page=None, context=None, site=None,
                         screenshot_dir=Path("."), log=lambda _m: None)


def test_helper_solves_data_url() -> None:
    label, b64 = REAL_SAMPLES[0]
    assert _helpers().solve_captcha(f"data:image/png;base64,{b64}") == label


def test_helper_solves_raw_bytes() -> None:
    label, b64 = REAL_SAMPLES[1]
    assert _helpers().solve_captcha(base64.b64decode(b64)) == label


def test_helper_returns_empty_on_uncertain_input() -> None:
    """识别不确定时返回空串，让调用方换图重试，而不是提交一个猜测。"""
    blank = np.full((NB.HEIGHT, NB.WIDTH, 3), NB.BACKGROUND, dtype=np.uint8)
    assert _helpers().solve_captcha(blank) == ""


def test_helper_rejects_unknown_scheme() -> None:
    label, b64 = REAL_SAMPLES[0]
    assert _helpers().solve_captcha(f"data:image/png;base64,{b64}", scheme="nope") == ""
