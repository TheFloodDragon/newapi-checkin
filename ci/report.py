#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读取 results/checkin_result.json，生成 Markdown 报告（CI 用）。

输出到 checkin_report.md（相对当前工作目录）。
解析 run__all_checkin.py 写出的结构：{"results": [...], "success_count": ..., ...}
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

md = f"# 签到报告\n\n**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
result_path = Path("results/checkin_result.json")

if result_path.exists():
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        rows = payload.get("results", []) if isinstance(payload, dict) else payload
        if rows:
            ok = sum(1 for x in rows if x.get("ok"))
            fail = len(rows) - ok
            md += f"## 统计\n\n- ✅ 成功/已领取: {ok}\n"
            if fail > 0:
                md += f"- ❌ 失败: {fail}\n"
            md += f"- 📝 总计: {len(rows)}\n\n"
            md += "## 详细结果\n\n| 站点 | 状态 | 备注 |\n|------|------|------|\n"
            for x in rows:
                site = x.get("site", "Unknown")
                icon = x.get("icon", "·")
                label = x.get("label", "Unknown")
                note = (x.get("note") or x.get("message", "")).replace("|", "\\|").replace("\n", " ")
                md += f"| {site} | {icon} {label} | {note} |\n"
        else:
            md += "## 错误\n\n签到脚本执行失败，未生成有效结果。\n"
    except Exception as exc:
        md += f"## 错误\n\n解析签到结果失败：{exc}\n"
else:
    md += "## 警告\n\n未生成签到结果文件。\n"

Path("checkin_report.md").write_text(md, encoding="utf-8")
print("report generated")
