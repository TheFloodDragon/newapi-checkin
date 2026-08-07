"""GoCaptcha click-shape 离线求解器。

算法依据 GoCaptcha v2 的公开生成不变量：
- 形状来自 go-captcha-assets 的 13 张固定模板；
- 主图颜色来自固定 7 色调色板；
- thumb 与主图对同一目标使用同一 Shape / Angle / Size，仅颜色不同；
- 同一张主图中的 Shape 不重复。

流程：thumb 每格对 13 模板做「主色覆盖 + Canny 边缘」联合拟合；主图仅从
官方调色板提取候选轮廓；最后对 (目标, 主图候选, 模板ID) 做一对一全局最优分配。
不依赖在线 OCR，也不需要浏览器。
"""
from __future__ import annotations

import base64
import io
from collections import Counter
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

# GoCaptcha v2/click/default.go 的主图与缩略图默认调色板。
_MAIN_PALETTE = (
    (253, 233, 142),
    (96, 193, 255),
    (252, 176, 142),
    (251, 136, 255),
    (180, 254, 212),
    (203, 250, 169),
    (120, 214, 248),
)
_THUMB_PALETTE = (
    (31, 85, 196),
    (120, 5, 146),
    (47, 107, 0),
    (145, 0, 0),
    (134, 68, 1),
    (103, 89, 1),
    (1, 110, 92),
)
_ANGLES = (*range(20, 61, 3), *range(290, 331, 3))
_TEMPLATE_DIR = Path(__file__).with_name("go_captcha_shapes")


def available() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return all((_TEMPLATE_DIR / f"shape_{i}.png").is_file() for i in range(1, 14))


def _cv2() -> Any:
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError(
            "click-shape 求解需要 opencv-python-headless；请执行 uv sync 后重试"
        ) from exc
    return cv2


def _decode_image(value: str) -> np.ndarray:
    payload = value.split(",", 1)[1] if "," in value else value
    with Image.open(io.BytesIO(base64.b64decode(payload))) as image:
        return np.asarray(image.convert("RGB"))


def _contour(mask: np.ndarray) -> tuple[Any, int]:
    cv2 = _cv2()
    contours, hierarchy = cv2.findContours(
        mask.astype("uint8") * 255, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
    )
    if not contours:
        raise ValueError("没有可识别轮廓")
    index = max(range(len(contours)), key=lambda i: cv2.contourArea(contours[i]))
    holes = 0
    if hierarchy is not None:
        child = int(hierarchy[0][index][2])
        while child >= 0:
            holes += 1
            child = int(hierarchy[0][child][0])
    return contours[index], holes


@lru_cache(maxsize=1)
def _templates() -> dict[int, tuple[np.ndarray, Any, int]]:
    cv2 = _cv2()
    result: dict[int, tuple[np.ndarray, Any, int]] = {}
    for shape_id in range(1, 14):
        image = cv2.imread(
            str(_TEMPLATE_DIR / f"shape_{shape_id}.png"), cv2.IMREAD_UNCHANGED
        )
        if image is None or image.ndim != 3 or image.shape[2] < 4:
            raise RuntimeError(f"无法读取 GoCaptcha 模板 shape_{shape_id}.png")
        alpha = image[:, :, 3]
        contour, holes = _contour(alpha > 40)
        result[shape_id] = (alpha, contour, holes)
    return result


def _place(mask: np.ndarray, height: int, width: int, cx: float, cy: float) -> np.ndarray:
    size_h, size_w = mask.shape
    x0 = round(cx - size_w / 2)
    y0 = round(cy - size_h / 2)
    output = np.zeros((height, width), dtype=bool)
    xa, ya = max(0, x0), max(0, y0)
    xb, yb = min(width, x0 + size_w), min(height, y0 + size_h)
    if xb > xa and yb > ya:
        output[ya:yb, xa:xb] = mask[ya - y0 : yb - y0, xa - x0 : xb - x0]
    return output


def _render(alpha: np.ndarray, size: int, angle: int) -> np.ndarray:
    cv2 = _cv2()
    scaled = cv2.resize(alpha, (size, size), interpolation=cv2.INTER_AREA)
    matrix = cv2.getRotationMatrix2D((size / 2, size / 2), angle, 1)
    return cv2.warpAffine(
        scaled, matrix, (size, size), flags=cv2.INTER_LINEAR, borderValue=0
    ) > 80


def _cell_observation(cell: np.ndarray) -> tuple[np.ndarray, float, float]:
    """返回目标主色 mask 与厚实核心质心；细干扰线不会左右中心。"""
    cv2 = _cv2()
    counts = Counter(map(tuple, cell.reshape(-1, 3)))
    palette = np.asarray(_THUMB_PALETTE, dtype=int)
    # 只接受 GoCaptcha thumb 调色板附近的颜色，避免把黑背景/干扰背景当主体。
    ranked: list[tuple[int, tuple[int, int, int]]] = []
    for color, count in counts.most_common(30):
        distance = np.max(np.abs(palette - np.asarray(color, dtype=int)), axis=1).min()
        if distance <= 18:
            ranked.append((count, color))
    if not ranked:
        raise ValueError("目标格未检测到 GoCaptcha 主色")
    color = np.asarray(max(ranked)[1], dtype=int)
    observed = np.max(np.abs(cell.astype(int) - color), axis=2) <= 16
    density = cv2.boxFilter(
        observed.astype("uint8"), cv2.CV_16S, (5, 5), normalize=False,
        borderType=cv2.BORDER_CONSTANT,
    )
    core = observed & (density >= 10)
    if int(core.sum()) < 20:
        raise ValueError("目标格有效形状像素过少")
    cy, cx = np.argwhere(core).mean(axis=0)
    return observed, float(cy), float(cx)


