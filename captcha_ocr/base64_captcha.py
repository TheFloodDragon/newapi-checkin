#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`string_captcha` 字符图片验证码识别（Go base64Captcha DriverString）。

这是本包收录的第三套验证码，与 randomtool.cn（`generator`/`matcher`）、固定5位点阵
验证码（`newapi_bitmap`）互不相干。接口

    GET /api/captcha?scene=checkin → {captcha_id, image(dataURL), expires_in}

逆向与验收记录见 docs/captcha_algorithm.md。

## 一、为什么可以查表识别

`base64Captcha` 的 `item_char.go::drawText` 决定了字符只有三个自由度，且全部可穷举：

    fontSize = height*(rand(7)+7)/16   # height=40 → 仅 17/20/22/25/27/30/32 共 7 档
    x        = fontWidth*i + fontWidth/fontSize     # fontWidth = width/length
    y        = height/2 + fontSize/2 - rand(height/16*3)
    颜色      = RandDeepColor()，逐字符独立；字体 = 从 9 个内置 TTF 里随机取

**没有旋转、没有缩放形变**，所以「字符 × 字体 × 字号」的模板空间是有限的（digits
配置下 10×9×7 = 630 项），识别退化为查表。模板由 PIL 用同一批 TTF 离线渲染后打包成
`base64_templates.npz`，运行期只需 numpy。

## 二、分割靠「颜色方向」，而不是颜色本身

图像不是干净调色板：实测每张仅 184~308 个深色像素却有 124~227 种颜色，字形几乎全是
抗锯齿混合像素，取纯色只能拿到十几个点。但混合是线性的：

    P = a*C + (1-a)*U        # C 为字符色，U 为背景或其下的浅色噪点

于是 `BG - P` 与 `BG - C` **同向**，方向与覆盖率 a 无关 —— 按方向聚类即可分割，且
不必预先知道 C。噪点字符走 `RandLightColor()`（各通道 ∈[200,255)），|BG-P| 上限约
91，用 |BG-P| > 100 就能整体剥离。

## 三、打分：软 IoU + 最小二乘估色

把观测与模板都二值化再算 IoU 会丢掉抗锯齿信息（实测整图正确率仅 59%）。改为每个
摆位用最小二乘反解该字符的颜色深度 `depth = Σ(proj·T)/Σ(T²)`，把观测折算成覆盖率
`A = clip(proj/depth, 0, 1)`，再用软 IoU `Σmin(A,T)/Σmax(A,T)` 打分。

## 四、验收（30 张站点真实样本，人工标注）

见模块末尾的 `python -m captcha_ocr bench-base64`；当前实测数字记录在
docs/captcha_algorithm.md，不在此重复以免漂移。

`exact=False` 表示这次读数不够可信（某位得分过低或与次优字符差距过小），调用方应当
换一张验证码重试而不是硬提交 —— 取图不消耗签到机会，猜错却会。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
# ── 站点侧生成参数（实测 sheapi.top）─────────────────────────────────────────
WIDTH = 120
HEIGHT = 40
DEFAULT_LENGTH = 4
# base64Captcha 的字符池；该 fork 的 GraphicCaptchaCharset 三档对应关系。
#
# digits 取 "23456789" 而非 0-9：30 张真实样本的 117 个标注字符里 **0 与 1 一次都没
# 出现过**（若字符池含它们，均匀分布下期望约 23 次，概率约 1e-11），可以确定该 fork
# 排除了这两个易混淆字形。把它们放进字符集会实测掉准确率——'1' 会抢走 3/7/2 的位。
# letters/mixed 照抄 upstream 的 TxtAlphabet（已去掉 I/L/P/W/i/l/p/w），但缺少该档位
# 的真实样本，未经验证；换档位后请重跑 bench 再依赖。
CHARSETS: dict[str, str] = {
    "digits": "23456789",
    "letters": "ABCDEFGHJKMNOQRSTUVXYZabcdefghjkmnoqrstuvxyz",
    "mixed": "23456789ABCDEFGHJKMNOQRSTUVXYZabcdefghjkmnoqrstuvxyz",
}
DEFAULT_CHARSET = "digits"
# 该 fork 硬编码的背景色。BgColor 传 nil 时 base64Captcha 会每张随机浅色，
# 实测恒定 (250,252,255) 即证明是显式传入的。
BACKGROUND = (250, 252, 255)

