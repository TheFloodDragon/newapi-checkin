# -*- coding: utf-8 -*-
"""识别器回归：真实样本准确率是唯一有效的验收口径。

合成集自测只能证明「复刻与复刻自洽」，不能证明「复刻与站点一致」。
因此这里的门槛断言全部跑在 data/real_samples.json（站点真实 Canvas 输出）上。
"""

from __future__ import annotations

import random
import time

import numpy as np
import pytest

from captcha_ocr import collect
from captcha_ocr.matcher import ALL_CHARS, DEFAULT_ANGLES, TemplateMatcher
from captcha_ocr.predictor import MAX_LENGTH, Predictor

REAL_SAMPLES = collect.load_samples()
needs_real = pytest.mark.skipif(not REAL_SAMPLES, reason="缺少真实样本，无法评测准确率")


@pytest.fixture(scope="module")
def matcher() -> TemplateMatcher:
    return TemplateMatcher.load_or_build()


@pytest.fixture(scope="module")
def predictor() -> Predictor:
    return Predictor()


# ── 模板库结构 ───────────────────────────────────────────────────────────────
def test_angle_grid_covers_full_rotation_range() -> None:
    """必须覆盖 ±0.35 两端。

    早期用 int(0.7/0.05) 截断，浮点误差导致少一档，+0.30~+0.35 完全没有模板。
    """
    from captcha_ocr.generator import MAX_ROTATION

    assert min(DEFAULT_ANGLES) == pytest.approx(-MAX_ROTATION)
    assert max(DEFAULT_ANGLES) == pytest.approx(MAX_ROTATION)


def test_template_charset_covers_every_site_charset() -> None:
    """number 含 0/1，其余字符集不含 —— 模板库必须是并集。"""
    from captcha_ocr.generator import CHARSETS

    for name, charset in CHARSETS.items():
        missing = set(charset) - set(ALL_CHARS)
        assert not missing, f"{name} 的 {missing} 缺少模板"


def test_templates_are_unit_normalized(matcher: TemplateMatcher) -> None:
    """模板存盘即单位化，运行期点积直接等于余弦相似度。"""
    norms = np.linalg.norm(matcher.templates.reshape(-1, matcher.templates.shape[-1]), axis=1)
    assert np.allclose(norms[norms > 0], 1.0, atol=1e-5)


def test_charset_constraint_narrows_candidates(matcher: TemplateMatcher) -> None:
    """限定 number 时不应返回字母。"""
    from captcha_ocr.preprocess import render_char_grid

    ch, _, _ = matcher.predict_window(render_char_grid("S"), "number")
    assert ch in "0123456789"


# ── 真实样本门槛（验收标准）──────────────────────────────────────────────────
@needs_real
def test_real_sample_accuracy_meets_threshold(predictor: Predictor) -> None:
    ok_char = tot_char = ok_img = 0
    failures: list[tuple[str, str]] = []
    for s in REAL_SAMPLES:
        result = predictor.predict_mask(s.to_mask(), s.length, s.kind)
        for want, got in zip(s.label, result.text):
            tot_char += 1
            ok_char += want == got
        if result.text == s.label:
            ok_img += 1
        else:
            failures.append((s.label, result.text))

    char_acc = ok_char / tot_char
    img_acc = ok_img / len(REAL_SAMPLES)
    assert char_acc >= 0.995, f"单字符准确率 {char_acc:.2%} 低于门槛，错例：{failures[:5]}"
    assert img_acc >= 0.98, f"整图准确率 {img_acc:.2%} 低于门槛，错例：{failures[:5]}"


@needs_real
def test_real_sample_coverage_is_broad_enough() -> None:
    """样本必须覆盖 4 种字符集，否则准确率数字没有代表性。"""
    info = collect.stats()
    assert info["images"] >= 300
    assert info["chars"] >= 1800
    assert set(info["by_kind"]) == {"number", "alpha", "mixed", "complex"}
    assert min(info["by_kind"].values()) >= 50


@needs_real
def test_inference_latency_within_budget(predictor: Predictor) -> None:
    """单张延迟预算 20ms（单核 CPU）。"""
    subset = REAL_SAMPLES[:80]
    masks = [(s.to_mask(), s.length, s.kind) for s in subset]
    start = time.perf_counter()
    for mask, length, kind in masks:
        predictor.predict_mask(mask, length, kind)
    per_image_ms = (time.perf_counter() - start) / len(masks) * 1000
    assert per_image_ms < 20, f"单张 {per_image_ms:.1f} ms 超出预算"


@needs_real
def test_template_library_size_within_budget() -> None:
    from captcha_ocr.matcher import TEMPLATES_PATH

    assert TEMPLATES_PATH.exists(), "模板库缺失，请运行 python -m captcha_ocr build-templates"
    assert TEMPLATES_PATH.stat().st_size < 3_000_000


# ── 合成集（仅作自洽性检查，不作为准确度证据）────────────────────────────────
def test_synthetic_self_consistency(predictor: Predictor) -> None:
    """复刻自测：这个数字不能对外声称为准确率，只用来发现管线自身回归。"""
    pytest.importorskip("PIL", reason="生成合成样本需要 pillow")
    from captcha_ocr import generator as G

    rng = random.Random(20260729)
    ok = total = 0
    for _ in range(60):
        s = G.sample(6, "mixed", rng)
        total += 1
        ok += predictor.predict(s.image, 6, "mixed").text == s.text
    assert ok / total >= 0.95


# ── 接口契约 ─────────────────────────────────────────────────────────────────
def test_predict_rejects_unsupported_length(predictor: Predictor) -> None:
    mask = np.zeros((100, 300), dtype=np.uint8)
    with pytest.raises(ValueError, match="length"):
        predictor.predict_mask(mask, MAX_LENGTH + 1)
    with pytest.raises(ValueError, match="length"):
        predictor.predict_mask(mask, 0)


def test_predict_rejects_unknown_charset(predictor: Predictor) -> None:
    with pytest.raises(ValueError, match="字符集"):
        predictor.predict(np.zeros((100, 300), dtype=np.uint8), 6, "nope")


def test_predict_rejects_wrong_mask_shape(predictor: Predictor) -> None:
    with pytest.raises(ValueError, match="掩码尺寸"):
        predictor.predict_mask(np.zeros((50, 50), dtype=np.uint8), 6)


def test_blank_window_yields_empty_char(matcher: TemplateMatcher) -> None:
    from captcha_ocr.preprocess import GRID_H, GRID_W

    ch, margin, second = matcher.predict_window(np.zeros((GRID_H, GRID_W), np.float32))
    assert ch == "" and margin == 0.0 and second == ""


def test_prediction_margins_align_with_text(predictor: Predictor) -> None:
    if not REAL_SAMPLES:
        pytest.skip("缺少真实样本")
    s = REAL_SAMPLES[0]
    result = predictor.predict_mask(s.to_mask(), s.length, s.kind)
    assert len(result.text) == len(result.margins) == len(result.seconds) == s.length
    assert result.min_margin == min(result.margins)
