#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""弹窗自动关闭守卫：注入 MutationObserver 动态发现并关闭公告弹窗。

公益站登录页常有「公告 / 通知」弹窗遮挡登录按钮，导致自动化点不中入口。
本模块提供两种关闭方式：
1. setup_popup_guard：注入 init script，页面任何时候弹出 modal 都自动关闭
   （MutationObserver 监听 DOM 变化，排除含登录表单的弹窗避免误关）；
2. dismiss_popups：主动触发一次关闭（JS + Playwright 双保险），返回关闭数量。

参考自 millylee/anyrouter-check-in 的 popups.py（Semi UI 弹窗特征）。
"""

from __future__ import annotations

import json

# 核心 JS：发现可见 modal 并点关闭按钮（排除登录表单弹窗）
_DISMISS_CORE_JS = """
const isVisible = (el) => {
  if (!el || !el.isConnected) return false;
  const style = window.getComputedStyle(el);
  if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) return false;
  const rect = el.getBoundingClientRect();
  return rect.width > 0 && rect.height > 0;
};
const modalSelectors = [
  'div[role="dialog"][aria-modal="true"]',
  'div.semi-modal .semi-modal-content[role="dialog"]',
  'div.semi-modal[role="dialog"]',
  'div.semi-modal[aria-modal="true"]',
  '.modal[style*="display: block"]',
  '.ant-modal-wrap',
];
const closeSelectors = [
  'button.semi-modal-close',
  'button[aria-label="close"]',
  'button[aria-label="Close"]',
  '.semi-modal-footer button.semi-button-primary',
  '.semi-modal-footer button:last-child',
  '.ant-modal-close',
  '.modal-header button.close',
];
const loginFieldSelectors = [
  'form.semi-form', '#username', 'input[name="username"]',
  'input[name="email"]', 'input[type="email"]', 'input[type="password"]', '#password',
];
const hasLoginFields = (root) => {
  if (!root) return false;
  for (const sel of loginFieldSelectors) {
    const el = root.querySelector(sel);
    if (el && isVisible(el)) return true;
  }
  return false;
};
const findModals = () => {
  const seen = new Set();
  const modals = [];
  const roots = [document.body, document.documentElement, ...document.querySelectorAll('div.semi-portal')];
  for (const root of roots) {
    if (!root) continue;
    for (const sel of modalSelectors) {
      for (const el of root.querySelectorAll(sel)) {
        if (isVisible(el) && !seen.has(el)) { seen.add(el); modals.push(el); }
      }
    }
  }
  return modals;
};
const findCloseButton = (modal) => {
  for (const sel of closeSelectors) {
    const btn = modal.querySelector(sel);
    if (btn && isVisible(btn)) return btn;
  }
  const confirmText = /^(确认|确定|我知道了?|知道了|同意|接受|继续|OK|Got it|I understand)$/i;
  for (const btn of modal.querySelectorAll('button, [role="button"]')) {
    const text = (btn.innerText || btn.textContent || '').trim();
    if (btn && isVisible(btn) && confirmText.test(text)) return btn;
  }
  return null;
};
const dismissOnce = () => {
  let closed = 0;
  for (const modal of findModals().reverse()) {
    if (hasLoginFields(modal)) continue;  // 不关含登录表单的弹窗
    const btn = findCloseButton(modal);
    if (btn) { btn.click(); closed += 1; }
  }
  return closed;
};
"""

# 主动关闭：循环最多 5 轮直到无可关闭弹窗
_DISMISS_MODALS_JS = f"""() => {{
{_DISMISS_CORE_JS}
  let total = 0;
  for (let i = 0; i < 5; i += 1) {{
    const closed = dismissOnce();
    if (closed === 0) break;
    total += closed;
  }}
  return total;
}}"""

def _popup_guard_js(allowed_origin: str | None = None) -> str:
    """生成 init script；传入 allowed_origin 时仅在该 origin 自动关闭公告。"""
    allowed = json.dumps((allowed_origin or "").rstrip("/"))
    return f"""() => {{
  const allowedOrigin = {allowed};
  if (allowedOrigin && location.origin !== allowedOrigin) return;
  if (window.__popupGuardInstalled) return;
  window.__popupGuardInstalled = true;
{_DISMISS_CORE_JS}
  const dismissLoop = () => {{
    for (let i = 0; i < 3; i += 1) {{ if (dismissOnce() === 0) break; }}
  }};
  let timer = null;
  const schedule = () => {{ clearTimeout(timer); timer = setTimeout(dismissLoop, 300); }};
  const observer = new MutationObserver(schedule);
  const start = () => {{
    if (allowedOrigin && location.origin !== allowedOrigin) return;
    if (!document.documentElement) return;
    observer.observe(document.documentElement, {{
      childList: true, subtree: true, attributes: true,
      attributeFilter: ['class', 'style', 'aria-hidden', 'aria-modal'],
    }});
    schedule();
  }};
  if (document.readyState === 'loading') {{
    document.addEventListener('DOMContentLoaded', start, {{ once: true }});
  }} else {{
    start();
  }}
}}"""


async def setup_popup_guard(page, allowed_origin: str | None = None) -> None:
    """为页面注入弹窗自动关闭脚本（后续弹窗由 MutationObserver 处理）。

    需在导航前调用（add_init_script 在每个新文档加载前执行）。传入
    allowed_origin 时，只在该 origin 自动关闭站点公告，避免误作用到 OAuth provider 页。
    """
    try:
        await page.add_init_script(_popup_guard_js(allowed_origin))
    except Exception:
        pass


async def dismiss_popups(page) -> int:
    """主动触发一次弹窗关闭，返回关闭的弹窗数量。

    供登录前手动调用，确保点击入口前没有遮挡弹窗。
    """
    try:
        result = await page.evaluate(_DISMISS_MODALS_JS)
        return int(result) if result else 0
    except Exception:
        return 0