# 内置 TTF 的文件名（base64Captcha fonts/ 目录，wqy-microhei.ttc 是中文字体，
# 数字/字母验证码不会用到，故不收录）。
FONT_NAMES = (
    "3Dumb.ttf",
    "ApothecaryFont.ttf",
    "Comismsh.ttf",
    "DENNEthree-dee.ttf",
    "DeborahFancyDress.ttf",
    "Flim-Flam.ttf",
    "RitaSmith.ttf",
    "actionj.ttf",
    "chromohv.ttf",
)
FONTS_URL = "https://github.com/mojocn/base64Captcha/tree/master/fonts"

TEMPLATES_PATH = Path(__file__).resolve().parent / "base64_templates.npz"
FONT_DIR = Path(__file__).resolve().parent / "fonts"

# ── 识别参数（全部由 30 张真实样本实测标定，改动请重跑 bench-base64）──────────
_BG = np.array(BACKGROUND, dtype=np.float64)
NOISE_MAX = 100.0      # RandLightColor 各通道 ∈[200,255) → |BG-P| ≤ 91，取 100 剥离
INLINE_MIN = 42.0      # 参与连通域的最低投影深度
COS_MIN = 0.99         # 方向聚类的余弦阈值
MAX_DIRECTIONS = 8
SHAPE_TOLERANCE = 5    # 观测包围盒与模板尺寸的允许偏差（像素）
PLACE_MARGIN = 3       # 观测窗口在包围盒外留的摆位余量
MIN_REGION_PIXELS = 10
MAX_GLYPH_W = 40
MAX_GLYPH_H = 38
DEPTH_MIN = 20.0       # 最小二乘反解的颜色深度下限，太浅说明摆位不对
# exact 判据：每位得分与「次优字符」的差距都要够大，否则换一张重试更划算。
EXACT_MIN_SCORE = 0.55
EXACT_MIN_MARGIN = 0.05


