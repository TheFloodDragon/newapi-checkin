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
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

GITHUB_SECRET_LIMIT = 64 * 1024  # GitHub 单个 Secret 上限约 64KB

# 压缩前（JSON 原始字节）上限：4 MB，足以容纳大量 cookie/localStorage
_MAX_RAW_BYTES = 4 * 1024 * 1024
# base64 后必须能放进 GitHub Secret；压缩包上限按 4:3 膨胀反推。
_MAX_ENCODED_BYTES = GITHUB_SECRET_LIMIT
_MAX_PACKED_BYTES = (GITHUB_SECRET_LIMIT // 4) * 3

# encode_state 只生成标准 base64；解码时拒绝 URL-safe/混合字母表。
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+=*$")


class BrowserStateError(Exception):
    """browser_state 编码/解码相关错误（供 provider 捕获）。"""


async def restore_storage_state(
    context: Any,
    storage_state: dict[str, Any] | None,
) -> None:
    """把 cookies/localStorage 恢复到浏览器上下文，并严格隔离不同 origin。"""
    data = storage_state or {}
    cookies = data.get("cookies") or []
    if cookies:
        await context.add_cookies(cookies)

    origin_map: dict[str, dict[str, str]] = {}
    for origin_data in data.get("origins", []) or []:
        if not isinstance(origin_data, dict):
            continue
        origin = str(origin_data.get("origin") or "").strip()
        if not origin:
            continue
        pairs = origin_map.setdefault(origin, {})
        for item in origin_data.get("localStorage", []) or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            value = item.get("value")
            pairs[name] = "" if value is None else str(value)

    origin_map = {origin: pairs for origin, pairs in origin_map.items() if pairs}
    if not origin_map:
        return

    init_js = """
    (() => {
      const states = %s;
      const pairs = states[location.origin] || {};
      for (const [key, value] of Object.entries(pairs)) {
        try { localStorage.setItem(key, value); } catch (_) {}
      }
    })();
    """ % json.dumps(origin_map, ensure_ascii=False, separators=(",", ":"))
    await context.add_init_script(init_js)


def _validate_storage_state(data: Any) -> None:
    """校验 storage_state 顶层结构及 cookies/origins/localStorage 基本类型。

    只做类型/存在性检查，不校验具体字段语义，以保持对不同版本 Playwright 的兼容。
    抛 BrowserStateError 说明不合规原因。
    """
    if not isinstance(data, dict):
        raise BrowserStateError("storage_state 必须是 JSON 对象（dict）。")
    if "cookies" not in data:
        raise BrowserStateError("storage_state 缺少必要字段 'cookies'。")

    cookies = data["cookies"]
    if not isinstance(cookies, list):
        raise BrowserStateError("storage_state.cookies 必须是数组。")
    for i, c in enumerate(cookies):
        if not isinstance(c, dict):
            raise BrowserStateError(f"storage_state.cookies[{i}] 必须是对象。")
        for required in ("name", "value", "domain", "path"):
            if required not in c:
                raise BrowserStateError(
                    f"storage_state.cookies[{i}] 缺少必要字段 '{required}'。"
                )
        if any(not isinstance(c.get(key), str) for key in ("name", "value", "domain", "path")):
            raise BrowserStateError(
                f"storage_state.cookies[{i}] 的 name/value/domain/path 必须是字符串。"
            )
        for bool_key in ("httpOnly", "secure"):
            if bool_key in c and not isinstance(c[bool_key], bool):
                raise BrowserStateError(f"storage_state.cookies[{i}].{bool_key} 必须是布尔值。")
        if "expires" in c and (isinstance(c["expires"], bool) or not isinstance(c["expires"], (int, float))):
            raise BrowserStateError(f"storage_state.cookies[{i}].expires 必须是数字。")

    origins = data.get("origins")
    if origins is None:
        return  # origins 是可选的
    if not isinstance(origins, list):
        raise BrowserStateError("storage_state.origins 必须是数组。")
    for i, origin_entry in enumerate(origins):
        if not isinstance(origin_entry, dict):
            raise BrowserStateError(f"storage_state.origins[{i}] 必须是对象。")
        if not isinstance(origin_entry.get("origin"), str):
            raise BrowserStateError(f"storage_state.origins[{i}].origin 必须是字符串。")
        ls = origin_entry.get("localStorage")
        if ls is None:
            continue
        if not isinstance(ls, list):
            raise BrowserStateError(
                f"storage_state.origins[{i}].localStorage 必须是数组。"
            )
        for j, item in enumerate(ls):
            if not isinstance(item, dict):
                raise BrowserStateError(
                    f"storage_state.origins[{i}].localStorage[{j}] 必须是对象。"
                )
            if not isinstance(item.get("name"), str) or not isinstance(
                item.get("value"), str
            ):
                raise BrowserStateError(
                    f"storage_state.origins[{i}].localStorage[{j}] "
                    "的 name/value 必须是字符串。"
                )


def encode_state(storage_state: dict[str, Any]) -> str:
    """把 Playwright storage_state dict 编码为可粘贴的 base64(gzip(json)) 文本。

    编码前校验输入结构，并检查编码结果不超过 GitHub Secret 上限。
    """
    _validate_storage_state(storage_state)
    raw = json.dumps(storage_state, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_RAW_BYTES:
        raise BrowserStateError(
            f"storage_state 序列化后过大（{len(raw):,} 字节 > 上限 {_MAX_RAW_BYTES:,}），"
            "请清理不必要的 cookie/localStorage 后重新捕获。"
        )
    packed = gzip.compress(raw, compresslevel=9)
    if len(packed) > _MAX_PACKED_BYTES:
        raise BrowserStateError(
            f"压缩后仍过大（{len(packed):,} 字节 > 上限 {_MAX_PACKED_BYTES:,}），"
            "超出 GitHub Secret 存储限制，请减少登录站点数量或清理登录态后重试。"
        )
    encoded = base64.b64encode(packed).decode("ascii")
    if len(encoded.encode("ascii")) > _MAX_ENCODED_BYTES:
        raise BrowserStateError(
            f"base64 登录态过大（{len(encoded):,} 字节 > 上限 {_MAX_ENCODED_BYTES:,}），"
            "无法放入 GitHub Secret。"
        )
    return encoded


def decode_state(text: str) -> dict[str, Any]:
    """把 base64(gzip(json)) 文本解码回 storage_state dict。失败抛 BrowserStateError。

    对旧版（tar.xz 打包 profile）格式给出明确升级提示。
    严格校验：base64 合法性、压缩包大小、JSON schema。
    """
    text = (text or "").strip()
    if not text:
        raise BrowserStateError("登录态文本为空")
    # 剔除粘贴时可能混入的空白/换行
    text = "".join(text.split())

    # 非 ASCII 说明数据已损坏
    try:
        ascii_bytes = text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise BrowserStateError(
            "登录态文本含非 ASCII 字符，数据已损坏，请用「浏览器登录捕获」重新生成。"
        ) from exc

    if len(ascii_bytes) > _MAX_ENCODED_BYTES:
        raise BrowserStateError(
            f"base64 登录态过大（{len(ascii_bytes):,} 字节 > 上限 {_MAX_ENCODED_BYTES:,}），拒绝处理。"
        )

    # 严格校验标准 base64 字符集和 padding
    if not _BASE64_RE.match(text):
        raise BrowserStateError(
            "登录态文本含非法 base64 字符，数据已损坏，请用「浏览器登录捕获」重新生成。"
        )
    # 标准 base64 长度必须是 4 的倍数（已用 '=' 填充）
    if len(text) % 4 != 0:
        raise BrowserStateError(
            "base64 文本长度不合规（非 4 的倍数），数据可能被截断，"
            "请用「浏览器登录捕获」重新生成。"
        )

    try:
        packed = base64.b64decode(ascii_bytes, validate=True)
    except Exception as exc:
        raise BrowserStateError(f"base64 解码失败：{exc}") from exc

    # 旧格式探测：tar.xz 魔数 0xFD '7zXZ'
    if packed[:6] == b"\xfd7zXZ\x00":
        raise BrowserStateError(
            "检测到旧版（profile 打包）登录态格式，已不再支持（无法跨平台）。"
            "请用「浏览器登录捕获」重新生成 storage_state 登录态。"
        )

    # 压缩包大小限制（防止 gzip bomb）
    if len(packed) > _MAX_PACKED_BYTES:
        raise BrowserStateError(
            f"压缩数据过大（{len(packed):,} 字节 > 上限 {_MAX_PACKED_BYTES:,}），"
            "拒绝处理，数据可能已损坏。"
        )

    try:
        with gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb") as gz:
            raw = gz.read(_MAX_RAW_BYTES + 1)
    except Exception as exc:
        raise BrowserStateError(f"gzip 解压失败（数据损坏或格式过旧）：{exc}") from exc

    # 流式读取只允许上限 + 1 字节，避免 gzip bomb 在校验前占满内存。
    if len(raw) > _MAX_RAW_BYTES:
        raise BrowserStateError(
            f"解压后数据过大（{len(raw):,} 字节 > 上限 {_MAX_RAW_BYTES:,}），"
            "拒绝处理，数据可能已损坏。"
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BrowserStateError(f"JSON 解析失败：{exc}") from exc

    # 校验 storage_state schema（类型 + 结构）
    _validate_storage_state(data)
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
