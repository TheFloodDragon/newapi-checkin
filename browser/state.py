#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""browser_oauth 登录态的编码 / 解码（跨平台 storage_state，用于 GitHub Secret）。

【为什么是 storage_state 而不是打包 profile 文件】
Chromium 的 Cookies 是用平台绑定的密钥加密的：Windows 上主密钥经 DPAPI
（CryptProtectData，per-user + per-machine）保护。若把整个 profile 二进制
打包搬到 Linux CI，那边的 Chromium 解不开 DPAPI 密钥 → 所有 cookie 失效 →
OAuth 必然失败。

Playwright 的 storage_state() 会在【捕获机器上】把 cookie 解密成明文 JSON
（cookies + localStorage origins），明文跨用户 / 跨机器 / 跨系统通用。
因此本模块只存 storage_state：json → gzip → base64，纯标准库、无加密、无口令。
登录态含明文第三方 cookie，与 ACCOUNTS.json 中其它凭据同级，靠 .gitignore /
GitHub Secret 加密存储保护。

数据格式（解码后）：Playwright storage_state dict，形如
    {"cookies": [...], "origins": [{"origin": ..., "localStorage": [...]}]}
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

GITHUB_SECRET_LIMIT = 64 * 1024  # GitHub 单个 Secret 上限约 64KB


class BrowserStateError(Exception):
    """browser_state 编码/解码相关错误（供 provider 捕获）。"""


def encode_state(storage_state: dict[str, Any]) -> str:
    """把 Playwright storage_state dict 编码为可粘贴的 base64(gzip(json)) 文本。"""
    raw = json.dumps(storage_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    packed = gzip.compress(raw, compresslevel=9)
    return base64.b64encode(packed).decode("ascii")


def decode_state(text: str) -> dict[str, Any]:
    """把 base64(gzip(json)) 文本解码回 storage_state dict。失败抛 BrowserStateError。

    对旧版（tar.xz 打包 profile）格式给出明确升级提示。
    """
    text = (text or "").strip()
    if not text:
        raise BrowserStateError("登录态文本为空")
    # 剔除粘贴时可能混入的空白/换行；非 ASCII 说明数据已损坏
    text = "".join(text.split())
    try:
        ascii_bytes = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise BrowserStateError(
            "登录态文本含非 ASCII 字符，数据已损坏，请用「浏览器登录捕获」重新生成。"
        ) from exc
    try:
        packed = base64.b64decode(ascii_bytes)
    except Exception as exc:
        raise BrowserStateError(f"base64 解码失败：{exc}") from exc

    # 旧格式探测：tar.xz 魔数 0xFD '7zXZ'
    if packed[:6] == b"\xfd7zXZ\x00":
        raise BrowserStateError(
            "检测到旧版（profile 打包）登录态格式，已不再支持（无法跨平台）。"
            "请用「浏览器登录捕获」重新生成 storage_state 登录态。"
        )

    try:
        raw = gzip.decompress(packed)
    except Exception as exc:
        raise BrowserStateError(f"gzip 解压失败（数据损坏或格式过旧）：{exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BrowserStateError(f"JSON 解析失败：{exc}") from exc
    if not isinstance(data, dict) or "cookies" not in data:
        raise BrowserStateError("登录态内容不是有效的 storage_state（缺少 cookies）。")
    return data


def state_summary(storage_state: dict[str, Any]) -> str:
    """生成登录态摘要（cookie 数 / 域名 / localStorage 条目），便于诊断。"""
    cookies = storage_state.get("cookies") or []
    origins = storage_state.get("origins") or []
    domains = sorted({str(c.get("domain", "")).lstrip(".") for c in cookies if c.get("domain")})
    ls_count = sum(len(o.get("localStorage") or []) for o in origins)
    return f"cookies={len(cookies)} 域名={','.join(domains) or '无'} localStorage条目={ls_count}"


# ── CLI（调试用：从本地 profile 导出 / 查看 storage_state）─────────────────────
def cmd_inspect(args) -> int:
    """读取一段 base64 登录态文本并打印摘要（校验格式是否有效）。"""
    if args.in_file:
        text = Path(args.in_file).read_text(encoding="ascii").strip()
    else:
        text = sys.stdin.read().strip()
    try:
        state = decode_state(text)
    except BrowserStateError as exc:
        print(f"无效：{exc}", file=sys.stderr)
        return 2
    print(state_summary(state))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="browser_oauth 登录态工具（storage_state；捕获请用 GUI 或 browser/poc_oauth.py）"
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    pi = sub.add_parser("inspect", help="校验并打印一段 base64 登录态摘要")
    pi.add_argument("--in", dest="in_file", default="", help="从文件读取（默认读 stdin）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode == "inspect":
        return cmd_inspect(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