def font_sizes(height: int = HEIGHT) -> tuple[int, ...]:
    """`drawText` 的字号档位：height*(rand(7)+7)/16 —— 只有 7 种取值。"""
    return tuple(sorted({height * (i + 7) // 16 for i in range(7)}))


@dataclass(frozen=True)
class CaptchaResult:
    """识别结果。

    exact 为 True 表示每一位都足够可信；为 False 时调用方应换一张验证码重试
    （取图不消耗签到机会，猜错却会）。
    """

    text: str
    exact: bool
    detail: tuple[tuple[str, float, float], ...]  # 每位 (字符, 得分, 与次优字符的差距)


# ── 模板库：构建（需要 pillow）与加载（只需 numpy）─────────────────────────────


def _render_glyph(ch: str, size: int, font_path: Path) -> np.ndarray:
    """用 PIL 渲染单字符的灰度覆盖率（0~1），裁到内容包围盒。

    anchor="ls" = 左侧 + 基线，与 Go freetype 的 `Pt(x, y)` 语义一致；画布给足
    余量避免裁切。返回的是覆盖率而不是二值图 —— 抗锯齿信息是打分的关键。
    """
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), size)
    canvas = Image.new("L", (size * 4, size * 5), 255)
    ImageDraw.Draw(canvas).text((size, size * 3), ch, font=font, fill=0, anchor="ls")
    cov = (255 - np.asarray(canvas, dtype=np.float64)) / 255.0
    ys, xs = np.nonzero(cov > 0.02)
    if len(ys) == 0:
        return np.zeros((1, 1))
    return cov[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]


def build_templates(
    font_dir: Path | str = FONT_DIR,
    charset: str = DEFAULT_CHARSET,
    height: int = HEIGHT,
    path: Path | str = TEMPLATES_PATH,
) -> Path:
    """渲染并打包模板库（需要 pillow 与 base64Captcha 的 TTF）。

    字体不随仓库分发（第三方字体，许可未逐一确认）；请从 FONTS_URL 下载到
    captcha_ocr/fonts/ 后再执行 `python -m captcha_ocr base64-build`。
    运行期只需要打包好的 .npz，不需要字体。
    """
    font_dir = Path(font_dir)
    missing = [name for name in FONT_NAMES if not (font_dir / name).exists()]
    if missing:
        raise FileNotFoundError(
            f"缺少 base64Captcha 字体 {missing}；请从 {FONTS_URL} 下载到 {font_dir}"
        )
    chars = CHARSETS.get(charset, charset)
    sizes = font_sizes(height)

    glyphs: list[np.ndarray] = []
    meta: list[tuple[str, str, int]] = []
    for ch in chars:
        for name in FONT_NAMES:
            for size in sizes:
                glyphs.append(_render_glyph(ch, size, font_dir / name))
                meta.append((ch, name, size))

    max_h = max(g.shape[0] for g in glyphs)
    max_w = max(g.shape[1] for g in glyphs)
    # 覆盖率量化成 uint8 存盘：压缩后体积约为 float32 的 1/4，1/255 的精度损失
    # 在软 IoU 上不可测（实测同一批样本结论完全一致）。
    packed = np.zeros((len(glyphs), max_h, max_w), dtype=np.uint8)
    shapes = np.zeros((len(glyphs), 2), dtype=np.uint16)
    for index, glyph in enumerate(glyphs):
        packed[index, : glyph.shape[0], : glyph.shape[1]] = np.round(glyph * 255).astype(np.uint8)
        shapes[index] = glyph.shape

    path = Path(path)
    np.savez_compressed(
        path,
        glyphs=packed,
        shapes=shapes,
        chars=np.array([m[0] for m in meta]),
        fonts=np.array([m[1] for m in meta]),
        sizes=np.array([m[2] for m in meta], dtype=np.uint16),
        charset=np.array(charset),
        height=np.array(height, dtype=np.uint16),
    )
    return path


class Templates:
    """模板库（字符 × 字体 × 字号），懒加载并按尺寸建索引。"""

    def __init__(self, glyphs: list[np.ndarray], chars: list[str], fonts: list[str], sizes: list[int],
                 charset: str, height: int) -> None:
        self.glyphs = glyphs
        self.chars = chars
        self.fonts = fonts
        self.sizes = sizes
        self.charset = charset
        self.height = height
        # 预算 Σ(T²) 与形状，匹配时直接取用
        self.energy = [float((g * g).sum()) for g in glyphs]

    def __len__(self) -> int:
        return len(self.glyphs)

    @classmethod
    def load(cls, path: Path | str = TEMPLATES_PATH) -> Templates:
        with np.load(Path(path), allow_pickle=False) as data:
            packed = data["glyphs"]
            shapes = data["shapes"]
            glyphs = [
                packed[i, : int(shapes[i][0]), : int(shapes[i][1])].astype(np.float64) / 255.0
                for i in range(len(packed))
            ]
            return cls(
                glyphs,
                [str(c) for c in data["chars"]],
                [str(f) for f in data["fonts"]],
                [int(s) for s in data["sizes"]],
                str(data["charset"]),
                int(data["height"]),
            )

    def candidates(self, shape: tuple[int, int], tolerance: int = SHAPE_TOLERANCE) -> list[int]:
        """按包围盒尺寸粗筛候选模板下标 —— 没有形变，尺寸差太多必然不是同一个字。"""
        height, width = shape
        return [
            i for i, glyph in enumerate(self.glyphs)
            if abs(glyph.shape[0] - height) <= tolerance and abs(glyph.shape[1] - width) <= tolerance
        ]


@lru_cache(maxsize=None)
def _templates_for_path(path: Path) -> Templates:
    return Templates.load(path)


def templates(path: Path | str = TEMPLATES_PATH) -> Templates:
    """按规范化文件路径懒加载模板；不同模板库绝不能共享同一个全局实例。"""
    return _templates_for_path(Path(path).expanduser().resolve())


# ── 分割：按「颜色方向」聚类 ─────────────────────────────────────────────────


def ink_directions(image: np.ndarray, limit: int = MAX_DIRECTIONS) -> list[np.ndarray]:
    """把深色像素按 (BG-P) 的方向聚类，返回若干单位方向向量。

    从最深的像素开始聚类：它们的 alpha 接近 1，方向最接近真实字符色，用它当簇心
    比从任意像素起步稳得多。簇心按深度加权更新，避免被大量低 alpha 像素带偏。
    """
    diff = _BG - image.astype(np.float64)
    magnitude = np.linalg.norm(diff, axis=-1)
    ys, xs = np.nonzero(magnitude > NOISE_MAX)
    if len(ys) == 0:
        return []
    centers: list[np.ndarray] = []
    weights: list[float] = []
    for index in np.argsort(-magnitude[ys, xs]):
        y, x = ys[index], xs[index]
        weight = float(magnitude[y, x])
        vector = diff[y, x] / weight
        for slot, center in enumerate(centers):
            if float(vector @ center) >= COS_MIN:
                merged = center * weights[slot] + vector * weight
                centers[slot] = merged / np.linalg.norm(merged)
                weights[slot] += weight
                break
        else:
            if len(centers) < limit:
                centers.append(vector.copy())
                weights.append(weight)
    return centers


def connected_components(mask: np.ndarray, min_size: int = 6) -> list[np.ndarray]:
    """8 邻域连通域（洪水填充）。

    图只有 120×40，纯 Python 遍历也在毫秒级，不值得为此引入 scipy。连通域的作用
    是把同色相里相邻的两个字符拆开 —— 只按颜色分割会把它们并成一坨。
    """
    height, width = mask.shape
    seen = np.zeros_like(mask)
    out: list[np.ndarray] = []
    for y0 in range(height):
        for x0 in range(width):
            if not mask[y0, x0] or seen[y0, x0]:
                continue
            stack = [(y0, x0)]
            seen[y0, x0] = True
            points: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                points.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = y + dy, x + dx
                        if 0 <= ny < height and 0 <= nx < width and mask[ny, nx] and not seen[ny, nx]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
            if len(points) < min_size:
                continue
            component = np.zeros_like(mask)
            for y, x in points:
                component[y, x] = True
            out.append(component)
    return out


# ── 打分：软 IoU + 最小二乘估色 ──────────────────────────────────────────────


def match_score(observed: np.ndarray, glyph: np.ndarray, energy: float) -> float:
    """观测窗口与模板的最佳软 IoU（在所有可行摆位里取最大）。

    observed 是「沿该字符颜色方向的投影深度」，尺度未知（取决于 RandDeepColor 的
    深浅）；因此每个摆位先用最小二乘反解深度 depth = Σ(proj·T)/Σ(T²)，把观测折算
    成覆盖率 A = clip(proj/depth, 0, 1)，再算 Σmin(A,T)/Σmax(A,T)。

    摆位循环用 sliding_window_view 一次性向量化：逐位置 Python 循环在 630 个候选上
    要 2.8 s/张，向量化后同样结果只需几十毫秒。
    """
    gh, gw = glyph.shape
    oh, ow = observed.shape
    if energy <= 1e-6 or oh < gh or ow < gw:
        return 0.0
    windows = np.lib.stride_tricks.sliding_window_view(observed, (gh, gw))
    depth = (windows * glyph).sum(axis=(-1, -2)) / energy
    valid = depth > DEPTH_MIN
    if not valid.any():
        return 0.0
    # 无效摆位的 depth 置 1 只为避免除零；它们的得分随后被 where 屏蔽。
    safe = np.where(valid, depth, 1.0)
    coverage = np.clip(windows / safe[..., None, None], 0.0, 1.0)
    intersection = np.minimum(coverage, glyph).sum(axis=(-1, -2))
    union = np.maximum(coverage, glyph).sum(axis=(-1, -2))
    scores = np.where(valid & (union > 0), intersection / np.where(union > 0, union, 1.0), 0.0)
    return float(scores.max())


def _best_char(observed: np.ndarray, shape: tuple[int, int], table: Templates) -> tuple[str, float, float]:
    """返回 (最佳字符, 得分, 与次优字符的差距)。"""
    best: dict[str, float] = {}
    for index in table.candidates(shape):
        score = match_score(observed, table.glyphs[index], table.energy[index])
        char = table.chars[index]
        if score > best.get(char, 0.0):
            best[char] = score
    if not best:
        return "", 0.0, 0.0
    ranked = sorted(best.items(), key=lambda item: -item[1])
    char, score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    return char, score, score - runner_up


# ── 对外识别入口 ─────────────────────────────────────────────────────────────


def solve_array(image: np.ndarray, length: int = DEFAULT_LENGTH,
                path: Path | str = TEMPLATES_PATH) -> CaptchaResult:
    """RGB ndarray → 识别结果。

    逐槽识别：`drawText` 里第 i 个字符的起点固定为 `width/length*i + 小偏移`，所以
    槽位是已知的强约束。每槽在所有颜色方向里挑得分最高的那个 —— 相邻字符色相偶尔
    接近，让候选竞争比预先绑定颜色更稳。
    """
    table = templates(path)
    height, width = image.shape[0], image.shape[1]
    slot_width = max(1, width // max(1, length))
    diff = _BG - image.astype(np.float64)

    layers: list[tuple[np.ndarray, list[np.ndarray]]] = []
    for direction in ink_directions(image):
        projection = diff @ direction
        residual = np.linalg.norm(diff - projection[..., None] * direction, axis=-1)
        # inline：确定属于该颜色的像素，用来找连通域（严格）
        inline = (projection > INLINE_MIN) & (residual <= 0.18 * projection + 12.0)
        # clean：用于打分的投影强度，容差放宽以保住抗锯齿边缘（宽松）
        clean = np.where((residual <= 0.25 * projection + 16.0) & (projection > 0), projection, 0.0)
        components = connected_components(inline)
        if components:
            layers.append((clean, components))

    chars: list[str] = []
    detail: list[tuple[str, float, float]] = []
    exact = True
    for slot in range(length):
        low, high = slot_width * slot - 6, slot_width * slot + 14
        best: tuple[float, str, float] | None = None
        for clean, components in layers:
            picked = [c for c in components if low <= int(np.nonzero(c)[1].min()) <= high]
            if not picked:
                continue
            region = np.logical_or.reduce(picked)
            ys, xs = np.nonzero(region)
            box = (int(ys.max() - ys.min() + 1), int(xs.max() - xs.min() + 1))
            if box[0] > MAX_GLYPH_H or box[1] > MAX_GLYPH_W or region.sum() < MIN_REGION_PIXELS:
                continue
            y0, y1 = max(0, ys.min() - PLACE_MARGIN), min(height, ys.max() + 1 + PLACE_MARGIN)
            x0, x1 = max(0, xs.min() - PLACE_MARGIN), min(width, xs.max() + 1 + PLACE_MARGIN)
            char, score, margin = _best_char(clean[y0:y1, x0:x1], box, table)
            if char and (best is None or score > best[0]):
                best = (score, char, margin)
        if best is None:
            chars.append("")
            detail.append(("", 0.0, 0.0))
            exact = False
            continue
        score, char, margin = best
        chars.append(char)
        detail.append((char, score, margin))
        if score < EXACT_MIN_SCORE or margin < EXACT_MIN_MARGIN:
            exact = False
    return CaptchaResult(text="".join(chars), exact=exact, detail=tuple(detail))


def solve_bytes(data: bytes, length: int = DEFAULT_LENGTH,
                path: Path | str = TEMPLATES_PATH) -> CaptchaResult:
    """PNG 字节流 → 识别结果（需要 pillow 解码）。"""
    import io

    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 取决于环境
        raise RuntimeError(
            "解码验证码 PNG 需要 pillow。请执行 uv sync --extra dev，"
            "或自行解码后调用 solve_array（仅需 numpy）。"
        ) from exc
    with Image.open(io.BytesIO(data)) as image:
        return solve_array(np.asarray(image.convert("RGB")), length, path)


def solve_data_url(data_url: str, length: int = DEFAULT_LENGTH,
                   path: Path | str = TEMPLATES_PATH) -> CaptchaResult:
    """`data:image/png;base64,...` → 识别结果（接口直接返回这种形态）。"""
    import base64

    payload = data_url.split(",", 1)[1] if "," in data_url else data_url
    return solve_bytes(base64.b64decode(payload), length, path)


__all__ = [
    "BACKGROUND",
    "CHARSETS",
    "DEFAULT_CHARSET",
    "DEFAULT_LENGTH",
    "FONTS_URL",
    "FONT_NAMES",
    "HEIGHT",
    "TEMPLATES_PATH",
    "WIDTH",
    "CaptchaResult",
    "Templates",
    "build_templates",
    "connected_components",
    "font_sizes",
    "ink_directions",
    "match_score",
    "solve_array",
    "solve_bytes",
    "solve_data_url",
    "templates",
]