def _target_scores(cell: np.ndarray) -> dict[int, float]:
    """目标格对 13 模板的主色/边缘联合拟合分。"""
    cv2 = _cv2()
    observed, cy, cx = _cell_observation(cell)
    edge = np.zeros(cell.shape[:2], dtype="uint8")
    for channel in range(3):
        edge = np.maximum(edge, cv2.Canny(cell[:, :, channel], 40, 100))
    edge = cv2.dilate(edge, np.ones((3, 3), dtype="uint8")).astype(bool)

    scores: dict[int, float] = {}
    for shape_id, (alpha, _, _) in _templates().items():
        best_color = 0.0
        best_edge = 0.0
        for size in range(22, 33, 2):
            for angle in _ANGLES:
                shape = _render(alpha, size, angle)
                boundary = cv2.morphologyEx(
                    shape.astype("uint8"), cv2.MORPH_GRADIENT,
                    np.ones((3, 3), dtype="uint8"),
                ).astype(bool)
                for oy in range(-3, 4, 2):
                    for ox in range(-3, 4, 2):
                        placed = _place(shape, cell.shape[0], cell.shape[1], cx + ox, cy + oy)
                        intersection = int(np.sum(placed & observed))
                        if placed.any():
                            cover = intersection / int(placed.sum())
                            dice = 2 * intersection / (int(placed.sum()) + int(observed.sum()))
                            best_color = max(best_color, 0.65 * cover + 0.35 * dice)

                        placed_boundary = _place(
                            boundary, cell.shape[0], cell.shape[1], cx + ox, cy + oy
                        )
                        if placed_boundary.any():
                            best_edge = max(
                                best_edge,
                                int(np.sum(placed_boundary & edge))
                                / int(placed_boundary.sum()),
                            )
        # 调和平均惩罚「只吻合颜色或只吻合干扰线」的偏科模板。
        scores[shape_id] = (
            2 * best_color * best_edge / (best_color + best_edge + 1e-9)
        )
    return scores


def _palette_candidates(main: np.ndarray) -> list[tuple[float, float, Any, int]]:
    """仅从 GoCaptcha 固定主图调色板提取形状候选，排除复杂背景假边缘。"""
    cv2 = _cv2()
    found: list[tuple[float, float, Any, int]] = []
    for color in _MAIN_PALETTE:
        mask = (
            np.max(np.abs(main.astype(int) - np.asarray(color, dtype=int)), axis=2) <= 22
        ).astype("uint8")
        mask = cv2.morphologyEx(
            mask, cv2.MORPH_CLOSE, np.ones((5, 5), dtype="uint8")
        )
        contours, hierarchy = cv2.findContours(
            mask * 255, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE
        )
        for index, contour in enumerate(contours):
            area = cv2.contourArea(contour)
            x, y, width, height = cv2.boundingRect(contour)
            if not (40 <= area <= 1600 and 12 <= width <= 50 and 12 <= height <= 50):
                continue
            moments = cv2.moments(contour)
            cx = moments["m10"] / moments["m00"] if moments["m00"] else x + width / 2
            cy = moments["m01"] / moments["m00"] if moments["m00"] else y + height / 2
            holes = 0
            if hierarchy is not None:
                child = int(hierarchy[0][index][2])
                while child >= 0:
                    holes += 1
                    child = int(hierarchy[0][child][0])
            found.append((float(cx), float(cy), contour, holes))

    # JPEG 容差可能让相邻调色板各提取一次；同中心只保留面积最大的轮廓。
    deduped: list[tuple[float, float, Any, int]] = []
    for item in sorted(found, key=lambda value: cv2.contourArea(value[2]), reverse=True):
        if not any(
            (item[0] - old[0]) ** 2 + (item[1] - old[1]) ** 2 < 49
            for old in deduped
        ):
            deduped.append(item)
    return deduped


