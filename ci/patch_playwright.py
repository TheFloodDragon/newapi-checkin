#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Patch Playwright's bundled Firefox driver to survive pageError with no location.

Root cause
----------
Playwright's driver (coreBundle.js) builds a pageError dispatch event with:

    url: pageError.location.url,
    line: pageError.location.lineNumber,
    column: pageError.location.columnNumber

Firefox (Camoufox) sometimes emits an uncaught page error whose ``location`` is
undefined (e.g. an unhandled site error like "get announcements failed"). Two
things then go wrong inside the Node driver, either of which crashes the whole
driver process:

  1. Reading ``.url`` on undefined throws a TypeError.
  2. Even if you make step 1 null-safe (optional chaining), a downstream
     validator (``tString``) rejects ``url: undefined`` with
     "ValidationError: location.url: expected string, got undefined" and
     crashes anyway.

Once the driver process dies, every later Playwright call fails with
"Connection closed while reading from the driver", so a single misbehaving site
script kills the entire check-in run.

The fix must therefore supply *valid* values, not just avoid the throw: when
``location`` is missing, url becomes "" and line/column become 0. We rewrite the
three field expressions to:

    url: (pageError.location && pageError.location.url) || "",
    line: (pageError.location && pageError.location.lineNumber) || 0,
    column: (pageError.location && pageError.location.columnNumber) || 0

This satisfies the string/number validators regardless of whether ``location``
is present.

This is a Playwright bug (present in 1.60.0). The patch is idempotent (skips if
already applied) and best-effort: if the file or the expected snippet is not
found, it prints a note and exits 0 so it never blocks the pipeline. Run it in
CI after ``uv sync`` / browser install and before the check-in step, because
reinstalling Playwright restores the original bundle.

Usage:
    python ci/patch_playwright.py
"""

from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg: str) -> None:
    print(f"[patch_playwright] {msg}")


# 每个 marker 对应一个可独立修复的 Playwright Firefox 驱动缺陷。
PAGE_ERROR_UNSAFE_MARKER = "pageError.location.url"
NETWORK_RESPONSE_UNSAFE_MARKER = """const response2 = request2.request._existingResponse();
        response2.setTransferSize(event.transferSize);"""

# Each entry maps the original unsafe snippet to a safe form.
#
# CRITICAL: pageError replacement text must NOT contain PAGE_ERROR_UNSAFE_MARKER，
# 否则幂等检查会一直认为它尚未修复。
# Using the "(pageError.location||{})" prefix breaks the substring: the text
# becomes "(pageError.location||{}).url" -- no ".location.url" run anywhere.
# When location is undefined this yields ({}).url === undefined, so we still need
# the "|| default" tail to satisfy the string/number validators downstream.
REPLACEMENTS = (
    (
        "pageError.location.url",
        '((pageError.location||{}).url||"")',
    ),
    (
        "pageError.location.lineNumber",
        "((pageError.location||{}).lineNumber||0)",
    ),
    (
        "pageError.location.columnNumber",
        "((pageError.location||{}).columnNumber||0)",
    ),
    (
        NETWORK_RESPONSE_UNSAFE_MARKER,
        """const response2 = request2.request._existingResponse();
        if (!response2) {
          this._requests.delete(request2._id);
          return;
        }
        response2.setTransferSize(event.transferSize);""",
    ),
)

UNSAFE_MARKERS = (PAGE_ERROR_UNSAFE_MARKER, NETWORK_RESPONSE_UNSAFE_MARKER)


def find_core_bundle() -> Path | None:
    """Locate coreBundle.js inside the installed playwright package."""
    try:
        import playwright  # noqa: PLC0415 - optional dependency, resolved at runtime
    except Exception as exc:  # noqa: BLE001 - any import failure means nothing to patch
        log(f"playwright not importable, nothing to patch: {exc}")
        return None

    pkg_dir = Path(playwright.__file__).resolve().parent
    candidate = pkg_dir / "driver" / "package" / "lib" / "coreBundle.js"
    if candidate.is_file():
        return candidate

    # Fall back to a search in case the layout changes across versions.
    for found in pkg_dir.rglob("coreBundle.js"):
        return found
    return None


def apply_replacements(text: str) -> tuple[str, int]:
    """Apply the null-safe rewrites.

    Order matters: the longer keys (lineNumber / columnNumber) must be replaced
    before the shorter ``pageError.location.url`` prefix would otherwise match
    inside them. In practice the three keys are distinct suffixes so there is no
    overlap, but we still replace the longest-specific ones first to be safe.
    """
    patched = text
    total = 0
    for unsafe, safe in sorted(REPLACEMENTS, key=lambda kv: len(kv[0]), reverse=True):
        count = patched.count(unsafe)
        if count:
            patched = patched.replace(unsafe, safe)
            total += count
    return patched, total


def main() -> int:
    target = find_core_bundle()
    if target is None:
        log("coreBundle.js not found; skipping (exit 0).")
        return 0

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - unreadable file should not break CI
        log(f"cannot read {target}: {exc}; skipping (exit 0).")
        return 0

    # 两类缺陷独立判断；即使旧 pageError 补丁已应用，也不能跳过新的网络补丁。
    present_markers = [marker for marker in UNSAFE_MARKERS if marker in text]
    if not present_markers:
        log("already patched (no known unsafe markers found); skipping.")
        return 0

    patched, total = apply_replacements(text)
    if total == 0:
        log("no expected snippets matched; skipping (exit 0).")
        return 0

    # 所有本次检测到的 marker 都必须消失，否则不写文件，避免半修复。
    remaining = [marker for marker in present_markers if marker in patched]
    if remaining:
        log(f"unsafe marker(s) still present after patch: {len(remaining)}; aborting write (exit 0).")
        return 0

    try:
        # Write to a temp file then replace, so a crash mid-write cannot leave a
        # truncated (and thus broken) driver bundle behind.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(patched, encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:  # noqa: BLE001 - write failure should not break CI
        log(f"cannot write patched file: {exc}; skipping (exit 0).")
        return 0

    log(f"patched {total} occurrence(s) in {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
