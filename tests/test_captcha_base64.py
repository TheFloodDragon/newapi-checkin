# -*- coding: utf-8 -*-
"""base64Captcha 系图形验证码识别回归（sheapi.top 的签到验证码）。

真值来自站点真实输出，30 张 PNG 以 base64 存在 tests/data/base64_captcha_samples.json。
标签里的 `?` 表示该位被浅色幽灵字符覆盖、人工无法确认 —— 这类位不参与判分，含 `?`
的整图也不计入整图正确率（拿不到可信真值的样本不该左右结论）。

准确率断言故意留有余量：它锁的是「不许回退」，不是追求某个漂亮数字。当前实测值见
docs/captcha_algorithm.md；真正的安全阀是 exact —— 只要 exact=True 就必须正确，
错一个都算回归，因为签到链路正是靠它决定「提交还是换一张」。
"""

from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import numpy as np
import pytest

from captcha_ocr import base64_captcha as BC

SAMPLE_PATH = Path(__file__).parent / "data" / "base64_captcha_samples.json"
_PAYLOAD = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))
SAMPLES: tuple[tuple[str, str], ...] = tuple(
    (s["label"], s["png_base64"]) for s in _PAYLOAD["samples"]
)
CHAR_COUNT = int(_PAYLOAD["char_count"])

pytestmark = pytest.mark.skipif(
    not BC.TEMPLATES_PATH.exists(),
    reason="缺少 base64_templates.npz（用 python -m captcha_ocr build-base64-templates 重建）",
)


def _decode(b64: str) -> np.ndarray:
    pytest.importorskip("PIL", reason="解码 PNG 需要 pillow（dev 依赖）")
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(b64))) as image:
        return np.asarray(image.convert("RGB"))


@pytest.fixture(scope="module")
def results() -> list[tuple[str, BC.CaptchaResult]]:
    pytest.importorskip("PIL", reason="解码 PNG 需要 pillow（dev 依赖）")
    return [(label, BC.solve_array(_decode(b64))) for label, b64 in SAMPLES]


# ── 图像与生成参数 ───────────────────────────────────────────────────────────
def test_samples_match_documented_geometry() -> None:
    """尺寸/字符数是分割的前置假设，样本一旦换形态必须先改常量。"""
    assert (_PAYLOAD["width"], _PAYLOAD["height"]) == (BC.WIDTH, BC.HEIGHT)
    assert CHAR_COUNT == BC.DEFAULT_LENGTH


def test_font_sizes_are_the_seven_upstream_steps() -> None:
    """drawText 的字号是 height*(rand(7)+7)/16，只有 7 档；模板空间靠它有限。"""
    assert BC.font_sizes(40) == (17, 20, 22, 25, 27, 30, 32)
    assert len(BC.font_sizes(40)) == 7


def test_digit_charset_excludes_confusable_zero_and_one() -> None:
    """0/1 在 117 个标注字符里一次都没出现；放进字符集实测会掉准确率。"""
    assert "0" not in BC.CHARSETS["digits"]
    assert "1" not in BC.CHARSETS["digits"]
    labels = "".join(label for label, _ in SAMPLES).replace("?", "")
    assert set(labels) <= set(BC.CHARSETS["digits"])


def test_templates_cover_charset_font_and_size_grid() -> None:
    table = BC.templates()
    assert len(table) == len(BC.CHARSETS[table.charset]) * len(BC.FONT_NAMES) * len(BC.font_sizes(table.height))
    assert set(table.chars) == set(BC.CHARSETS[table.charset])


# ── 准确率 ───────────────────────────────────────────────────────────────────
def test_per_character_accuracy_does_not_regress(results) -> None:
    ok = total = 0
    for label, result in results:
        text = result.text.ljust(len(label))
        for want, got in zip(label, text):
            if want == "?":
                continue
            total += 1
            ok += int(want == got)
    assert total >= 100, "样本量太小，准确率结论不可靠"
    assert ok / total >= 0.93, f"单字符准确率退化到 {ok}/{total}"


def test_whole_image_accuracy_does_not_regress(results) -> None:
    clean = [(label, result) for label, result in results if "?" not in label]
    ok = sum(int(result.text == label) for label, result in clean)
    assert ok / len(clean) >= 0.85, f"整图准确率退化到 {ok}/{len(clean)}"


def test_exact_results_are_always_correct(results) -> None:
    """exact 是签到链路的安全阀：它说「可信」就必须真的对。

    错一个都算回归 —— 一旦 exact 会误报，链路就会拿错答案去提交，而验证码
    每次提交都会作废，等于白扔一次机会。
    """
    wrong = [
        (label, result.text)
        for label, result in results
        if result.exact and "?" not in label and result.text != label
    ]
    assert wrong == [], f"exact=True 却识别错误：{wrong}"


def test_exact_pass_rate_is_high_enough_for_retries(results) -> None:
    """通过率决定「换一张重试」要试几次；低于一半就该重新标定阈值。"""
    clean = [result for label, result in results if "?" not in label]
    passed = sum(int(result.exact) for result in clean)
    assert passed / len(clean) >= 0.55, f"exact 通过率仅 {passed}/{len(clean)}，重试成本过高"


# ── 结构与降级 ───────────────────────────────────────────────────────────────
def test_result_detail_reports_score_and_margin(results) -> None:
    _label, result = results[0]
    assert len(result.detail) == CHAR_COUNT
    for char, score, margin in result.detail:
        assert char in BC.CHARSETS["digits"]
        assert 0.0 <= score <= 1.0
        assert margin >= 0.0


def test_blank_image_is_not_reported_as_exact() -> None:
    """全背景图不该被当成「识别成功」，否则链路会提交空答案。"""
    blank = np.full((BC.HEIGHT, BC.WIDTH, 3), BC.BACKGROUND, dtype=np.uint8)
    result = BC.solve_array(blank)
    assert result.exact is False
    assert result.text.strip("") == ""


def test_data_url_and_bytes_entrypoints_agree() -> None:
    pytest.importorskip("PIL", reason="解码 PNG 需要 pillow（dev 依赖）")
    label, b64 = SAMPLES[0]
    from_bytes = BC.solve_bytes(base64.b64decode(b64))
    from_url = BC.solve_data_url(f"data:image/png;base64,{b64}")
    assert from_bytes.text == from_url.text == label
