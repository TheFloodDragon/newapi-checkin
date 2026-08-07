#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行入口：python -m captcha_ocr <命令>

predict          识别图片
bench            在真实样本上跑准确率与延迟（验收用）
build-templates  重建模板库（改了窗口/角度参数后必须执行）
stats            查看真实样本库构成

base64-build     重建 string_captcha（base64Captcha）模板库
base64-bench     在 tests/data 的真实样本上评测 base64Captcha 识别器
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .generator import CHARSETS


def _cmd_predict(args: argparse.Namespace) -> int:
    from .predictor import Predictor

    predictor = Predictor()
    for path in args.images:
        result = predictor.predict(path, length=args.length, charset=args.charset)
        if args.verbose:
            detail = " ".join(f"{c}({m:.3f}|{s})" for c, m, s
                              in zip(result.text, result.margins, result.seconds))
            print(f"{path}: {result.text}  min_margin={result.min_margin:.3f}  {detail}")
        else:
            print(result.text)
    return 0


def _cmd_bench(args: argparse.Namespace) -> int:
    """真实样本基准。这是唯一有意义的准确率口径 —— 合成集自测只能证明复刻自洽。"""
    import collections

    from . import collect
    from .predictor import Predictor

    samples = collect.load_samples(args.samples) if args.samples else collect.load_samples()
    if not samples:
        print("没有真实样本可评测。先用浏览器执行 collect.EXTRACT_JS 采集并 merge。",
              file=sys.stderr)
        return 1

    predictor = Predictor()
    by = collections.defaultdict(lambda: [0, 0, 0, 0])  # okc, totc, oki, toti
    conf: collections.Counter[str] = collections.Counter()
    worst: list[tuple[float, str, str]] = []

    start = time.perf_counter()
    for s in samples:
        result = predictor.predict_mask(s.to_mask(), s.length, s.kind)
        row = by[s.kind]
        for want, got in zip(s.label, result.text):
            row[1] += 1
            if want == got:
                row[0] += 1
            else:
                conf[f"{want}->{got}"] += 1
        row[3] += 1
        row[2] += result.text == s.label
        worst.append((result.min_margin, s.label, result.text))
    elapsed = time.perf_counter() - start

    print(f"真实样本 {len(samples)} 张 / {sum(s.length for s in samples)} 字符")
    print(f"{'kind':10s}{'char':>18s}{'image':>18s}")
    tc = tt = ti = tn = 0
    for kind in sorted(by):
        okc, totc, oki, toti = by[kind]
        tc += okc
        tt += totc
        ti += oki
        tn += toti
        print(f"{kind:10s}{okc:>7d}/{totc:<5d}{okc/totc:>6.2%}{oki:>7d}/{toti:<5d}{oki/toti:>6.2%}")
    print(f"{'TOTAL':10s}{tc:>7d}/{tt:<5d}{tc/tt:>6.2%}{ti:>7d}/{tn:<5d}{ti/tn:>6.2%}")
    print(f"延迟 {elapsed / len(samples) * 1000:.2f} ms/张")

    if conf:
        print("混淆:", ", ".join(f"{k}x{v}" for k, v in conf.most_common(12)))
    worst.sort()
    print("置信度最低的样本（margin 越小越接近误判边界）:")
    for margin, want, got in worst[:5]:
        flag = "OK " if want == got else "ERR"
        print(f"  {flag} margin={margin:.4f} 真值={want} 预测={got}")
    return 0


def _cmd_build_templates(args: argparse.Namespace) -> int:
    from .matcher import ALL_CHARS, DEFAULT_ANGLES, build_and_save
    from .preprocess import (WIN_BOTTOM, WIN_LEFT, WIN_RIGHT, WIN_TOP,
                             measure_glyph_extents)

    lo_x, hi_x, lo_y, hi_y = measure_glyph_extents(ALL_CHARS, DEFAULT_ANGLES)
    covered = (WIN_LEFT <= lo_x and hi_x <= WIN_RIGHT
               and WIN_TOP <= lo_y and hi_y <= WIN_BOTTOM)
    print(f"字形实测极值 dx [{lo_x}, {hi_x}]  dy [{lo_y}, {hi_y}]")
    print(f"当前取窗范围 dx [{WIN_LEFT}, {WIN_RIGHT}]  dy [{WIN_TOP}, {WIN_BOTTOM}]")
    if not covered:
        # 窗口裁掉字形会静默掉准确率（实测从 100% 掉到 70%），必须拦住
        print("窗口未覆盖字形极值，请先调整 preprocess.WIN_* 后重试", file=sys.stderr)
        return 1
    print("窗口覆盖充分")

    path = build_and_save(args.output) if args.output else build_and_save()
    size = Path(path).stat().st_size
    print(f"模板库已写入 {path}（{size / 1e6:.2f} MB，{len(ALL_CHARS)} 字符 × "
          f"{len(DEFAULT_ANGLES)} 角度）")
    return 0


