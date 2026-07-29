#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""randomtool.cn 验证码生成器的本地精确复刻。

绘制参数由浏览器端 hook CanvasRenderingContext2D 录得（4 次独立采样完全一致）：

- 画布 300x100，纯白底 `#ffffff`
- 50 个 r=1 圆形噪点，浅色
- 7 条干扰线，浅色
- 每字符：save → translate(x, 50) → rotate(±0.35 rad) → fillText(ch, 0, 0) → restore
  字体固定 `bold 40px Arial`，填充为深色
- 字符 x 坐标严格等距：x_i = start + 35 * i，start 见 char_positions()
- **textBaseline = "middle"**（非默认 alphabetic）：translate 的 y=50 是字形垂直
  中心，基线在其下方 (fontAscent-fontDescent)/2 = (36-8)/2 = 14px 处。
  旋转中心同样是这个 translate 点，不是基线点。

颜色分层是本方案的关键前提：字符 RGB 各分量落在 [0x30, 0x95]，
干扰元素落在 [0x96, 0xdb]，两者不重叠，因此灰度阈值可干净分离前景。

pillow 只在真正渲染时才导入（见 _pil()）：本模块的常量与 char_positions()
被推理路径（preprocess/matcher）引用，而运行期只装 numpy、不装 pillow。
模块级 import PIL 会让纯推理路径直接 ImportError。
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any

WIDTH = 300
HEIGHT = 100
# 站点 translate 的 y 值。因 textBaseline="middle"，它是字形垂直中心 + 旋转中心，
# 不是基线。命名刻意避开 BASELINE_Y —— 早期误当基线处理，导致所有字形高 14px。
ANCHOR_Y = 50
CHAR_STEP = 35
FONT_SIZE = 40
MAX_ROTATION = 0.35
# textBaseline="middle" 锚点到基线的距离，浏览器实测（见 middle_to_baseline()）。
MIDDLE_TO_BASELINE = 11
NOISE_DOTS = 50
NOISE_LINES = 7

# 站点字符集（已剔除易混淆字符 0/O/o、1/l/I）
CHARSET_NUMBER = "0123456789"
CHARSET_ALPHA = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz"
CHARSET_MIXED = "23456789" + CHARSET_ALPHA
CHARSET_COMPLEX = "!#$%&*@^" + CHARSET_MIXED

CHARSETS = {
    "number": CHARSET_NUMBER,
    "alpha": CHARSET_ALPHA,
    "mixed": CHARSET_MIXED,
    "complex": CHARSET_COMPLEX,
}

# 前景/背景色区间（录制观测值）
FG_RANGE = (0x30, 0x95)
BG_RANGE = (0x96, 0xDB)


