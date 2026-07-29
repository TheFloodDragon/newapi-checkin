#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实样本采集与落盘。

为什么必须要真实样本：本仓库的生成器是我对站点算法的**复刻**，用它自测只能
证明「复刻与复刻一致」，不能证明「复刻与站点一致」。所有对外声明的准确率都
必须用站点真实输出验证，这是本模块存在的唯一理由。

采集策略：在页面里 hook `CanvasRenderingContext2D.fillText` 拿到**绘制时的
真实字符序列**作为标签 —— 比从 DOM 文本列表里猜哪条对应当前 Canvas 可靠得多
（站点一次生成 12 条文本却只渲染 1 个 Canvas 预览）。

导出格式刻意只带「二值掩码 + 标签」：
- 掩码在页面里按与 preprocess.FG_THRESHOLD 完全相同的亮度阈值算出，
  下游全部预处理仍在 Python 侧完成，避免两套实现漂移；
- 300×100 位图打包成 bitset 再 base64，单张约 5KB，可批量导出。

用法（需要在能执行页面 JS 的环境里跑 EXTRACT_JS，再把返回值喂给本模块）：
    python -m captcha_ocr collect --merge payload.json
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .generator import HEIGHT, WIDTH
from .preprocess import FG_THRESHOLD

DATA_DIR = Path(__file__).resolve().parent / "data"
SAMPLES_PATH = DATA_DIR / "real_samples.json"

# 页面内采集脚本。参数：kind（number/alpha/mixed/complex）、length（4-8）、count。
# 返回 [{label, kind, length, mask}]，mask 为 300x100 bitset 的 base64。
#
# 关于阈值：这里的 <= FG_THRESHOLD 必须与 preprocess.FG_THRESHOLD 保持一致，
# 站点字符 RGB ∈ [0x30,0x95]、干扰 ∈ [0x96,0xdb]，两区间不重叠故单阈值可分。
EXTRACT_JS = r"""
(async ({kind, length, count, threshold, width, height}) => {
  const C = CanvasRenderingContext2D.prototype;
  if (!window.__origFillText) window.__origFillText = C.fillText;
  window.__buf = [];
  C.fillText = function (t) {
    window.__buf.push(String(t));
    return window.__origFillText.apply(this, arguments);
  };

  const setVal = (el, v) => {
    const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    d.call(el, String(v));
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
  };

  const radio = [...document.querySelectorAll('input[type=radio]')].find(r => r.value === kind);
  if (radio && !radio.checked) radio.click();
  const range = document.querySelector('input[type=range]');
  if (range) setVal(range, length);
  await new Promise(r => setTimeout(r, 300));

  const cv = document.querySelector('canvas');
  const out = [];
  for (let i = 0; i < count; i++) {
    window.__buf = [];
    cv.dispatchEvent(new MouseEvent('click', {bubbles: true}));
    await new Promise(r => setTimeout(r, 210));
    const label = window.__buf.join('');
    if (label.length !== length) continue;
    const px = cv.getContext('2d').getImageData(0, 0, width, height).data;
    const bytes = new Uint8Array(Math.ceil(width * height / 8));
    for (let p = 0, n = width * height; p < n; p++) {
      const o = p * 4;
      const lum = (px[o] * 299 + px[o + 1] * 587 + px[o + 2] * 114) / 1000;
      if (lum <= threshold) bytes[p >> 3] |= (1 << (p & 7));
    }
    let s = '';
    for (const b of bytes) s += String.fromCharCode(b);
    out.push({label, kind, length, mask: btoa(s)});
  }
  return out;
})
"""


def extract_js_args(kind: str = "mixed", length: int = 6, count: int = 40) -> dict:
    """构造 EXTRACT_JS 的调用参数（阈值与画布尺寸由 Python 侧统一给出）。"""
    return {
        "kind": kind,
        "length": length,
        "count": count,
        "threshold": FG_THRESHOLD,
        "width": WIDTH,
        "height": HEIGHT,
    }


@dataclass(frozen=True)
class RealSample:
    label: str
    kind: str
    length: int
    mask: str  # base64 of bitset

    def to_mask(self) -> np.ndarray:
        """还原为 (HEIGHT, WIDTH) 的 {0,1} 前景掩码。"""
        raw = base64.b64decode(self.mask)
        bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8), bitorder="little")
        return bits[: WIDTH * HEIGHT].reshape(HEIGHT, WIDTH).astype(np.uint8)


def load_samples(path: Path | str = SAMPLES_PATH) -> list[RealSample]:
    path = Path(path)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("samples", payload) if isinstance(payload, dict) else payload
    return [
        RealSample(
            label=str(it["label"]),
            kind=str(it.get("kind", "mixed")),
            length=int(it.get("length", len(it["label"]))),
            mask=str(it["mask"]),
        )
        for it in items
        if it.get("label") and it.get("mask")
    ]


def save_samples(samples: list[RealSample], path: Path | str = SAMPLES_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "note": "randomtool.cn 真实 Canvas 输出的二值掩码；仅用于验证识别准确率",
        "width": WIDTH,
        "height": HEIGHT,
        "threshold": FG_THRESHOLD,
        "samples": [
            {"label": s.label, "kind": s.kind, "length": s.length, "mask": s.mask}
            for s in samples
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def merge(new_items, path: Path | str = SAMPLES_PATH) -> tuple[int, int]:
    """把新采集批次并入样本库，按 (kind,label,mask) 去重。

    返回 (新增数, 总数)。去重带上 mask：同一 label 的两次渲染是不同的图，
    都有价值（旋转角与干扰不同），只有完全相同的位图才算重复。
    """
    existing = load_samples(path)
    seen = {(s.kind, s.label, s.mask) for s in existing}
    added = 0
    for it in new_items:
        s = RealSample(
            label=str(it["label"]),
            kind=str(it.get("kind", "mixed")),
            length=int(it.get("length", len(it["label"]))),
            mask=str(it["mask"]),
        )
        key = (s.kind, s.label, s.mask)
        if key in seen:
            continue
        seen.add(key)
        existing.append(s)
        added += 1
    save_samples(existing, path)
    return added, len(existing)


def stats(path: Path | str = SAMPLES_PATH) -> dict:
    """样本库构成统计（供采集进度与验收报告引用）。"""
    samples = load_samples(path)
    by_kind: dict[str, int] = {}
    by_len: dict[int, int] = {}
    for s in samples:
        by_kind[s.kind] = by_kind.get(s.kind, 0) + 1
        by_len[s.length] = by_len.get(s.length, 0) + 1
    return {
        "images": len(samples),
        "chars": sum(s.length for s in samples),
        "by_kind": dict(sorted(by_kind.items())),
        "by_length": dict(sorted(by_len.items())),
    }


__all__ = [
    "EXTRACT_JS",
    "SAMPLES_PATH",
    "RealSample",
    "extract_js_args",
    "load_samples",
    "merge",
    "save_samples",
    "stats",
]