def _cmd_base64_build(args: argparse.Namespace) -> int:
    """重建 base64Captcha 系模板库。

    字体不随仓库分发（第三方字体），需先下载到 captcha_ocr/fonts/。
    """
    from . import base64_captcha as bc

    try:
        path = bc.build_templates(
            font_dir=args.fonts or bc.FONT_DIR,
            charset=args.charset,
            path=args.output or bc.TEMPLATES_PATH,
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    table = bc.Templates.load(path)
    print(f"模板库已写入 {path}（{Path(path).stat().st_size / 1024:.0f} KB，"
          f"{len(table)} 项 = {len(bc.CHARSETS[args.charset])} 字符 × "
          f"{len(bc.FONT_NAMES)} 字体 × {len(bc.font_sizes(table.height))} 字号）")
    return 0


def _cmd_base64_bench(args: argparse.Namespace) -> int:
    """真实样本基准。标签里的 `?` 表示人工无法确认，该位与该图都跳过。"""
    import base64
    import io
    import json

    import numpy as np

    from . import base64_captcha as bc

    path = args.samples or Path(__file__).resolve().parents[1] / "tests" / "data" / "base64_captcha_samples.json"
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"读取样本失败：{exc}", file=sys.stderr)
        return 1
    try:
        from PIL import Image
    except ImportError:
        print("解码 PNG 需要 pillow：uv sync --extra dev", file=sys.stderr)
        return 1

    ok_char = tot_char = ok_img = tot_img = exact_ok = exact_tot = 0
    start = time.perf_counter()
    for sample in payload["samples"]:
        label = str(sample["label"])
        with Image.open(io.BytesIO(base64.b64decode(sample["png_base64"]))) as image:
            array = np.asarray(image.convert("RGB"))
        result = bc.solve_array(array, length=int(payload.get("char_count") or bc.DEFAULT_LENGTH))
        text = result.text.ljust(len(label))
        for want, got in zip(label, text):
            if want == "?":
                continue
            tot_char += 1
            ok_char += int(want == got)
        if "?" in label:
            continue
        tot_img += 1
        correct = text == label
        ok_img += int(correct)
        if result.exact:
            exact_tot += 1
            exact_ok += int(correct)
        if args.verbose:
            flag = "OK " if correct else "ERR"
            marks = " ".join(f"{c or '?'}:{s:.2f}/{m:+.2f}" for c, s, m in result.detail)
            print(f"{flag} 真值={label} 预测={result.text} exact={int(result.exact)} {marks}")
    elapsed = time.perf_counter() - start

    print(f"单字符 {ok_char}/{tot_char} = {ok_char / max(1, tot_char):.2%}")
    print(f"整图   {ok_img}/{tot_img} = {ok_img / max(1, tot_img):.2%}")
    if exact_tot:
        print(f"exact 通过率 {exact_tot}/{tot_img} = {exact_tot / tot_img:.2%}；"
              f"通过者正确率 {exact_ok}/{exact_tot} = {exact_ok / exact_tot:.2%}")
    print(f"延迟 {elapsed / len(payload['samples']) * 1000:.0f} ms/张")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    from . import collect

    info = collect.stats(args.samples) if args.samples else collect.stats()
    print(f"真实样本 {info['images']} 张 / {info['chars']} 字符")
    print("按字符集:", info["by_kind"] or "（空）")
    print("按长度:  ", info["by_length"] or "（空）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="captcha_ocr", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_predict = sub.add_parser("predict", help="识别图片")
    p_predict.add_argument("images", nargs="+", help="图片路径")
    p_predict.add_argument("-n", "--length", type=int, default=6, help="字符数（默认 6）")
    p_predict.add_argument("-c", "--charset", choices=sorted(CHARSETS),
                           help="限定字符集，可显著降低混淆")
    p_predict.add_argument("-v", "--verbose", action="store_true", help="输出置信度与次优候选")
    p_predict.set_defaults(func=_cmd_predict)

    p_bench = sub.add_parser("bench", help="真实样本准确率与延迟基准")
    p_bench.add_argument("--samples", type=Path, help="样本 JSON 路径（默认包内 data/）")
    p_bench.set_defaults(func=_cmd_bench)

    p_build = sub.add_parser("build-templates", help="重建模板库")
    p_build.add_argument("-o", "--output", type=Path, help="输出 .npz 路径")
    p_build.set_defaults(func=_cmd_build_templates)

    p_stats = sub.add_parser("stats", help="真实样本库构成")
    p_stats.add_argument("--samples", type=Path, help="样本 JSON 路径")
    p_stats.set_defaults(func=_cmd_stats)

    p_b64_build = sub.add_parser("base64-build", help="重建 base64Captcha 系模板库")
    p_b64_build.add_argument("--fonts", type=Path, help="base64Captcha TTF 目录（默认 captcha_ocr/fonts）")
    p_b64_build.add_argument("-c", "--charset", default="digits",
                             choices=("digits", "letters", "mixed"),
                             help="站点 GraphicCaptchaCharset 档位（默认 digits）")
    p_b64_build.add_argument("-o", "--output", type=Path, help="输出 .npz 路径")
    p_b64_build.set_defaults(func=_cmd_base64_build)

    p_b64_bench = sub.add_parser("base64-bench", help="base64Captcha 识别器准确率基准")
    p_b64_bench.add_argument("--samples", type=Path, help="样本 JSON 路径（默认 tests/data）")
    p_b64_bench.add_argument("-v", "--verbose", action="store_true", help="逐张输出得分")
    p_b64_bench.set_defaults(func=_cmd_base64_bench)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