def _detect_target_count(thumb: np.ndarray) -> int | None:
    """用厚实主色组件的 X 聚类推断目标数（GoCaptcha 默认 2–4）。

    干扰细线会把一个形状切成上下数块，但这些块的 X 中心基本一致；按颜色取
    5×5 密度核心后，将相距 <=12px 的 X 合并并累计面积即可恢复目标格数量。
    """
    cv2 = _cv2()
    pieces: list[tuple[float, float]] = []
    for color in _THUMB_PALETTE:
        raw = (
            np.max(np.abs(thumb.astype(int) - np.asarray(color, dtype=int)), axis=2) <= 16
        ).astype("uint8")
        density = cv2.boxFilter(
            raw, cv2.CV_16S, (5, 5), normalize=False,
            borderType=cv2.BORDER_CONSTANT,
        )
        core = (raw & (density >= 10)).astype("uint8")
        contours, _ = cv2.findContours(
            core * 255, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        for contour in contours:
            area = cv2.contourArea(contour)
            _, _, width, height = cv2.boundingRect(contour)
            if area < 30 or width < 8 or height < 8:
                continue
            moments = cv2.moments(contour)
            if not moments["m00"]:
                continue
            pieces.append((moments["m10"] / moments["m00"], float(area)))

    clusters: list[list[float]] = []  # [加权X总和, 面积总和]
    for x, area in sorted(pieces):
        if clusters and abs(x - clusters[-1][0] / clusters[-1][1]) <= 12:
            clusters[-1][0] += x * area
            clusters[-1][1] += area
        else:
            clusters.append([x * area, area])
    strong = [cluster for cluster in clusters if cluster[1] >= 120]
    return len(strong) if 2 <= len(strong) <= 4 else None


def _solve_for_count(
    main: np.ndarray, thumb: np.ndarray, count: int
) -> tuple[float, list[tuple[int, int]], list[tuple[int, int]], list[float]]:
    cv2 = _cv2()
    width = thumb.shape[1] / count
    target_scores = []
    for index in range(count):
        left = round(index * width)
        right = round((index + 1) * width)
        target_scores.append(_target_scores(thumb[:, left:right]))

    candidates = _palette_candidates(main)
    if len(candidates) < count:
        raise ValueError(f"主图只检测到 {len(candidates)} 个调色板形状，少于目标数 {count}")

    candidate_distance: list[dict[int, float]] = []
    for _, _, contour, holes in candidates:
        distances: dict[int, float] = {}
        for shape_id, (_, template_contour, template_holes) in _templates().items():
            distances[shape_id] = cv2.matchShapes(
                contour, template_contour, cv2.CONTOURS_MATCH_I1, 0
            ) + abs(holes - template_holes) * 0.3
        candidate_distance.append(distances)

    options: list[list[tuple[float, int, int, float, float]]] = []
    for target_index in range(count):
        values = []
        for candidate_index in range(len(candidates)):
            for shape_id in range(1, 14):
                fit_score = target_scores[target_index][shape_id]
                distance = candidate_distance[candidate_index][shape_id]
                values.append(
                    (fit_score - distance, candidate_index, shape_id, fit_score, distance)
                )
        options.append(sorted(values, reverse=True)[: max(30, count * 12)])

    assignments: list[tuple[float, tuple[Any, ...]]] = []
    for combination in product(*options):
        candidate_ids = [item[1] for item in combination]
        shape_ids = [item[2] for item in combination]
        if len(set(candidate_ids)) != count or len(set(shape_ids)) != count:
            continue
        assignments.append((sum(item[0] for item in combination), combination))
    if not assignments:
        raise ValueError("没有满足候选/模板一对一约束的解")
    assignments.sort(key=lambda item: item[0], reverse=True)
    best_total, best = assignments[0]
    second_total = assignments[1][0] if len(assignments) > 1 else best_total - 1

    points = [
        (round(candidates[item[1]][0]), round(candidates[item[1]][1])) for item in best
    ]
    shapes = [(item[2], item[1]) for item in best]
    pair_scores = [float(item[0]) for item in best]
    # 对不同目标数做可比的均分，同时小幅奖励全局解与次优解的间隔。
    quality = best_total / count + min(max(best_total - second_total, 0), 0.2)
    return float(quality), points, shapes, pair_scores


def solve_challenge(
    image_base64: str, thumb_base64: str, log: Any = None
) -> list[tuple[int, int]]:
    if not available():
        raise RuntimeError(
            "click-shape 求解依赖 opencv-python-headless 与官方形状模板，当前环境不完整"
        )
    main = _decode_image(image_base64)
    thumb = _decode_image(thumb_base64)
    if main.shape[0] < 100 or main.shape[1] < 150:
        raise ValueError(f"主图尺寸异常：{main.shape[1]}×{main.shape[0]}")

    detected_count = _detect_target_count(thumb)
    counts = (detected_count,) if detected_count is not None else (2, 3, 4)
    results = []
    errors = []
    for count in counts:
        try:
            results.append((count, *_solve_for_count(main, thumb, count)))
        except Exception as exc:
            errors.append(f"{count}目标: {exc}")
    if not results:
        raise ValueError("；".join(errors))
    count, quality, points, shapes, pair_scores = max(results, key=lambda item: item[1])
    if min(pair_scores) < 0.55 or quality < 0.65:
        raise ValueError(
            f"形状匹配置信度不足（目标数={count}, quality={quality:.3f}, "
            f"pairs={[round(x, 3) for x in pair_scores]}）"
        )
    if log:
        log(
            f"click-shape 识别：目标数={count}，点位={points}，"
            f"模板/候选={shapes}，quality={quality:.3f}"
        )
    return points
