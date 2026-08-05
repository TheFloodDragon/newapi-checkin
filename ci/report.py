#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取签到结果 JSON（accounts_store.RESULTS_DIR），生成经过脱敏的 Markdown CI 报告。"""

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


def _row_status(row: dict[str, Any]) -> str:
    parts: list[str] = []
    if row.get("retry_succeeded") is True:
        parts.append("🔁 重试成功")
    elif row.get("retried") is True:
        parts.append("本轮重试")
    if row.get("carried_forward") is True:
        parts.append("本轮跳过")
    base_status = f"{row.get('icon', '')} {row.get('label', 'Unknown')}".strip()
    parts.append(base_status)
    return _cell(" · ".join(part for part in parts if part))


def build_report(payload: Any) -> str:
    md = f"# 签到报告\n\n**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    safe_payload = sanitize_data(payload)
    rows = safe_payload.get("results", []) if isinstance(safe_payload, dict) else safe_payload
    if not isinstance(rows, list) or not rows:
        return md + "## 错误\n\n签到脚本未生成有效结果。\n"

    rows = [row for row in rows if isinstance(row, dict)]
    ok = sum(1 for row in rows if row.get("ok") is True)
    fail = len(rows) - ok
    has_run_metadata = any(
        "executed_this_run" in row or "carried_forward" in row
        for row in rows
    )
    if has_run_metadata:
        executed = sum(1 for row in rows if row.get("executed_this_run") is True)
        carried = sum(1 for row in rows if row.get("carried_forward") is True)
        retry_succeeded = sum(
            1
            for row in rows
            if row.get("executed_this_run") is True and row.get("retry_succeeded") is True
        )
    else:
        # 兼容升级前的结果文件：当时所有行都来自本轮执行。
        executed = len(rows)
        carried = 0
        retry_succeeded = 0
    md += (
        f"## 统计\n\n"
        f"- 成功/已领取: {ok}\n"
        f"- 失败: {fail}\n"
        f"- 总计: {len(rows)}\n"
        f"- 本轮实际执行: {executed}\n"
        f"- 沿用上次完成: {carried}\n"
        f"- 本轮重试成功: {retry_succeeded}\n\n"
    )
    md += "## 详细结果\n\n| 站点 | 状态 | 备注 |\n|------|------|------|\n"
    for row in rows:
        site = _cell(row.get("site", "Unknown"))
        status = _row_status(row)
        note = _cell(row.get("note") or row.get("message", ""))
        md += f"| {site} | {status} | {note} |\n"
    return md


def main() -> int:
    result_path = accounts_store.RESULTS_DIR / "checkin_result.json"
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