def char_positions(n: int) -> list[int]:
    """返回 n 个字符的 x 坐标，复刻站点的等距布局。

    实测：n=4 → 80,115,150,185；n=6 → 45,80,...,220；n=8 → 20,55,...,265。
    即整体居中（步长恒为 35），但 start 有 20px 下限——这也是站点的 bug 来源：
    n>8 时最后几个字符的 x 会超出画布宽度（n=12 时达 405），根本画不出来，
    因此实用长度上限是 8。
    """
    start = max(20, (WIDTH - CHAR_STEP * n) // 2)
    return [start + CHAR_STEP * i for i in range(n)]


def _rand_color(rng: random.Random, lo: int, hi: int) -> tuple[int, int, int]:
    return (rng.randint(lo, hi), rng.randint(lo, hi), rng.randint(lo, hi))


def _pil():
    """延迟导入 pillow，缺失时给出可操作的提示。

    生成样本与构建模板库需要 pillow（dev 依赖）；纯推理路径只用 numpy，
    因此不能在模块级 import。
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "生成验证码需要 pillow（仅 dev 依赖）。请执行：uv sync --extra dev"
        ) from exc
    return Image, ImageDraw, ImageFont


@dataclass
class Sample:
    image: Any  # PIL.Image.Image；此处不标注以免模块级引入 pillow
    text: str


def _load_font():
    """加载 Arial Bold —— 站点固定用它，换字体会让字形与真实样本不一致。"""
    _, _, ImageFont = _pil()
    for path in (
        "C:/Windows/Fonts/arialbd.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, FONT_SIZE)
        except OSError:
            continue
    raise RuntimeError("未找到可用的粗体 TrueType 字体，无法复刻站点字形")


_FONT = None


def get_font():
    global _FONT
    if _FONT is None:
        _FONT = _load_font()
    return _FONT


def middle_to_baseline() -> float:
    """textBaseline="middle" 的锚点到字形基线的垂直距离（像素）。

    这是**浏览器实测值，不是公式推导值**。在真实 canvas 上用同一字体分别以
    "alphabetic" 和 "middle" 画 'H'，两者 ink 底边相差 11px：

        alphabetic 于 y=100 → ink 71..99（基线 100）
        middle     于 y=100 → ink 82..110（基线 111）

    三处独立证据一致指向 11：上述直接测量、真实样本 ink 行质心统计（11.8）、
    以及模板匹配的偏移扫描（最优 dy 对应 11）。

    刻意不用 (fontAscent-fontDescent)/2 这类公式：按当时读到的 36/8 算出 14，
    比实际大 3px，真实样本准确率从 99.6% 掉到 93.9%（更早整体按基线锚点绘制、
    偏差 14px 时更只有 4.55%）。浏览器对 "middle" 用的是 em 盒中心，其 ascent/
    descent 拆分并不等于 measureText 报告的 fontBoundingBox* 值。

    换字体或字号后必须重新测量（脚本见 docs/captcha_algorithm.md）。
    """
    return MIDDLE_TO_BASELINE


def draw_glyph(ch: str, angle: float, color: tuple[int, int, int] | int):
    """把单字符渲染到透明图层，返回 (图层, 锚点在图层内的坐标)。

    严格复刻站点的变换顺序 translate(x, ANCHOR_Y) → rotate(angle) → fillText：
    旋转中心是**锚点**而非基线，两者相差 14px，用错会让字形整体绕偏。
    generator 与模板构建共用此函数，避免两侧渲染出现任何差异。
    """
    Image, ImageDraw, _ = _pil()
    pad = FONT_SIZE * 4  # 4 倍字号：容纳最宽字形 + 旋转外扩，避免裁切
    anchor_xy = (pad // 2, pad // 2)
    mode = "RGBA" if isinstance(color, tuple) else "L"
    fill = (*color, 255) if isinstance(color, tuple) else color
    layer = Image.new(mode, (pad, pad), (0, 0, 0, 0) if isinstance(color, tuple) else 0)
    # 锚点在中心，基线位于锚点下方 middle_to_baseline()
    ImageDraw.Draw(layer).text(
        (anchor_xy[0], anchor_xy[1] + middle_to_baseline()),
        ch, font=get_font(), fill=fill, anchor="ls",
    )
    if angle:
        # PIL 逆时针 / canvas 顺时针，取负号；绕锚点旋转
        layer = layer.rotate(-angle * 180 / math.pi, resample=Image.BICUBIC, center=anchor_xy)
    return layer, anchor_xy


def render(text: str, rng: random.Random | None = None):
    """按站点算法渲染一张验证码图，返回 PIL.Image。"""
    Image, ImageDraw, _ = _pil()
    rng = rng or random.Random()
    img = Image.new("RGB", (WIDTH, HEIGHT), (0xFF, 0xFF, 0xFF))
    draw = ImageDraw.Draw(img)

    # 噪点（浅色，r=1 → 直径 2px）
    for _ in range(NOISE_DOTS):
        x = rng.randint(0, WIDTH - 1)
        y = rng.randint(0, HEIGHT - 1)
        draw.ellipse([x - 1, y - 1, x + 1, y + 1], fill=_rand_color(rng, *BG_RANGE))

    # 干扰线（浅色，lineWidth=1）
    for _ in range(NOISE_LINES):
        draw.line(
            [rng.randint(0, WIDTH), rng.randint(0, HEIGHT),
             rng.randint(0, WIDTH), rng.randint(0, HEIGHT)],
            fill=_rand_color(rng, *BG_RANGE),
            width=1,
        )

    # 字符：各自独立旋转后贴回，等价于 canvas 的 translate+rotate+fillText
    for x, ch in zip(char_positions(len(text)), text):
        angle = rng.uniform(-MAX_ROTATION, MAX_ROTATION)
        layer, (ax, ay) = draw_glyph(ch, angle, _rand_color(rng, *FG_RANGE))
        # 贴回时让图层内的锚点落在画布的 (x, ANCHOR_Y)
        img.paste(layer, (x - ax, ANCHOR_Y - ay), layer)

    return img


def sample(length: int = 6, kind: str = "mixed", rng: random.Random | None = None) -> Sample:
    """随机生成一条样本（文本 + 图像）。

    rng 可传入固定种子的 Random 以复现同一张图，便于测试与调试。
    """
    rng = rng or random.Random()
    charset = CHARSETS[kind]
    text = "".join(rng.choice(charset) for _ in range(length))
    return Sample(image=render(text, rng), text=text)


def iter_samples(count: int, length: int = 6, kind: str = "mixed", seed: int | None = None):
    """批量生成样本；seed 固定时结果可复现（训练/评测集构建用）。"""
    rng = random.Random(seed)
    for _ in range(count):
        yield sample(length, kind, rng)


if __name__ == "__main__":
    import sys

    out = sys.argv[1] if len(sys.argv) > 1 else "_demo.png"
    s = sample()
    s.image.save(out)
    print(f"{s.text} -> {out}")
