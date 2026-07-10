#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 results/checkin_result.json，生成经过脱敏的 Markdown CI 报告。"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import accounts_store
from mask_utils import mask_secrets, sanitize_data

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _cell(value: Any) -> str:
    text = mask_secrets(str(value or ""))
    text = html.escape(text, quote=False)
    return text.replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def build_report(payload: Any) -> str:
    md = f"# 签到报告\n\n**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    safe_payload = sanitize_data(payload)
    rows = safe_payload.get("results", []) if isinstance(safe_payload, dict) else safe_payload
    if not isinstance(rows, list) or not rows:
        return md + "## 错误\n\n签到脚本未生成有效结果。\n"

    rows = [row for row in rows if isinstance(row, dict)]
    ok = sum(1 for row in rows if row.get("ok"))
    fail = len(rows) - ok
    md += f"## 统计\n\n- 成功/已领取: {ok}\n- 失败: {fail}\n- 总计: {len(rows)}\n\n"
    md += "## 详细结果\n\n| 站点 | 状态 | 备注 |\n|------|------|------|\n"
    for row in rows:
        site = _cell(row.get("site", "Unknown"))
        icon = _cell(row.get("icon", ""))
        label = _cell(row.get("label", "Unknown"))
        note = _cell(row.get("note") or row.get("message", ""))
        status = f"{icon} {label}".strip()
        md += f"| {site} | {status} | {note} |\n"
    return md


def main() -> int:
    result_path = Path("results/checkin_result.json")
    if result_path.exists():
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            markdown = build_report(payload)
        except Exception as exc:
            markdown = build_report([]) + f"\n解析签到结果失败：{_cell(exc)}\n"
    else:
        markdown = build_report([]) + "\n未生成签到结果文件。\n"

    report_path = Path("checkin_report.md")
    with accounts_store.file_lock(report_path):
        accounts_store.atomic_write_text(report_path, markdown)
    print("report generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
