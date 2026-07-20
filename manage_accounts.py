#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公益站 & 账号管理 GUI（PySide6 现代桌面版）。

功能与旧版一致，界面迁移到 PySide6：
- 左侧站点列表：状态点、名称、地址、类型徽标；
- 右侧编辑区：站点信息、认证凭据；
- 支持新增、删除、复制、排序、保存、导出 Secret、剪贴板导入；
- 数据由 accounts_store 写回 ACCOUNTS.json（统一保存站点配置、状态与凭据）。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import traceback
from datetime import datetime
from typing import Any

try:
    from PySide6.QtCore import Qt, QTimer, QThread, QThreadPool, QRunnable, QObject, Signal
    from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QButtonGroup,
        QCheckBox,
        QComboBox,
        QDialog,
        QFrame,
        QGraphicsDropShadowEffect,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSpacerItem,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as exc:  # pragma: no cover - 运行期依赖提示
    print("PySide6 is not installed. Install it with: pip install PySide6", file=sys.stderr)
    raise SystemExit(1) from exc

import accounts_store
from mask_utils import mask_secrets

from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent  # checkin/，供 BrowserWorker 加入 sys.path 后 import browser 包

# ── 常量 ──────────────────────────────────────────────────────────────────────
# 站点适配器（site_profile）：接口长什么样。
TYPES = ("newapi", "sub2api")
CRED = ("user_id", "access_token", "cookie")

# 登录方式（auth_method）：如何获得已认证会话。
AUTH_METHODS = ("access_token", "cookie", "browser", "oauth")
AUTH_METHOD_LABELS = {
    "access_token": "Access Token (Bearer)",
    "cookie": "Cookie",
    "browser": "站点浏览器登录态",
    "oauth": "OAuth 登录态（共享账号）",
}

# 签到方式（checkin_action）：如何触发发额度。
CHECKIN_ACTIONS = ("api", "visit", "relogin", "browser_script")
ACTION_LABELS = {
    "api": "接口签到 (调签到接口)",
    "visit": "访问保活 (只读监控额度)",
    "relogin": "浏览器重登 (自动 OAuth 发额度)",
    "browser_script": "自定义浏览器脚本",
}

# relogin 共享 OAuth provider（登录态存 ACCOUNTS.json 顶层 oauth_states）。
OAUTH_PROVIDERS = accounts_store.KNOWN_OAUTH_PROVIDERS
OAUTH_PROVIDER_LABELS = {
    "linuxdo": "Linux.do",
    "github": "GitHub",
}

# newapi + api 的接口变体偏好（api_variant）。
API_VARIANTS = ("auto", "legacy")
API_VARIANT_LABELS = {
    "auto": "自动 (challenge 优先)",
    "legacy": "旧版接口 (legacy)",
}

TYPE_INFO = {
    "newapi": {"label": "New API", "fg": "#3730a3", "bg": "#e0e7ff"},
    "sub2api": {"label": "Sub2API", "fg": "#065f46", "bg": "#d1fae5"},
}

C = {
    "bg": "#eef2f7",
    "surface": "#ffffff",
    "surface_alt": "#f6f8fb",
    "border": "#e2e8f0",
    "border_mid": "#cbd5e1",
    "accent": "#5b54e6",
    "accent_dk": "#4840d4",
    "accent_soft": "#eef0ff",
    "text": "#0f172a",
    "soft": "#475569",
    "mute": "#94a3b8",
    "ok": "#16a34a",
    "danger": "#e11d48",
    "danger_soft": "#fff1f2",
    "warn": "#d97706",
}

F = "Segoe UI"
MONO = "Consolas"


_SENSITIVE_LOG_KEYS = {
    "access_token",
    "authorization",
    "auth_token",
    "browser_state",
    "browser_state_text",
    "cookie",
    "cookies",
    "state",
    "storage_state",
    "token",
}


def _is_sensitive_log_key(key: Any) -> bool:
    text = str(key or "").strip().lower()
    return text in _SENSITIVE_LOG_KEYS or text.endswith("_token") or text.endswith("_state") or "cookie" in text


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): (f"<redacted:{len(str(v or ''))} chars>" if _is_sensitive_log_key(k) else _redact_log_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(v) for v in value)
    return value


def _safe_log_value(value: Any, key: str = "") -> str:
    if value is None:
        return ""
    if _is_sensitive_log_key(key):
        text = f"<redacted:{len(str(value or ''))} chars>"
    elif isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(_redact_log_value(value), ensure_ascii=False, default=str, separators=(",", ":"))
        except Exception:
            text = str(_redact_log_value(value))
    else:
        text = str(value)
    return mask_secrets(text.replace("\r", " ").replace("\n", " ").strip())


def _bg_log(level: str, message: str, **fields: Any) -> None:
    """输出后台任务日志到控制台（stderr），供终端/调试器查看。"""
    try:
        extra = " ".join(
            f"{key}={_safe_log_value(value, key)}"
            for key, value in fields.items()
            if value not in (None, "")
        )
        line = f"[{level.upper()}] {mask_secrets(str(message))}"
        if extra:
            line += f" | {extra}"
        print(line, file=sys.stderr, flush=True)
    except Exception:
        # 日志输出失败不能影响 GUI 主流程。
        pass


def _error_result(message: str, exc: BaseException | None = None, **fields: Any) -> dict[str, Any]:
    tb = traceback.format_exc() if exc is not None else ""
    error_text = f"{message}：{exc}" if exc is not None else message
    _bg_log("ERROR", error_text, traceback=tb, **fields)
    return {
        "ok": False,
        "status": "error",
        "message": mask_secrets(error_text),
        "error": mask_secrets(str(exc)) if exc is not None else mask_secrets(message),
        "traceback": tb,
    }


# ── 数据装载 ──────────────────────────────────────────────────────────────────
def _rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in accounts_store.load_unified_accounts():
        url = accounts_store.normalize_base_url(str(row.get("base_url", "") or row.get("url", "")))
        nm = str(row.get("name") or url)
        t = str(row.get("site_profile") or row.get("type") or row.get("provider") or "newapi").strip().lower()
        if t not in TYPES:
            t = "newapi"
        auth_method = str(row.get("auth_method") or "").strip().lower()
        if auth_method not in AUTH_METHODS:
            auth_method = "access_token" if row.get("access_token") else "cookie"
        checkin_action = str(row.get("checkin_action") or "").strip().lower()
        if checkin_action not in CHECKIN_ACTIONS:
            checkin_action = "api"
        if checkin_action == "relogin":
            auth_method = "oauth"
        if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
            auth_method = "oauth"
        api_variant = str(row.get("api_variant") or "auto").strip().lower()
        if api_variant not in API_VARIANTS:
            api_variant = "auto"
        oauth_provider = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
        out.append(
            {
                "name": nm,
                "base_url": url,
                "type": t,
                "auth_method": auth_method,
                "checkin_action": checkin_action,
                "script": str(row.get("script") or ""),
                "script_args": accounts_store.normalize_script_args(row.get("script_args")),
                "_script_args_text": json.dumps(accounts_store.normalize_script_args(row.get("script_args")), ensure_ascii=False, indent=2) if accounts_store.normalize_script_args(row.get("script_args")) else "{}",
                "script_timeout": accounts_store.parse_script_timeout(row.get("script_timeout"), 120),
                "api_variant": api_variant,
                "oauth_provider": oauth_provider,
                "oauth_account": oauth_account,
                "oauth_fallback_provider": accounts_store.normalize_oauth_provider(row.get("oauth_fallback_provider")),
                "oauth_fallback_account": accounts_store.normalize_oauth_account(row.get("oauth_fallback_account")),
                "enabled": accounts_store.parse_enabled(row.get("enabled"), True),
                "user_id": row.get("user_id", ""),
                "access_token": row.get("access_token", ""),
                "cookie": row.get("cookie", ""),
                "browser_state": "" if checkin_action == "relogin" or (checkin_action == "browser_script" and auth_method == "oauth") else row.get("browser_state", ""),
                "proxy": row.get("proxy", ""),
                "verify_ssl": accounts_store.parse_enabled(row.get("verify_ssl"), True),
            }
        )
    return out


def _button(text: str, kind: str = "ghost") -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setProperty("kind", kind)
    return btn


def _badge(text: str, fg: str, bg: str) -> QLabel:
    label = QLabel(text)
    label.setAlignment(Qt.AlignCenter)
    label.setStyleSheet(
        f"""
        QLabel {{
            color: {fg};
            background: {bg};
            border-radius: 9px;
            padding: 3px 9px;
            font-size: 11px;
            font-weight: 700;
        }}
        """
    )
    return label


class NoWheelComboBox(QComboBox):
    """禁止滚轮直接改变选项；下拉框仍可正常点击选择。"""

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        event.ignore()


def _query_failure_label(status: str, *, compact: bool = False) -> str:
    key = (status or "error").strip().lower()
    labels = {
        "need_login": ("🔐 登录失效", "🔐 失效"),
        "need_verification": ("⚠ 需人机验证", "⚠ 验证"),
        "need_config": ("⚙ 配置缺失", "⚙ 配置"),
        "network_error": ("🌐 站点不可达", "🌐 不可达"),
        "error": ("❌ 查询失败", "❌ 失败"),
    }
    full, short = labels.get(key, labels["error"])
    return short if compact else full


def _query_failure_toast(status: str, message: str) -> str:
    key = (status or "error").strip().lower()
    prefix = {
        "need_login": "登录失效",
        "need_verification": "需要验证",
        "need_config": "配置缺失",
        "network_error": "站点不可达/网络异常",
        "error": "查询失败",
    }.get(key, "查询失败")
    return f"{prefix}：{message}" if message else prefix


def _card_shadow(widget: QWidget) -> None:
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 8)
    shadow.setColor(QColor(15, 23, 42, 18))
    widget.setGraphicsEffect(shadow)


# ── 站点列表 Item ─────────────────────────────────────────────────────────────
class SiteItemWidget(QWidget):
    def __init__(self, row: dict[str, Any], selected: bool = False, owner=None, real_idx: int | None = None, status: dict[str, Any] | None = None):
        super().__init__()
        self.owner = owner
        self.real_idx = real_idx
        self.setObjectName("siteItem")
        self.setProperty("selected", selected)
        self._build(row, status)
        self._apply_selected(selected)

    def _build(self, row: dict[str, Any], status: dict[str, Any] | None = None) -> None:
        self.setMaximumWidth(328)  # 防止长 URL 撑宽列表（侧栏固定 360）
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 12, 10)
        root.setSpacing(9)

        self.handle = QLabel("⋮⋮")
        self.handle.setObjectName("dragHandle")
        self.handle.setAlignment(Qt.AlignCenter)
        self.handle.setFixedWidth(14)
        root.addWidget(self.handle, 0, Qt.AlignVCenter)

        self.dot = QLabel()
        self.dot.setFixedSize(10, 10)
        self.dot.setObjectName("statusDot")
        root.addWidget(self.dot, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(3)
        root.addLayout(text_col, 1)

        self.name = QLabel()
        self.name.setObjectName("siteName")
        self.name.setTextFormat(Qt.PlainText)
        self.name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text_col.addWidget(self.name)

        self.url = QLabel()
        self.url.setObjectName("siteUrl")
        self.url.setTextFormat(Qt.PlainText)
        self.url.setWordWrap(False)
        self.url.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        text_col.addWidget(self.url)

        # 状态行：签到状态徽标 + 额度
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.status_pill = QLabel()
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setAlignment(Qt.AlignCenter)
        status_row.addWidget(self.status_pill)
        self.quota_label = QLabel()
        self.quota_label.setObjectName("quotaMini")
        self.quota_label.setFont(QFont(MONO, 10, QFont.Bold))
        status_row.addWidget(self.quota_label)
        status_row.addStretch(1)
        text_col.addLayout(status_row)

        badge_col = QVBoxLayout()
        badge_col.setContentsMargins(0, 0, 0, 0)
        badge_col.setSpacing(5)
        self.type_badge = QLabel()
        self.type_badge.setAlignment(Qt.AlignCenter)
        badge_col.addWidget(self.type_badge)
        self.state_btn = QPushButton()
        self.state_btn.setObjectName("stateToggle")
        self.state_btn.setCursor(Qt.PointingHandCursor)
        self.state_btn.clicked.connect(self._toggle_enabled)
        badge_col.addWidget(self.state_btn)
        root.addLayout(badge_col, 0)

        self.update_row(row, status)

    def _toggle_enabled(self) -> None:
        if self.owner is not None and self.real_idx is not None:
            self.owner._toggle_enabled(self.real_idx)

    def update_row(self, row: dict[str, Any], status: dict[str, Any] | None = None) -> None:
        enabled = bool(row.get("enabled"))
        self.setProperty("enabledState", "on" if enabled else "off")
        self.name.setText(row.get("name") or "（未命名）")
        self.url.setText(row.get("base_url") or "—")
        self.url.setToolTip(row.get("base_url") or "")
        self.dot.setStyleSheet(
            f"QLabel {{ background: {C['ok'] if enabled else C['mute']}; border-radius: 5px; }}"
        )
        self.state_btn.setText("启用" if enabled else "禁用")
        self.state_btn.setProperty("state", "on" if enabled else "off")
        self.state_btn.style().unpolish(self.state_btn)
        self.state_btn.style().polish(self.state_btn)
        info = TYPE_INFO.get(row.get("type"), TYPE_INFO["newapi"])
        self.type_badge.setText(info["label"])
        self.type_badge.setStyleSheet(
            f"""
            QLabel {{
                color: {info['fg']};
                background: {info['bg']};
                border-radius: 9px;
                padding: 3px 8px;
                font-size: 11px;
                font-weight: 700;
            }}
            """
        )
        self._render_status(status)

    def _render_status(self, status: dict[str, Any] | None) -> None:
        """渲染状态徽标 + 额度（status=None 表示无数据）。"""
        if not status:
            self.status_pill.setText("○ 未查询")
            self.status_pill.setProperty("kind", "unknown")
            self.status_pill.setToolTip("")
            self.quota_label.setText("")
            self.quota_label.setToolTip("")
        else:
            checked_in = status.get("checked_in")
            quota = status.get("quota_usd")
            message = str(status.get("message") or "")
            if checked_in is True:
                self.status_pill.setText("🎁 已签到")
                self.status_pill.setProperty("kind", "done")
            elif checked_in is False:
                self.status_pill.setText("○ 待签到")
                self.status_pill.setProperty("kind", "todo")
            elif status.get("ok") is False:
                self.status_pill.setText(_query_failure_label(str(status.get("status") or "error"), compact=True))
                self.status_pill.setProperty("kind", "fail")
            else:
                self.status_pill.setText("—")
                self.status_pill.setProperty("kind", "unknown")
            self.status_pill.setToolTip(message)

            def _fmt(value: float) -> str:
                # 自适应格式：>=0.01 显示 2 位小数，<0.01 显示 4 位小数
                return f"${value:.2f}" if value >= 0.01 else f"${value:.4f}"

            if quota is not None:
                suffix = "" if not status.get("cached") else " (缓存)"
                self.quota_label.setText(f"{_fmt(quota)}{suffix}")
                self.quota_label.setToolTip(message)
            else:
                # 失效/未取到实时额度时，回退展示失效前的历史额度（灰显标注）。
                last_quota = status.get("last_quota_usd")
                if isinstance(last_quota, (int, float)):
                    self.quota_label.setText(f"{_fmt(last_quota)} (失效前)")
                    self.quota_label.setToolTip(f"失效前的最后额度\n{message}" if message else "失效前的最后额度")
                else:
                    self.quota_label.setText("")
                    self.quota_label.setToolTip(message)
        self.status_pill.style().unpolish(self.status_pill)
        self.status_pill.style().polish(self.status_pill)

    def _apply_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)


class SiteListWidget(QListWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.setProperty("dragging", True)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().dropEvent(event)
        self.setProperty("dragging", False)
        self.style().unpolish(self)
        self.style().polish(self)
        self.owner._sync_order_from_list()


# ── 新增站点弹窗 ───────────────────────────────────────────────────────────────
class TypeDialog(QDialog):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.chosen: str | None = None
        self.setWindowTitle("新增站点")
        self.setModal(True)
        self.setFixedSize(430, 400)
        self.setObjectName("typeDialog")
        self._build()

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(12)

        title = QLabel("选择站点类型")
        title.setObjectName("dialogTitle")
        root.addWidget(title)

        desc = QLabel("不同类型决定签到接口与所需凭据。")
        desc.setObjectName("hintText")
        root.addWidget(desc)

        opts = [
            ("newapi", "New API", "Cookie / Access Token · 可选 OAuth 重登/访问保活"),
            ("sub2api", "Sub2API", "Bearer Token（localStorage auth_token）"),
        ]
        for t, title_text, desc_text in opts:
            root.addWidget(self._option(t, title_text, desc_text))

        root.addItem(QSpacerItem(1, 1, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def _option(self, t: str, title_text: str, desc_text: str) -> QPushButton:
        info = TYPE_INFO[t]
        btn = QPushButton()
        btn.setObjectName("typeOption")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setMinimumHeight(64)
        layout = QHBoxLayout(btn)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(13)

        layout.addWidget(_badge(info["label"], info["fg"], info["bg"]))

        col = QVBoxLayout()
        col.setSpacing(3)
        name = QLabel(title_text)
        name.setObjectName("optionTitle")
        sub = QLabel(desc_text)
        sub.setObjectName("optionDesc")
        col.addWidget(name)
        col.addWidget(sub)
        layout.addLayout(col, 1)

        btn.clicked.connect(lambda: self._pick(t))
        return btn

    def _pick(self, t: str) -> None:
        self.chosen = t
        self.accept()


# ── 浏览器后台线程 ─────────────────────────────────────────────────────────────
class BrowserWorker(QThread):
    """在后台线程跑 Playwright 操作，避免阻塞 UI。

    action ∈ {"capture", "verify", "checkin"}：
      - capture ：有头浏览器人工登录捕获登录态，结束返回 base64 state；
      - verify  ：无头检测登录态是否有效；
      - checkin ：无头自动重放 OAuth 触发发额度。
    通过信号把进度/结果回传主线程（Qt 信号跨线程安全）。
    """

    progress = Signal(str)          # 进度日志
    finished_ok = Signal(dict)      # 成功结果（payload dict）
    failed = Signal(str)            # 失败原因

    def __init__(self, action: str, params: dict[str, Any], parent=None):
        super().__init__(parent)
        self.action = action
        self.params = params
        self._close_requested = False

    def request_close(self) -> None:
        """capture 模式：用户点「完成登录」后置位，让 wait_for_close 返回。"""
        self._close_requested = True

    def _context(self) -> dict[str, Any]:
        p = self.params
        return {
            "action": self.action,
            "site": p.get("name") or p.get("base_url"),
            "base_url": p.get("base_url", ""),
            "site_profile": p.get("site_profile") or p.get("type", ""),
            "auth_method": p.get("auth_method", ""),
            "checkin_action": p.get("checkin_action", ""),
        }

    def _fail(self, message: str, exc: BaseException | None = None) -> None:
        tb = traceback.format_exc() if exc is not None else ""
        text = f"{message}：{exc}" if exc is not None else message
        _bg_log("ERROR", text, traceback=tb, **self._context())
        self.failed.emit(mask_secrets(text))

    def run(self) -> None:  # noqa: D401 - QThread 入口
        log = self.progress.emit
        p = self.params
        started = time.perf_counter()
        _bg_log("INFO", "后台任务开始", **self._context())

        # site_checkin：走统一入口 providers.run_checkin，适用于所有站点类型
        # （newapi/sub2api/访问保活/OAuth 重登）。不依赖 browser_session。
        if self.action == "site_checkin":
            self._run_site_checkin(log)
            return

        # query：只读查询额度 + 签到状态（不执行签到）
        if self.action == "query":
            self._run_query(log)
            return

        try:
            from browser import session as browser_session
            import asyncio
        except Exception as exc:
            self._fail("加载 browser_session 失败", exc)
            return

        # 在 QThread 中用 asyncio.run() 桥接 async session（阻塞执行）
        async def _wait_for_close_async() -> None:
            """capture 模式：异步等待主线程调用 request_close"""
            waited = 0.0
            while not self._close_requested and waited < 600.0:
                await asyncio.sleep(0.2)
                waited += 0.2

        try:
            if self.action == "capture":
                if p.get("auth_method") == "oauth":
                    result = browser_session.run_sync(browser_session.capture_oauth_state(
                        oauth_provider=p.get("oauth_provider", "linuxdo"),
                        proxy=p.get("proxy", ""),
                        log=log,
                        wait_for_close=_wait_for_close_async,
                    ))
                elif (p.get("site_profile") or p.get("type")) == "sub2api":
                    result = browser_session.run_sync(browser_session.capture_sub2api_login(
                        base_url=p["base_url"],
                        proxy=p.get("proxy", ""),
                        log=log,
                        wait_for_close=_wait_for_close_async,
                    ))
                else:
                    result = browser_session.run_sync(browser_session.capture_login(
                        base_url=p["base_url"],
                        fallback_uid=p.get("fallback_uid", ""),
                        proxy=p.get("proxy", ""),
                        log=log,
                        wait_for_close=_wait_for_close_async,
                    ))
            elif self.action == "verify":
                if (p.get("site_profile") or p.get("type")) == "sub2api":
                    # sub2api 无 /api/user/self，用 browser_state 刷新 token 检测有效性
                    token = browser_session.run_sync(browser_session.capture_sub2api_token(
                        base_url=p["base_url"],
                        browser_state_text=p.get("browser_state", ""),
                        proxy=p.get("proxy", ""),
                        log=log,
                    ))
                    if token:
                        result = {"ok": True, "message": f"登录态有效，已刷新 auth_token（{len(token)} 字符）"}
                    else:
                        result = {"ok": False, "message": "登录态无效或无法刷新 token，请重新捕获。"}
                else:
                    result = browser_session.run_sync(browser_session.verify_state(
                        base_url=p["base_url"],
                        browser_state_text=p.get("browser_state", ""),
                        fallback_uid=p.get("fallback_uid", ""),
                        proxy=p.get("proxy", ""),
                        log=log,
                    ))
            elif self.action == "checkin":
                result = browser_session.run_sync(browser_session.run_oauth_checkin(
                    base_url=p["base_url"],
                    browser_state_text=p.get("browser_state", ""),
                    oauth_provider=p.get("oauth_provider", "linuxdo"),
                    fallback_uid=p.get("fallback_uid", ""),
                    proxy=p.get("proxy", ""),
                    log=log,
                ))
            else:
                self._fail(f"未知操作：{self.action}")
                return
        except browser_session.BrowserSessionError as exc:
            self._fail("浏览器会话失败", exc)
            return
        except Exception as exc:
            self._fail("浏览器操作异常", exc)
            return

        ok = bool(result.get("ok", True)) if isinstance(result, dict) else True
        _bg_log("INFO" if ok else "WARN", "后台任务完成", ok=ok, duration=f"{time.perf_counter() - started:.2f}s", result=result, **self._context())
        self.finished_ok.emit(result)

    def _run_site_checkin(self, log) -> None:
        """走统一入口 providers.run_checkin，对所有站点类型执行一次真实签到。"""
        try:
            import providers
        except Exception as exc:
            self._fail("加载 providers 失败", exc)
            return

        p = self.params
        started = time.perf_counter()
        try:
            site = accounts_store.site_config_from_mapping(p)
            log(f"开始测试签到（{site.site_profile} / {site.auth_method} / {site.checkin_action}）…")
            _bg_log("INFO", "测试签到开始", **self._context())
            result = providers.run_checkin(site)
        except Exception as exc:
            self._fail("签到异常", exc)
            return

        ok = result.status in ("success", "already_done")
        _bg_log(
            "INFO" if ok else "WARN",
            "测试签到完成",
            status=result.status,
            result_message=result.message,
            detail=result.detail,
            duration=f"{time.perf_counter() - started:.2f}s",
            **self._context(),
        )
        self.finished_ok.emit({
            "ok": ok,
            "status": result.status,
            "message": result.message,
            "detail": result.detail,
        })

    def _run_query(self, log) -> None:
        """走 providers.query_status 只读查询额度 + 签到状态。"""
        try:
            import providers
        except Exception as exc:
            self._fail("加载 providers 失败", exc)
            return

        p = self.params
        started = time.perf_counter()
        try:
            site = accounts_store.site_config_from_mapping(p)
            log(f"查询额度（{site.site_profile} / {site.auth_method} / {site.checkin_action}）…")
            _bg_log("INFO", "查询开始", **self._context())
            qs = providers.query_status(site)
        except Exception as exc:
            self._fail("查询异常", exc)
            return

        _bg_log(
            "INFO" if qs.ok else "WARN",
            "查询完成",
            ok=qs.ok,
            query_status=qs.status,
            quota_usd=qs.quota_usd,
            checked_in=qs.checked_in,
            result_message=qs.message,
            detail=qs.detail,
            duration=f"{time.perf_counter() - started:.2f}s",
            **self._context(),
        )
        self.finished_ok.emit({
            "query": True,
            "ok": qs.ok,
            "status": qs.status,
            "quota_usd": qs.quota_usd,
            "checked_in": qs.checked_in,
            "message": qs.message,
            "detail": qs.detail,
        })


class BatchTask(QRunnable):
    """独立任务：查询/签到单个站点。"""

    class _Signals(QObject):
        finished = Signal(str, dict)

    def __init__(self, action: str, params: dict[str, Any], callback):
        super().__init__()
        self.action = action
        self.params = params
        self.signals = self._Signals()
        self.signals.finished.connect(callback)
        # 不自动删除：跨线程 finished 是排队投递，run() 返回后线程池会立即销毁
        # runnable，导致承载信号的 QObject 在主线程派发前析构、回调被丢弃。改由
        # App 持有引用（见 _start_task），信号派发完成后再释放。
        self.setAutoDelete(False)

    def run(self) -> None:
        p = self.params
        task_name = p.get("name") or p.get("base_url") or "未命名站点"
        context = {
            "action": self.action,
            "site": task_name,
            "base_url": p.get("base_url", ""),
            "site_profile": p.get("site_profile") or p.get("type", ""),
            "auth_method": p.get("auth_method", ""),
            "checkin_action": p.get("checkin_action", ""),
        }
        started = time.perf_counter()
        _bg_log("INFO", "独立任务开始", **context)
        try:
            import providers

            site = accounts_store.site_config_from_mapping(p)

            if self.action == "query":
                qs = providers.query_status(site)
                result = {
                    "ok": qs.ok,
                    "query": True,
                    "status": qs.status,
                    "quota_usd": qs.quota_usd,
                    "checked_in": qs.checked_in,
                    "message": qs.message,
                    "detail": qs.detail,
                }
            else:  # checkin
                cr = providers.run_checkin(site)
                result = {
                    "ok": cr.status in ("success", "already_done"),
                    "status": cr.status,
                    "message": cr.message,
                    "detail": cr.detail,
                }
            _bg_log(
                "INFO" if result.get("ok") else "WARN",
                "独立任务完成",
                duration=f"{time.perf_counter() - started:.2f}s",
                result=result,
                **context,
            )
        except Exception as exc:
            result = _error_result("独立任务异常", exc, duration=f"{time.perf_counter() - started:.2f}s", **context)
            result["query"] = self.action == "query"

        self.signals.finished.emit(task_name, result)


# ── 主窗口 ────────────────────────────────────────────────────────────────────
class App(QMainWindow):
    def __init__(self):
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.oauth_states: dict[str, dict[str, Any]] = {}
        self.filtered_indices: list[int] = []
        self.cur: int | None = None
        self._lock = False
        self._dirty = False
        self._saved_snapshot: dict[str, Any] = {"accounts": [], "oauth_states": {}}
        self._type_buttons: dict[str, QPushButton] = {}
        self._worker: BrowserWorker | None = None
        self._capture_dialog: QMessageBox | None = None
        self._thread_pool = QThreadPool.globalInstance()
        self._thread_pool.setMaxThreadCount(5)
        # 站点状态缓存：{base_url|name: {"quota_usd":..., "checked_in":..., "ok":..., "status":..., "message":...}}
        self._status_cache: dict[str, dict[str, Any]] = {}
        # 正在运行任务的站点键集合（按 base_url 互斥，防止同一站点多个任务并发）
        self._checkin_running: set[str] = set()
        # 持有在飞的 BatchTask 引用，防止其信号 QObject 在主线程派发前被销毁导致回调丢失
        self._active_tasks: set[BatchTask] = set()
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self.toast.setText(""))

        self._win()
        self._build()
        self._hotkeys()
        self._reload()

    # ── 窗口 / 样式 ──
    def _win(self) -> None:
        self.setWindowTitle("公益站 & 账号管理")
        self.resize(1180, 760)
        self.setMinimumSize(980, 620)
        self.setStyleSheet(APP_STYLE)

    # ── 布局骨架 ──
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._topbar())

        body = QHBoxLayout()
        body.setContentsMargins(18, 18, 18, 14)
        body.setSpacing(14)
        root.addLayout(body, 1)

        body.addWidget(self._sidebar(), 0)
        body.addWidget(self._editor(), 1)

        root.addWidget(self._footer())

    def _topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(64)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(12)

        mark = QLabel("✓")
        mark.setObjectName("mark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(34, 34)
        layout.addWidget(mark)

        title = QLabel("公益站 & 账号管理")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        self.status = QLabel("● 已保存")
        self.status.setObjectName("saveStatus")
        self.status.setProperty("state", "saved")
        layout.addWidget(self.status)
        return bar

    def _sidebar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("sidebar")
        wrap.setFixedWidth(360)
        _card_shadow(wrap)

        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("站点")
        title.setObjectName("sectionTitle")
        header.addWidget(title)
        self.count = QLabel("0")
        self.count.setObjectName("countBadge")
        header.addWidget(self.count)
        header.addStretch(1)
        add_btn = _button("＋ 新增", "primary")
        add_btn.clicked.connect(self._add)
        header.addWidget(add_btn)
        layout.addLayout(header)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchInput")
        self.search_edit.setPlaceholderText("搜索站点名称 / 地址 / 类型")
        self.search_edit.textChanged.connect(self._render_list)
        layout.addWidget(self.search_edit)

        self.sidebar_hint = QLabel("拖动排序 · 点击右侧启用 / 禁用")
        self.sidebar_hint.setObjectName("sidebarHint")
        layout.addWidget(self.sidebar_hint)

        self.listw = SiteListWidget(self)
        self.listw.setObjectName("siteList")
        self.listw.setSpacing(6)
        self.listw.setDragDropMode(QAbstractItemView.InternalMove)
        self.listw.setDefaultDropAction(Qt.MoveAction)
        self.listw.setDragEnabled(True)
        self.listw.setAcceptDrops(True)
        self.listw.setDropIndicatorShown(True)
        self.listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listw.currentRowChanged.connect(self._select_visible)
        layout.addWidget(self.listw, 1)
        return wrap

    def _editor(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        summary = QFrame()
        summary.setObjectName("summaryCard")
        _card_shadow(summary)
        scol = QVBoxLayout(summary)
        scol.setContentsMargins(18, 14, 18, 16)
        scol.setSpacing(12)

        # ── 上排：标题 + 类型徽标 + 启用状态 + 复制/删除 ──
        top_row = QHBoxLayout()
        top_row.setContentsMargins(0, 0, 0, 0)
        top_row.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(3)
        title_line = QHBoxLayout()
        title_line.setContentsMargins(0, 0, 0, 0)
        title_line.setSpacing(8)
        self.edit_title = QLabel("未选择站点")
        self.edit_title.setObjectName("editTitle")
        title_line.addWidget(self.edit_title)
        self.summary_badge = QLabel("—")
        self.summary_badge.setAlignment(Qt.AlignCenter)
        title_line.addWidget(self.summary_badge)
        title_line.addStretch(1)
        title_col.addLayout(title_line)
        self.summary_url = QLabel("从左侧选择一个站点，或点击新增开始配置。")
        self.summary_url.setObjectName("summaryUrl")
        self.summary_url.setTextFormat(Qt.PlainText)
        title_col.addWidget(self.summary_url)
        top_row.addLayout(title_col, 1)

        self.summary_state = QLabel("—")
        self.summary_state.setObjectName("summaryState")
        self.summary_state.setAlignment(Qt.AlignCenter)
        top_row.addWidget(self.summary_state, 0, Qt.AlignVCenter)

        self.btn_dup = _button("复制", "tool")
        self.btn_del = _button("删除", "danger")
        self.btn_dup.clicked.connect(self._dup)
        self.btn_del.clicked.connect(self._del)
        for btn in (self.btn_dup, self.btn_del):
            top_row.addWidget(btn, 0, Qt.AlignVCenter)
        scol.addLayout(top_row)

        # ── 下排：额度信息块 + 签到状态 + 操作按钮 ──
        info_row = QHBoxLayout()
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.setSpacing(12)

        # 额度卡片块（浅色背景，聚焦展示）
        quota_box = QFrame()
        quota_box.setObjectName("quotaBox")
        qb = QHBoxLayout(quota_box)
        qb.setContentsMargins(14, 8, 10, 8)
        qb.setSpacing(10)
        qb_text = QVBoxLayout()
        qb_text.setContentsMargins(0, 0, 0, 0)
        qb_text.setSpacing(1)
        qcap = QLabel("当前额度")
        qcap.setObjectName("quotaCaption")
        qb_text.addWidget(qcap)
        self.quota_value = QLabel("—")
        self.quota_value.setObjectName("quotaValue")
        self.quota_value.setFont(QFont(MONO, 17, QFont.Bold))
        qb_text.addWidget(self.quota_value)
        qb.addLayout(qb_text)
        self.btn_refresh = QPushButton("🔄")
        self.btn_refresh.setObjectName("iconButton")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.setFixedSize(30, 30)
        self.btn_refresh.setToolTip("实时查询额度与签到状态")
        self.btn_refresh.clicked.connect(self._refresh_status)
        qb.addWidget(self.btn_refresh, 0, Qt.AlignVCenter)
        info_row.addWidget(quota_box, 0)

        # 签到状态徽标
        self.checkin_pill = QLabel("未查询")
        self.checkin_pill.setObjectName("statusPillLg")
        self.checkin_pill.setProperty("kind", "unknown")
        self.checkin_pill.setAlignment(Qt.AlignCenter)
        info_row.addWidget(self.checkin_pill, 0, Qt.AlignVCenter)

        info_row.addStretch(1)

        # 立即签到（主操作）
        self.btn_checkin_now = _button("立即签到", "primary")
        self.btn_checkin_now.clicked.connect(self._checkin_current)
        info_row.addWidget(self.btn_checkin_now, 0, Qt.AlignVCenter)
        scol.addLayout(info_row)

        layout.addWidget(summary)

        scroll = QScrollArea()
        scroll.setObjectName("editorScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        form_host = QWidget()
        form_host.setObjectName("formHost")
        self.form = QVBoxLayout(form_host)
        self.form.setContentsMargins(2, 2, 18, 20)
        self.form.setSpacing(14)
        scroll.setWidget(form_host)
        layout.addWidget(scroll, 1)

        self._build_form()
        return wrap

    def _footer(self) -> QWidget:
        foot = QFrame()
        foot.setObjectName("footer")
        foot.setFixedHeight(64)
        layout = QHBoxLayout(foot)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(9)

        self.toast = QLabel("Ctrl+S 保存 · Ctrl+N 新增 · Del 删除")
        self.toast.setObjectName("toast")
        layout.addWidget(self.toast, 1)

        reload_btn = _button("重新加载", "ghost")
        reload_btn.clicked.connect(self._reload)
        layout.addWidget(reload_btn)

        export_btn = _button("导出 Secret", "ghost")
        export_btn.clicked.connect(self._export)
        layout.addWidget(export_btn)

        save_btn = _button("保存全部", "primary")
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)
        return foot

    # ── 表单 ──
    def _build_form(self) -> None:
        columns = QHBoxLayout()
        columns.setContentsMargins(0, 0, 0, 0)
        columns.setSpacing(14)
        self.form.addLayout(columns)

        site_card = self._card("站点信息", parent_layout=columns)
        site_layout = site_card.layout()

        self.name_edit = self._line(site_layout, "站点名称")
        self.base_edit = self._line(site_layout, "站点地址", "形如 https://example.com")

        self._type_segment(site_layout)

        # 登录方式（auth_method）：如何获得已认证会话
        auth_wrap = self._field(site_layout, "登录方式")
        self.auth_combo = NoWheelComboBox()
        self.auth_combo.setObjectName("input")
        self.auth_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in AUTH_METHODS:
            self.auth_combo.addItem(AUTH_METHOD_LABELS.get(m, m), m)
        auth_wrap.layout().addWidget(self.auth_combo)

        # 签到方式（checkin_action）：如何触发发额度
        action_wrap = self._field(site_layout, "签到方式")
        self.action_combo = NoWheelComboBox()
        self.action_combo.setObjectName("input")
        self.action_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in CHECKIN_ACTIONS:
            self.action_combo.addItem(ACTION_LABELS.get(m, m), m)
        action_wrap.layout().addWidget(self.action_combo)

        # OAuth provider/account：OAuth 登录方式可见，决定使用哪份共享第三方登录态
        self.oauth_provider_wrap = self._field(site_layout, "OAuth 提供商", "共享登录态来源")
        self.oauth_provider_combo = NoWheelComboBox()
        self.oauth_provider_combo.setObjectName("input")
        self.oauth_provider_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in OAUTH_PROVIDERS:
            self.oauth_provider_combo.addItem(OAUTH_PROVIDER_LABELS.get(m, m), m)
        self.oauth_provider_wrap.layout().addWidget(self.oauth_provider_combo)

        self.oauth_account_wrap = self._field(site_layout, "OAuth 账号", "同一提供商可保存多个账号")
        account_row = QHBoxLayout()
        account_row.setContentsMargins(0, 0, 0, 0)
        account_row.setSpacing(8)
        self.oauth_account_combo = NoWheelComboBox()
        self.oauth_account_combo.setObjectName("input")
        self.oauth_account_combo.setEditable(True)
        self.oauth_account_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        account_row.addWidget(self.oauth_account_combo, 1)
        self.btn_oauth_refresh = _button("刷新账号", "tool")
        self.btn_oauth_refresh.clicked.connect(self._reload_oauth_accounts)
        account_row.addWidget(self.btn_oauth_refresh)
        self.btn_oauth_delete = _button("删除登录态", "tool")
        self.btn_oauth_delete.clicked.connect(self._delete_oauth_account)
        account_row.addWidget(self.btn_oauth_delete)
        self.oauth_account_wrap.layout().addLayout(account_row)

        # 接口变体（api_variant）：仅 newapi + 接口签到 时可见
        self.variant_wrap = self._field(site_layout, "接口变体", "仅 New API 接口签到")
        self.variant_combo = NoWheelComboBox()
        self.variant_combo.setObjectName("input")
        self.variant_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in API_VARIANTS:
            self.variant_combo.addItem(API_VARIANT_LABELS.get(m, m), m)
        self.variant_wrap.layout().addWidget(self.variant_combo)

        self.script_wrap = self._field(site_layout, "脚本路径", "仓库内相对路径，例如 scripts/checkin/100xlabs.py")
        self.script_edit = QLineEdit()
        self.script_edit.setObjectName("input")
        self.script_edit.setPlaceholderText("scripts/checkin/100xlabs.py")
        self.script_wrap.layout().addWidget(self.script_edit)

        self.script_args_wrap = self._field(site_layout, "脚本参数 JSON", "传给 site.script_args")
        self.script_args_edit = QPlainTextEdit()
        self.script_args_edit.setObjectName("textInput")
        self.script_args_edit.setFixedHeight(88)
        self.script_args_edit.setPlaceholderText('{\n  "checkin_text": "签到"\n}')
        self.script_args_wrap.layout().addWidget(self.script_args_edit)

        self.script_timeout_wrap = self._field(site_layout, "脚本超时（秒）", "默认 120")
        self.script_timeout_edit = QLineEdit()
        self.script_timeout_edit.setObjectName("input")
        self.script_timeout_edit.setPlaceholderText("120")
        self.script_timeout_wrap.layout().addWidget(self.script_timeout_edit)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("hintText")
        self.mode_hint.setWordWrap(True)
        site_layout.addWidget(self.mode_hint)

        site_layout.addStretch(1)

        cred_card = self._card("认证凭据", "保存后写入本地 ACCOUNTS.json（已被 .gitignore）", parent_layout=columns)
        cred_layout = cred_card.layout()

        self.token_edit = self._line(cred_layout, "Access Token", mono=True, secret=False)
        self.uid_edit = self._line(cred_layout, "用户 ID", "newapi 的 New-Api-User")

        cookie_wrap = self._field(cred_layout, "Cookie")
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setObjectName("textInput")
        self.cookie_edit.setFixedHeight(104)
        cookie_wrap.layout().addWidget(self.cookie_edit)

        # 登录态：browser 保存站点级登录态；oauth 使用 provider/account 共享登录态
        self.state_wrap = self._field(
            cred_layout, "站点登录状态", "poc_oauth.py setup 产物，用于自动登录 / 刷新 token"
        )
        self.state_edit = QPlainTextEdit()
        self.state_edit.setObjectName("textInput")
        self.state_edit.setFixedHeight(80)
        self.state_edit.setPlaceholderText("非 relogin 场景可粘贴 base64 浏览器登录态（如 sub2api 自动刷新）")
        self.state_wrap.layout().addWidget(self.state_edit)
        self.oauth_state_status = QLabel("")
        self.oauth_state_status.setObjectName("hintText")
        self.oauth_state_status.setWordWrap(True)
        self.state_wrap.layout().addWidget(self.oauth_state_status)

        # 可选 OAuth 兜底：从已保存的共享 OAuth provider/account 中选择。
        # 选“不使用”时失效直接报错，不启动浏览器。
        self.oauth_fallback_wrap = self._field(cred_layout, "可选 OAuth")
        fallback_row = QHBoxLayout()
        fallback_row.setContentsMargins(0, 0, 0, 0)
        fallback_row.setSpacing(8)
        self.oauth_fallback_combo = NoWheelComboBox()
        self.oauth_fallback_combo.setObjectName("input")
        self.oauth_fallback_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.oauth_fallback_combo.addItem("不使用", "")
        fallback_row.addWidget(self.oauth_fallback_combo, 1)
        self.btn_oauth_fallback_refresh = _button("刷新账号", "tool")
        self.btn_oauth_fallback_refresh.clicked.connect(self._reload_oauth_accounts)
        fallback_row.addWidget(self.btn_oauth_fallback_refresh)
        self.oauth_fallback_wrap.layout().addLayout(fallback_row)

        # 浏览器登录态操作（捕获 / 检测），整体随 state_wrap 显隐
        self.browser_ops = QWidget()
        ops_row = QHBoxLayout(self.browser_ops)
        ops_row.setContentsMargins(0, 2, 0, 0)
        ops_row.setSpacing(8)
        self.btn_capture = _button("浏览器登录捕获", "primary")
        self.btn_capture.clicked.connect(self._browser_capture)
        self.btn_verify = _button("检测登录态", "tool")
        self.btn_verify.clicked.connect(self._browser_verify)
        ops_row.addWidget(self.btn_capture)
        ops_row.addWidget(self.btn_verify)
        ops_row.addStretch(1)
        cred_layout.addWidget(self.browser_ops)

        # 代理（可选，支持 http/https/socks5）
        self.proxy_edit = self._line(cred_layout, "代理（可选）", "如 http://user:pass@host:port")

        self.verify_ssl_wrap = self._field(
            cred_layout,
            "TLS 证书校验",
            "默认开启；仅证书过期/链异常站点临时关闭",
        )
        self.verify_ssl_check = QCheckBox("校验 HTTPS 证书和主机名")
        self.verify_ssl_check.setObjectName("plainCheck")
        self.verify_ssl_check.setChecked(True)
        self.verify_ssl_wrap.layout().addWidget(self.verify_ssl_check)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 4, 0, 0)
        imp_btn = _button("从剪贴板导入", "tool")
        imp_btn.clicked.connect(self._imp)
        actions.addWidget(imp_btn)
        cp_btn = _button("复制凭据 JSON", "tool")
        cp_btn.clicked.connect(self._cpcred)
        actions.addWidget(cp_btn)
        # 测试签到：所有类型都可用（走统一入口 providers.run_checkin）
        self.btn_test = _button("测试签到", "tool")
        self.btn_test.clicked.connect(self._test_checkin)
        actions.addWidget(self.btn_test)
        actions.addStretch(1)
        cred_layout.addLayout(actions)

        self.form.addStretch(1)

        self.name_edit.textChanged.connect(self._flush)
        self.base_edit.textChanged.connect(self._flush)
        self.auth_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.action_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.oauth_provider_combo.currentIndexChanged.connect(self._on_oauth_provider_changed)
        self.oauth_account_combo.currentIndexChanged.connect(self._on_oauth_account_changed)
        self.oauth_fallback_combo.currentIndexChanged.connect(self._on_oauth_fallback_changed)
        if self.oauth_account_combo.lineEdit():
            self.oauth_account_combo.lineEdit().editingFinished.connect(self._on_combo_changed)
        self.variant_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.variant_combo.currentIndexChanged.connect(self._flush)
        self.script_edit.textChanged.connect(self._flush)
        self.script_args_edit.textChanged.connect(self._flush)
        self.script_timeout_edit.textChanged.connect(self._flush)
        self.token_edit.textChanged.connect(self._flush)
        self.uid_edit.textChanged.connect(self._flush)
        self.cookie_edit.textChanged.connect(self._flush)
        self.state_edit.textChanged.connect(self._flush)
        self.proxy_edit.textChanged.connect(self._flush)
        self.verify_ssl_check.stateChanged.connect(self._flush)

    def _card(self, title: str, subtitle: str = "", parent_layout: QHBoxLayout | QVBoxLayout | None = None) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        card.setMinimumWidth(0)
        _card_shadow(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        t = QLabel(title)
        t.setObjectName("cardTitle")
        header.addWidget(t)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("hintText")
            sub.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            header.addWidget(sub, 1)
        header.addStretch(0)
        layout.addLayout(header)

        target = parent_layout or self.form
        target.addWidget(card)
        return card

    def _field(self, parent_layout: QVBoxLayout, label: str, hint: str = "") -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(label)
        lab.setObjectName("fieldLabel")
        top.addWidget(lab)
        if hint:
            h = QLabel(hint)
            h.setObjectName("hintText")
            h.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            top.addWidget(h, 1)
        top.addStretch(0)
        lay.addLayout(top)

        parent_layout.addWidget(wrap)
        return wrap

    def _line(self, parent_layout: QVBoxLayout, label: str, hint: str = "", mono: bool = False, secret: bool = False) -> QLineEdit:
        wrap = self._field(parent_layout, label, hint)
        edit = QLineEdit()
        edit.setObjectName("input")
        if mono:
            edit.setFont(QFont(MONO, 10))
        if secret:
            edit.setEchoMode(QLineEdit.Password)
        wrap.layout().addWidget(edit)
        return edit

    def _type_segment(self, parent_layout: QVBoxLayout) -> None:
        wrap = self._field(parent_layout, "站点类型")
        seg = QFrame()
        seg.setObjectName("segment")
        row = QHBoxLayout(seg)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)

        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        for t in TYPES:
            btn = QPushButton(TYPE_INFO[t]["label"])
            btn.setObjectName("typeButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, tt=t: self._set_type(tt))
            self.type_group.addButton(btn)
            self._type_buttons[t] = btn
            row.addWidget(btn, 1)
        wrap.layout().addWidget(seg)

    # ── 快捷键 ──
    def _hotkeys(self) -> None:
        QShortcut(QKeySequence("Ctrl+S"), self, self._save)
        QShortcut(QKeySequence("Ctrl+N"), self, self._add)
        QShortcut(QKeySequence("Ctrl+L"), self, self._reload)
        QShortcut(QKeySequence("Delete"), self, self._del)

    # ── 数据装载 / 列表 ──
    def _reload(self) -> None:
        try:
            self.rows = _rows()
            self.oauth_states = accounts_store.load_oauth_states()
        except Exception as exc:
            QMessageBox.critical(self, "错误", str(exc))
            self.rows = []
            self.oauth_states = {}
        self._load_cached_status()
        self.cur = None
        self.search_edit.clear()
        self._render_list()
        if self.rows:
            self.listw.setCurrentRow(0)
            self._select_real(0)
        else:
            self._clear()
        self._mark_saved()

    # ── 状态缓存 ──
    def _status_key(self, row: dict[str, Any]) -> str:
        """状态缓存键：base_url + name 区分同站点不同账号。"""
        base = accounts_store.normalize_base_url(str(row.get("base_url") or ""))
        name = str(row.get("name") or "")
        return f"{base}|{name}"

    def _site_task_key(self, row: dict[str, Any]) -> str:
        """任务互斥键：同一 base_url 视为同一站点，不允许并发。"""
        base = accounts_store.normalize_base_url(str(row.get("base_url") or ""))
        return base or str(row.get("name") or "")

    def _try_start_site_task(self, idx: int, label: str = "任务") -> str:
        """尝试给站点加任务锁；返回锁 key，空串表示已被占用。"""
        if idx < 0 or idx >= len(self.rows):
            return ""
        key = self._site_task_key(self.rows[idx])
        if not key:
            return ""
        if key in self._checkin_running:
            self._say(f"该站点已有任务运行中，已跳过新的{label}")
            return ""
        self._checkin_running.add(key)
        return key

    def _finish_site_task(self, task_key: str) -> None:
        if task_key:
            self._checkin_running.discard(task_key)

    def _start_task(self, action: str, params: dict[str, Any], callback) -> "BatchTask":
        """创建并启动后台任务，持有其引用直到 finished 信号在主线程派发完毕。

        BatchTask 关闭了 autoDelete，若不保留引用，run() 返回后 Python 侧对象即可被
        回收，其承载 finished 信号的 QObject 随之析构，导致排队中的回调被 Qt 丢弃。
        """
        task = BatchTask(action, params, callback)
        self._active_tasks.add(task)
        # 回调（在 __init__ 中先行连接）执行完毕后再释放引用，交由 GC 回收。
        task.signals.finished.connect(lambda *_a, _t=task: self._active_tasks.discard(_t))
        self._thread_pool.start(task)
        return task

    @staticmethod
    def _detail_quota_usd(detail: dict[str, Any] | None) -> float | None:
        """从签到结果 detail 提取美元额度。

        newapi 返回内部 quota（需 /500000）；sub2api 已是美元（quota_is_usd=True）。
        """
        if not isinstance(detail, dict):
            return None
        cq = detail.get("current_quota")
        if not isinstance(cq, (int, float)):
            return None
        return float(cq) if detail.get("quota_is_usd") else float(cq) / 500000

    def _load_cached_status(self) -> None:
        """预填状态缓存：先读 results/checkin_result.json（批量签到结果），
        再合并 results/gui_status_cache.json（GUI 内实时查询/签到的最新额度）。

        GUI 缓存里带 saved_at 时间戳的条目更"新"（可能来自关程序前的手动查询），
        与批量结果按时间取新者，让重开程序仍能显示上次看到的额度。"""
        path = accounts_store.SCRIPT_DIR / "results" / "checkin_result.json"
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                rows = payload.get("results", []) if isinstance(payload, dict) else []
                batch_saved_at = str(payload.get("generated_at") or "") if isinstance(payload, dict) else ""
            except Exception:
                rows, batch_saved_at = [], ""
            for item in rows:
                if not isinstance(item, dict):
                    continue
                base = accounts_store.normalize_base_url(str(item.get("base_url") or ""))
                name = str(item.get("site") or "")
                if not base and not name:
                    continue
                key = f"{base}|{name}"
                # 解析额度字符串（如 "$246.1"）为 float
                quota_usd = None
                cq = str(item.get("current_quota") or "").lstrip("$")
                try:
                    quota_usd = float(cq) if cq else None
                except ValueError:
                    quota_usd = None
                cached_status = str(item.get("status") or "")
                ok = cached_status in ("success", "already_done")
                checked_in = True if ok else None
                self._status_cache[key] = {
                    "quota_usd": quota_usd if ok else None,
                    # 失效结果也保留解析到的历史额度，供渲染层灰显「失效前额度」。
                    "last_quota_usd": quota_usd,
                    "checked_in": checked_in,
                    "ok": ok,
                    "status": cached_status or ("success" if ok else "error"),
                    "message": item.get("note") or item.get("message") or "",
                    "cached": True,  # 标记为缓存（非实时）
                    "saved_at": batch_saved_at,
                }
        self._merge_gui_status_cache()

    def _merge_gui_status_cache(self) -> None:
        """读取 GUI 状态缓存，与已有缓存按 saved_at 取新者合并。"""
        path = accounts_store.SCRIPT_DIR / "results" / "gui_status_cache.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            existing = self._status_cache.get(key)
            if existing is not None:
                # 已有批量结果：仅当 GUI 缓存更新（saved_at 更大）时才覆盖。
                if str(entry.get("saved_at") or "") <= str(existing.get("saved_at") or ""):
                    continue
            merged = {
                "quota_usd": entry.get("quota_usd"),
                "last_quota_usd": entry.get("last_quota_usd") if entry.get("last_quota_usd") is not None else entry.get("quota_usd"),
                "checked_in": entry.get("checked_in"),
                "ok": bool(entry.get("ok")),
                "status": str(entry.get("status") or "error"),
                "message": str(entry.get("message") or ""),
                "cached": True,
                "saved_at": str(entry.get("saved_at") or ""),
            }
            self._status_cache[key] = merged

    def _save_gui_status_cache(self) -> None:
        """把当前 GUI 状态缓存中的实时结果落盘，供下次启动预填。"""
        results_dir = accounts_store.SCRIPT_DIR / "results"
        path = results_dir / "gui_status_cache.json"
        entries: dict[str, Any] = {}
        for key, status in self._status_cache.items():
            if not isinstance(status, dict):
                continue
            # 只持久化有额度或明确状态的条目，避免写入空占位。
            if status.get("quota_usd") is None and status.get("last_quota_usd") is None and not status.get("status"):
                continue
            entries[key] = {
                "quota_usd": status.get("quota_usd"),
                "last_quota_usd": status.get("last_quota_usd"),
                "checked_in": status.get("checked_in"),
                "ok": bool(status.get("ok")),
                "status": str(status.get("status") or ""),
                "message": str(status.get("message") or ""),
                "saved_at": str(status.get("saved_at") or ""),
            }
        payload = {"entries": entries}
        try:
            results_dir.mkdir(parents=True, exist_ok=True)
            with accounts_store.file_lock(path):
                accounts_store.atomic_write_text(
                    path, json.dumps(payload, ensure_ascii=False, indent=2)
                )
        except Exception:
            # 持久化失败不影响 GUI 运行。
            pass

    def _matches_filter(self, row: dict[str, Any], query: str) -> bool:
        if not query:
            return True
        info = TYPE_INFO.get(row.get("type"), TYPE_INFO["newapi"])
        haystack = " ".join(
            str(part).lower()
            for part in (
                row.get("name", ""),
                row.get("base_url", ""),
                row.get("type", ""),
                info.get("label", ""),
            )
        )
        return query in haystack

    def _visible_pos(self, real_idx: int | None) -> int:
        if real_idx is None:
            return -1
        try:
            return self.filtered_indices.index(real_idx)
        except ValueError:
            return -1

    def _render_list(self) -> None:
        query = self.search_edit.text().strip().lower() if hasattr(self, "search_edit") else ""
        self.filtered_indices = [idx for idx, row in enumerate(self.rows) if self._matches_filter(row, query)]

        drag_enabled = not query
        self.listw.setDragEnabled(drag_enabled)
        self.listw.setAcceptDrops(drag_enabled)
        self.listw.setDragDropMode(QAbstractItemView.InternalMove if drag_enabled else QAbstractItemView.NoDragDrop)
        self.sidebar_hint.setText("拖动排序 · 点击右侧启用 / 禁用" if drag_enabled else "搜索结果中不可排序，清空搜索后可拖动排序")

        self.listw.blockSignals(True)
        self.listw.clear()
        for real_idx in self.filtered_indices:
            row = self.rows[real_idx]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, real_idx)
            widget = SiteItemWidget(row, real_idx == self.cur, self, real_idx, self._status_cache.get(self._status_key(row)))
            item.setSizeHint(widget.sizeHint())
            self.listw.addItem(item)
            self.listw.setItemWidget(item, widget)
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self.listw.blockSignals(False)
        shown, total = len(self.filtered_indices), len(self.rows)
        self.count.setText(f"{shown}/{total}" if query else str(total))

    def _sync_order_from_list(self) -> None:
        if self.search_edit.text().strip():
            return
        if self.listw.count() != len(self.rows):
            return
        order = [self.listw.item(i).data(Qt.UserRole) for i in range(self.listw.count())]
        if order == list(range(len(self.rows))):
            return
        selected = self.rows[self.cur] if self.cur is not None and 0 <= self.cur < len(self.rows) else None
        self.rows = [self.rows[int(i)] for i in order]
        self.cur = next((i for i, row in enumerate(self.rows) if row is selected), None)
        self._render_list()
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self._sync_dirty_state()
        self._say("已更新站点顺序，保存后生效")

    def _toggle_enabled(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.rows):
            return
        if self.cur is not None:
            self._flush()
        self.rows[idx]["enabled"] = not bool(self.rows[idx].get("enabled"))
        self._refresh_row(idx)
        if idx == self.cur:
            self._update_summary(self.rows[idx])
        self._sync_dirty_state()
        self._say(f"已{'启用' if self.rows[idx]['enabled'] else '关闭'}「{self.rows[idx]['name'] or '未命名站点'}」")

    def _refresh_row(self, idx: int) -> None:
        pos = self._visible_pos(idx)
        if pos < 0 or pos >= self.listw.count():
            return
        item = self.listw.item(pos)
        widget = self.listw.itemWidget(item)
        if isinstance(widget, SiteItemWidget):
            widget.update_row(self.rows[idx], self._status_cache.get(self._status_key(self.rows[idx])))
            widget._apply_selected(idx == self.cur)

    def _refresh_selection(self) -> None:
        for idx in self.filtered_indices:
            self._refresh_row(idx)

    def _select_visible(self, visible_idx: int) -> None:
        if visible_idx < 0 or visible_idx >= len(self.filtered_indices):
            return
        self._select_real(self.filtered_indices[visible_idx])

    def _select_real(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.rows):
            return
        if self.cur is not None and self.cur != idx:
            self._flush()
        prev = self.cur
        self.cur = idx
        if prev is not None:
            self._refresh_row(prev)
        self._refresh_row(idx)
        self._load(idx)

    def _load(self, idx: int) -> None:
        self._lock = True
        row = self.rows[idx]
        self.name_edit.setText(row["name"])
        self.base_edit.setText(row["base_url"])
        self._set_type_value(row["type"] if row["type"] in TYPES else "newapi")
        self._set_combos(
            row.get("auth_method") or "cookie",
            row.get("checkin_action") or "api",
            row.get("api_variant") or "auto",
        )
        self._set_oauth_provider(accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo")
        self._refresh_oauth_account_choices(row.get("oauth_account") or accounts_store.DEFAULT_OAUTH_ACCOUNT)
        self._refresh_oauth_fallback_choices(
            str(row.get("oauth_fallback_provider") or ""),
            str(row.get("oauth_fallback_account") or ""),
        )
        self.script_edit.setText(str(row.get("script") or ""))
        script_args_text = row.get("_script_args_text")
        if script_args_text is None:
            script_args = accounts_store.normalize_script_args(row.get("script_args"))
            script_args_text = json.dumps(script_args, ensure_ascii=False, indent=2) if script_args else "{}"
        self.script_args_edit.setPlainText(str(script_args_text))
        self.script_timeout_edit.setText(str(accounts_store.parse_script_timeout(row.get("script_timeout"), 120)))
        self.state_edit.setPlainText(row.get("browser_state", ""))
        self.proxy_edit.setText(row.get("proxy", ""))
        self.verify_ssl_check.setChecked(accounts_store.parse_enabled(row.get("verify_ssl"), True))
        self.uid_edit.setText(row["user_id"])
        self.token_edit.setText(row["access_token"])
        self.cookie_edit.setPlainText(row["cookie"])
        self._update_summary(row)
        self._set_actions_enabled(True)
        self._lock = False
        self._sync_type()

    def _clear(self) -> None:
        self._lock = True
        self.cur = None
        self.name_edit.clear()
        self.base_edit.clear()
        self._set_type_value("newapi")
        self._set_combos("cookie", "api", "auto")
        self._set_oauth_provider("linuxdo")
        self._refresh_oauth_account_choices(accounts_store.DEFAULT_OAUTH_ACCOUNT)
        self._refresh_oauth_fallback_choices()
        self.script_edit.clear()
        self.script_args_edit.setPlainText("{}")
        self.script_timeout_edit.setText("120")
        self.state_edit.clear()
        self.proxy_edit.clear()
        self.verify_ssl_check.setChecked(True)
        self.uid_edit.clear()
        self.token_edit.clear()
        self.cookie_edit.clear()
        self._update_summary(None)
        self._set_actions_enabled(False)
        self._lock = False
        self._sync_type()

    def _update_summary(self, row: dict[str, Any] | None = None) -> None:
        if row is None and self.cur is not None and 0 <= self.cur < len(self.rows):
            row = self.rows[self.cur]
        if not row:
            self.edit_title.setText("未选择站点")
            self.summary_url.setText("从左侧选择一个站点，或点击新增开始配置。")
            self.summary_badge.setText("—")
            self.summary_badge.setStyleSheet("color: #94a3b8; background: #f1f5f9; border-radius: 9px; padding: 3px 9px; font-weight: 700;")
            self.summary_state.setText("未选择")
            self.summary_state.setProperty("state", "idle")
            self.summary_state.style().unpolish(self.summary_state)
            self.summary_state.style().polish(self.summary_state)
            self._render_summary_status(None)
            return
        enabled = bool(row.get("enabled"))
        self.edit_title.setText(row.get("name") or "（未命名）")
        self.summary_url.setText(row.get("base_url") or "尚未填写站点地址")
        info = TYPE_INFO.get(row.get("type"), TYPE_INFO["newapi"])
        self.summary_badge.setText(info["label"])
        self.summary_badge.setStyleSheet(
            f"color: {info['fg']}; background: {info['bg']}; border-radius: 9px; padding: 3px 9px; font-weight: 700;"
        )
        self.summary_state.setText("自动签到已启用" if enabled else "自动签到已关闭")
        self.summary_state.setProperty("state", "on" if enabled else "off")
        self.summary_state.style().unpolish(self.summary_state)
        self.summary_state.style().polish(self.summary_state)
        self._render_summary_status(self._status_cache.get(self._status_key(row)))

    def _render_summary_status(self, status: dict[str, Any] | None) -> None:
        """渲染汇总卡片的额度与签到状态徽标。"""
        has_site = self.cur is not None
        self.btn_refresh.setEnabled(has_site)
        self.btn_checkin_now.setEnabled(has_site)
        checked_in_now = False
        if not status:
            self.quota_value.setText("—")
            self.quota_value.setToolTip("")
            self.checkin_pill.setText("未查询" if has_site else "—")
            self.checkin_pill.setProperty("kind", "unknown")
            self.checkin_pill.setToolTip("")
        else:
            quota = status.get("quota_usd")
            cached = status.get("cached")
            failed = status.get("ok") is False
            message = str(status.get("message") or "")
            if quota is not None:
                fmt = f"${quota:.2f}" if quota >= 0.01 else f"${quota:.4f}"
                self.quota_value.setText(fmt)
                self.quota_value.setToolTip(
                    ("上次签到缓存（点🔄实时刷新）" if cached else "实时查询结果")
                    + (f"\n{message}" if message else "")
                )
            elif failed and status.get("last_quota_usd") is not None:
                # 登录失效等失败：仍展示失效前的最后额度（灰显 + 标注），比清空更有参考价值。
                last = status.get("last_quota_usd")
                fmt = f"${last:.2f}" if last >= 0.01 else f"${last:.4f}"
                self.quota_value.setText(f"{fmt} ⚠")
                self.quota_value.setToolTip(
                    f"这是失效前的最后额度，非当前实时值。\n{_query_failure_toast(str(status.get('status') or 'error'), message)}"
                )
            else:
                self.quota_value.setText("—")
                self.quota_value.setToolTip(message)
            checked_in = status.get("checked_in")
            if checked_in is True:
                self.checkin_pill.setText("🎁 今日已签到")
                self.checkin_pill.setProperty("kind", "done")
                checked_in_now = True
            elif checked_in is False:
                self.checkin_pill.setText("○ 今日待签到")
                self.checkin_pill.setProperty("kind", "todo")
            elif failed:
                self.checkin_pill.setText(_query_failure_label(str(status.get("status") or "error")))
                self.checkin_pill.setProperty("kind", "fail")
            else:
                self.checkin_pill.setText("—")
                self.checkin_pill.setProperty("kind", "unknown")
            # 失效时把原因写进 tooltip，方便悬停查看具体信息（如需重新捕获 OAuth 登录态）。
            self.checkin_pill.setToolTip(
                _query_failure_toast(str(status.get("status") or "error"), message) if failed else message
            )
            if cached and not failed:
                self.checkin_pill.setText(self.checkin_pill.text() + " (缓存)")
        self.checkin_pill.style().unpolish(self.checkin_pill)
        self.checkin_pill.style().polish(self.checkin_pill)
        # 立即签到按钮智能态：已签到 → 次要「重新签到」；否则主色「立即签到」
        if checked_in_now:
            self.btn_checkin_now.setText("重新签到")
            self.btn_checkin_now.setProperty("kind", "tool")
        else:
            self.btn_checkin_now.setText("立即签到")
            self.btn_checkin_now.setProperty("kind", "primary")
        self.btn_checkin_now.style().unpolish(self.btn_checkin_now)
        self.btn_checkin_now.style().polish(self.btn_checkin_now)

    def _refresh_status(self) -> None:
        """实时查询当前站点额度，非阻塞；手动刷新始终发起请求并输出控制台日志。"""
        params = self._browser_params()
        if params is None:
            return
        
        cur_idx = self.cur
        if cur_idx is None:
            return
            
        key = self._status_key(self.rows[cur_idx])
        task_key = self._try_start_site_task(cur_idx, "查询")
        if not task_key:
            return
        
        # 手动点击“实时查询”时总是发起请求，确保能看到最新失败原因并输出控制台日志。
        self._say("正在查询额度…")
        self.quota_value.setText("…")
        self.checkin_pill.setText("查询中")
        self.checkin_pill.setProperty("kind", "unknown")
        self.checkin_pill.style().unpolish(self.checkin_pill)
        self.checkin_pill.style().polish(self.checkin_pill)

        def on_done(name: str, result: dict[str, Any]) -> None:
            try:
                self._on_query_done(cur_idx, key, result)
            finally:
                self._finish_site_task(task_key)

        self._start_task("query", params, on_done)

    def _on_query_done(self, cur_idx: int, key: str, result: dict[str, Any]) -> None:
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("success" if ok else "error"))
        message = result.get("message") or ("查询成功" if ok else "查询失败")
        # 失效时保留上次已知额度作为 last_quota_usd：登录失效不代表账户余额归零，
        # 仍向用户展示「失效前的最后额度」比直接清空更有参考价值。
        prev = self._status_cache.get(key) or {}
        prev_quota = prev.get("quota_usd")
        if prev_quota is None:
            prev_quota = prev.get("last_quota_usd")
        self._status_cache[key] = {
            "quota_usd": result.get("quota_usd") if ok else None,
            "last_quota_usd": result.get("quota_usd") if ok else prev_quota,
            "checked_in": result.get("checked_in") if ok else None,
            "ok": ok,
            "status": status,
            "message": message,
            "detail": result.get("detail"),
            "cached": False,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._refresh_row(cur_idx)
        if cur_idx == self.cur:
            self._update_summary(self.rows[cur_idx])
        self._save_gui_status_cache()
        self._say(message if ok else _query_failure_toast(status, str(message)))

    def _apply_checkin_result(self, idx: int, key: str, result: dict[str, Any]) -> None:
        status = str(result.get("status") or ("success" if result.get("ok") else "error"))
        ok = status in ("success", "already_done") or bool(result.get("ok") and status == "unknown")
        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
        quota_usd = self._detail_quota_usd(detail)
        message = str(result.get("message") or status or "签到完成")
        # 签到失败时保留失效前的历史额度，供渲染层灰显参考（不误导为当前实时值）。
        prev = self._status_cache.get(key) or {}
        last_quota = quota_usd if quota_usd is not None else (
            prev.get("quota_usd") if prev.get("quota_usd") is not None else prev.get("last_quota_usd")
        )
        self._status_cache[key] = {
            "quota_usd": quota_usd,
            "last_quota_usd": last_quota,
            "checked_in": True if ok else None,
            "ok": ok,
            "status": status,
            "message": message,
            "cached": False,
            "saved_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._save_gui_status_cache()
        self._refresh_row(idx)
        if idx == self.cur:
            self._update_summary(self.rows[idx])

    def _checkin_current(self) -> None:
        """签到当前选中站点。"""
        if self.cur is None:
            QMessageBox.information(self, "提示", "请先选择一个站点。")
            return
        
        idx = self.cur
        key = self._status_key(self.rows[idx])
        task_key = self._try_start_site_task(idx, "签到")
        if not task_key:
            return
        
        self._update_checkin_button_state(idx, running=True)
        self.btn_checkin_now.setEnabled(False)
        
        def on_done(name: str, result: dict[str, Any]) -> None:
            try:
                self._apply_checkin_result(idx, key, result)
                status = result.get("status", "error")
                message = result.get("message", "")
                self._say(f"{name}: {status} - {message}" if result.get("ok") else f"{name}: 签到失败 [{status}] {message}")
            finally:
                # 清除站点任务锁
                self._finish_site_task(task_key)
                self._update_checkin_button_state(idx, running=False)
                self.btn_checkin_now.setEnabled(True)
        
        row = self.rows[idx]
        auth_method = row.get("auth_method", "cookie")
        checkin_action = row.get("checkin_action", "api")
        if checkin_action == "relogin":
            auth_method = "oauth"
            row["auth_method"] = "oauth"
            row["browser_state"] = ""
        if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
            auth_method = "oauth"
            row["auth_method"] = "oauth"
        oauth_provider = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
        browser_state = str(row.get("browser_state") or "").strip()
        if auth_method == "oauth":
            browser_state = str((((self.oauth_states.get(oauth_provider) or {}).get("accounts") or {}).get(oauth_account) or {}).get("state") or "").strip()
        params = {
            "name": row["name"],
            "base_url": accounts_store.normalize_base_url(row["base_url"]),
            "site_profile": row.get("type", "newapi"),
            "auth_method": auth_method,
            "checkin_action": checkin_action,
            "script": str(row.get("script") or "").strip(),
            "script_args": accounts_store.normalize_script_args(row.get("script_args")),
            "script_timeout": accounts_store.parse_script_timeout(row.get("script_timeout"), 120),
            "api_variant": row.get("api_variant", "auto"),
            "cookie": row.get("cookie", ""),
            "access_token": row.get("access_token", ""),
            "user_id": row.get("user_id", ""),
            "oauth_provider": oauth_provider,
            "oauth_account": oauth_account,
            "oauth_fallback_provider": row.get("oauth_fallback_provider", ""),
            "oauth_fallback_account": row.get("oauth_fallback_account", ""),
            "browser_state": browser_state,
            "proxy": row.get("proxy", ""),
            "verify_ssl": accounts_store.parse_enabled(row.get("verify_ssl"), True),
        }
        self._start_task("checkin", params, on_done)

    def _checkin_all(self) -> None:
        """签到所有启用站点；每个站点独立计算状态。"""
        enabled = [i for i, r in enumerate(self.rows) if r.get("enabled", True)]
        if not enabled:
            QMessageBox.information(self, "提示", "没有启用的站点。")
            return

        # 过滤掉正在运行任务的站点；同一 base_url 在本批次也只保留第一个
        to_checkin = []
        to_checkin_task_keys: dict[int, str] = {}
        seen_site_keys: set[str] = set()
        skipped_running = 0
        skipped_duplicate = 0
        for idx in enabled:
            task_key = self._site_task_key(self.rows[idx])
            if not task_key:
                continue
            if task_key in self._checkin_running:
                skipped_running += 1
                continue
            if task_key in seen_site_keys:
                skipped_duplicate += 1
                continue
            seen_site_keys.add(task_key)
            to_checkin.append(idx)
            to_checkin_task_keys[idx] = task_key

        if not to_checkin:
            QMessageBox.information(self, "提示", "所有站点都有任务正在运行，或本批次已跳过重复站点。")
            return
        if skipped_running or skipped_duplicate:
            self._say(f"已跳过 {skipped_running} 个运行中的站点任务、{skipped_duplicate} 个同站点重复任务。")

        # 标记为运行中，禁用按钮
        for idx in to_checkin:
            task_key = to_checkin_task_keys[idx]
            self._checkin_running.add(task_key)
            self._update_checkin_button_state(idx, running=True)

        self.btn_checkin_now.setEnabled(False)

        completed = [0]
        results: dict[str, dict[str, Any]] = {}

        def on_done_for(done_idx: int):
            def on_done(name: str, result: dict[str, Any]) -> None:
                try:
                    results[name] = result
                    # 更新该站点的状态缓存（成功/失败都覆盖旧状态）
                    key = self._status_key(self.rows[done_idx])
                    self._apply_checkin_result(done_idx, key, result)
                finally:
                    completed[0] += 1
                    self._finish_site_task(to_checkin_task_keys.get(done_idx, ""))
                    self._update_checkin_button_state(done_idx, running=False)
                    self._say(f"签到进度：{completed[0]}/{len(to_checkin)}")

                    if completed[0] >= len(to_checkin):
                        # 兜底清理，防止异常路径遗留站点任务锁
                        for cleanup_idx in to_checkin:
                            self._finish_site_task(to_checkin_task_keys.get(cleanup_idx, ""))
                            self._update_checkin_button_state(cleanup_idx, running=False)
                        self.btn_checkin_now.setEnabled(True)

                        success = sum(1 for r in results.values() if r.get("ok") or r.get("status") in ("success", "already_done"))
                        failed = len(results) - success
                        msg = f"签到完成：{success}/{len(results)} 成功，{failed} 个失败或需处理\n\n"
                        for n, r in results.items():
                            status = r.get("status", "error")
                            prefix = "OK" if r.get("ok") or status in ("success", "already_done") else "FAIL"
                            msg += f"[{prefix}] {n}: {status} - {r.get('message', '')}\n"
                        QMessageBox.information(self, "所有站点签到完成", msg)
                        self._say(f"签到完成：{success}/{len(results)} 成功，{failed} 个失败或需处理")

            return on_done

        for idx in to_checkin:
            row = self.rows[idx]
            auth_method = row.get("auth_method", "cookie")
            checkin_action = row.get("checkin_action", "api")
            if checkin_action == "relogin":
                auth_method = "oauth"
                row["auth_method"] = "oauth"
                row["browser_state"] = ""
            if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
                auth_method = "oauth"
                row["auth_method"] = "oauth"
            oauth_provider = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
            oauth_account = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
            browser_state = str(row.get("browser_state") or "").strip()
            if auth_method == "oauth":
                browser_state = str((((self.oauth_states.get(oauth_provider) or {}).get("accounts") or {}).get(oauth_account) or {}).get("state") or "").strip()
            params = {
                "name": row["name"],
                "base_url": accounts_store.normalize_base_url(row["base_url"]),
                "site_profile": row.get("type", "newapi"),
                "auth_method": auth_method,
                "checkin_action": checkin_action,
                "script": str(row.get("script") or "").strip(),
                "script_args": accounts_store.normalize_script_args(row.get("script_args")),
                "script_timeout": accounts_store.parse_script_timeout(row.get("script_timeout"), 120),
                "api_variant": row.get("api_variant", "auto"),
                "cookie": row.get("cookie", ""),
                "access_token": row.get("access_token", ""),
                "user_id": row.get("user_id", ""),
                "oauth_provider": oauth_provider,
                "oauth_account": oauth_account,
                "oauth_fallback_provider": row.get("oauth_fallback_provider", ""),
                "oauth_fallback_account": row.get("oauth_fallback_account", ""),
                "browser_state": browser_state,
                "proxy": row.get("proxy", ""),
                "verify_ssl": accounts_store.parse_enabled(row.get("verify_ssl"), True),
            }
            self._start_task("checkin", params, on_done_for(idx))

    def _update_checkin_button_state(self, idx: int, running: bool) -> None:
        """更新签到按钮状态和列表项显示。"""
        if running:
            if idx == self.cur:
                self.checkin_pill.setText("签到中…")
                self.checkin_pill.setProperty("kind", "unknown")
                self.checkin_pill.style().unpolish(self.checkin_pill)
                self.checkin_pill.style().polish(self.checkin_pill)
        else:
            # 签到完成，刷新状态显示
            if idx == self.cur:
                self._update_summary(self.rows[idx])

    def _set_actions_enabled(self, on: bool) -> None:
        self.summary_state.setEnabled(on)
        for btn in (self.btn_dup, self.btn_del):
            btn.setEnabled(on)

    # ── 类型联动 ──
    def _set_type(self, t: str) -> None:
        if self._lock:
            return
        self._set_type_value(t)
        self._sync_type()
        self._flush()

    def _set_type_value(self, t: str) -> None:
        if t not in TYPES:
            t = "newapi"
        for tt, btn in self._type_buttons.items():
            btn.setChecked(tt == t)

    def _current_type(self) -> str:
        for t, btn in self._type_buttons.items():
            if btn.isChecked():
                return t
        return "newapi"

    # ── 三维组合控件读写 ──
    @staticmethod
    def _combo_value(combo: QComboBox, valid: tuple[str, ...], default: str) -> str:
        data = combo.currentData()
        return data if data in valid else default

    def _set_combo_value(self, combo: QComboBox, value: str, default: str) -> None:
        idx = combo.findData(value)
        if idx < 0:
            idx = combo.findData(default)
        combo.blockSignals(True)
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def _current_auth_method(self) -> str:
        return self._combo_value(self.auth_combo, AUTH_METHODS, "cookie")

    def _current_action(self) -> str:
        return self._combo_value(self.action_combo, CHECKIN_ACTIONS, "api")

    def _current_variant(self) -> str:
        return self._combo_value(self.variant_combo, API_VARIANTS, "auto")

    def _current_oauth_provider(self) -> str:
        return self._combo_value(self.oauth_provider_combo, OAUTH_PROVIDERS, "linuxdo")

    def _current_oauth_account(self) -> str:
        text = self.oauth_account_combo.currentText().strip()
        idx = self.oauth_account_combo.currentIndex()
        current_label = self.oauth_account_combo.itemText(idx).strip() if idx >= 0 else ""
        data = self.oauth_account_combo.currentData()
        if data and (not text or text == current_label):
            return accounts_store.normalize_oauth_account(data)
        return accounts_store.normalize_oauth_account(text or data)

    def _set_oauth_provider(self, oauth_provider: str) -> None:
        self._set_combo_value(self.oauth_provider_combo, oauth_provider, "linuxdo")

    def _current_oauth_fallback(self) -> tuple[str, str]:
        """返回可选 OAuth 兜底的 (provider, account)；空元组值表示不使用。"""
        data = str(self.oauth_fallback_combo.currentData() or "")
        if ":" not in data:
            return "", ""
        provider, account = data.split(":", 1)
        provider = accounts_store.normalize_oauth_provider(provider)
        if not provider:
            return "", ""
        return provider, accounts_store.normalize_oauth_account(account)

    def _refresh_oauth_fallback_choices(self, selected_provider: str = "", selected_account: str = "") -> None:
        """用已保存的 OAuth 登录态填充可选下拉框。"""
        selected_provider = accounts_store.normalize_oauth_provider(selected_provider)
        selected_account = accounts_store.normalize_oauth_account(selected_account) if selected_provider else ""
        selected_data = f"{selected_provider}:{selected_account}" if selected_provider else ""
        self.oauth_fallback_combo.blockSignals(True)
        self.oauth_fallback_combo.clear()
        self.oauth_fallback_combo.addItem("不使用", "")
        saved_count = 0
        for provider in OAUTH_PROVIDERS:
            accounts = dict(((self.oauth_states.get(provider) or {}).get("accounts") or {}))
            names = sorted(accounts)
            if accounts_store.DEFAULT_OAUTH_ACCOUNT in names:
                names.remove(accounts_store.DEFAULT_OAUTH_ACCOUNT)
                names.insert(0, accounts_store.DEFAULT_OAUTH_ACCOUNT)
            for account in names:
                entry = accounts.get(account) or {}
                username = str(entry.get("username") or "").strip()
                label = f"{OAUTH_PROVIDER_LABELS.get(provider, provider)} / {account}"
                if username and username != account:
                    label += f" · {username}"
                self.oauth_fallback_combo.addItem(label, f"{provider}:{account}")
                saved_count += 1
        if not saved_count:
            self.oauth_fallback_combo.addItem("暂无共享 OAuth 登录态（请先捕获）", "")
        idx = self.oauth_fallback_combo.findData(selected_data)
        self.oauth_fallback_combo.setCurrentIndex(max(idx, 0))
        self.oauth_fallback_combo.blockSignals(False)

    def _oauth_accounts_for_provider(self, provider: str | None = None) -> dict[str, dict[str, Any]]:
        prov = provider or self._current_oauth_provider()
        return dict(((self.oauth_states.get(prov) or {}).get("accounts") or {}))

    def _oauth_state_entry(self, provider: str | None = None, account: str | None = None) -> dict[str, Any]:
        accounts = self._oauth_accounts_for_provider(provider)
        key = accounts_store.normalize_oauth_account(account or self._current_oauth_account())
        return dict(accounts.get(key) or {})

    def _refresh_oauth_account_choices(self, selected: str | None = None) -> None:
        selected_key = accounts_store.normalize_oauth_account(selected or self._current_oauth_account())
        provider = self._current_oauth_provider()
        accounts = self._oauth_accounts_for_provider(provider)
        names = sorted(accounts.keys())
        if accounts_store.DEFAULT_OAUTH_ACCOUNT in names:
            names.remove(accounts_store.DEFAULT_OAUTH_ACCOUNT)
            names.insert(0, accounts_store.DEFAULT_OAUTH_ACCOUNT)
        if selected_key not in names:
            names.insert(0, selected_key)
        self.oauth_account_combo.blockSignals(True)
        self.oauth_account_combo.clear()
        for name in names:
            entry = accounts.get(name) or {}
            username = str(entry.get("username") or "").strip()
            label = name
            if provider != "linuxdo" and username and username != name:
                label += f" · {username}"
            self.oauth_account_combo.addItem(label, name)
        idx = self.oauth_account_combo.findData(selected_key)
        self.oauth_account_combo.setCurrentIndex(max(idx, 0))
        self.oauth_account_combo.blockSignals(False)

    def _reload_oauth_accounts(self) -> None:
        selected = self._current_oauth_account()
        try:
            self.oauth_states = accounts_store.load_oauth_states()
        except Exception as exc:
            _bg_log("ERROR", "刷新 OAuth 账号列表失败", error=exc)
            QMessageBox.critical(self, "刷新 OAuth 账号失败", mask_secrets(str(exc)))
            return
        provider = self._current_oauth_provider()
        account_count = len(self._oauth_accounts_for_provider(provider))
        fallback_provider, fallback_account = self._current_oauth_fallback()
        _bg_log("INFO", "刷新 OAuth 账号列表", oauth_provider=provider, oauth_account=selected, account_count=account_count)
        self._refresh_oauth_account_choices(selected)
        self._refresh_oauth_fallback_choices(fallback_provider, fallback_account)
        self._sync_type()
        self._flush()
        self._say("已刷新 OAuth 账号列表")

    def _set_combos(self, auth_method: str, checkin_action: str, api_variant: str) -> None:
        if checkin_action == "relogin":
            auth_method = "oauth"
        if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
            auth_method = "oauth"
        self._set_combo_value(self.auth_combo, auth_method, "cookie")
        self._set_combo_value(self.action_combo, checkin_action, "api")
        self._set_combo_value(self.variant_combo, api_variant, "auto")

    def _on_oauth_provider_changed(self, *_args: Any) -> None:
        self._refresh_oauth_account_choices(accounts_store.DEFAULT_OAUTH_ACCOUNT)
        self._on_combo_changed()

    def _on_oauth_account_changed(self, *_args: Any) -> None:
        """下拉选择 OAuth 账号时优先保留 item data，避免 editable combo 读到旧文本。"""
        idx = self.oauth_account_combo.currentIndex()
        line = self.oauth_account_combo.lineEdit()
        if idx >= 0 and line is not None:
            label = self.oauth_account_combo.itemText(idx)
            if line.text() != label:
                line.blockSignals(True)
                line.setText(label)
                line.blockSignals(False)
        self._on_combo_changed()

    def _on_oauth_fallback_changed(self, *_args: Any) -> None:
        self._sync_type()
        self._flush()

    def _on_combo_changed(self, *_args: Any) -> None:
        """登录方式 / 签到方式变化时同步显隐，再回写。"""
        action = self._current_action()
        auth_method = self._current_auth_method()
        if action == "relogin" and auth_method != "oauth":
            self._set_combo_value(self.auth_combo, "oauth", "oauth")
        elif action == "browser_script" and auth_method not in {"browser", "oauth"}:
            self._set_combo_value(self.auth_combo, "oauth", "oauth")
        self._sync_type()
        self._flush()

    def _sync_type(self) -> None:
        t = self._current_type()
        for tt, btn in self._type_buttons.items():
            btn.setProperty("active", tt == t)
            btn.style().unpolish(btn)
            btn.style().polish(btn)
        auth_method = self._current_auth_method()
        action = self._current_action()
        if action == "relogin" and auth_method != "oauth":
            auth_method = "oauth"
            self._set_combo_value(self.auth_combo, "oauth", "oauth")
        if action == "browser_script" and auth_method not in {"browser", "oauth"}:
            auth_method = "oauth"
            self._set_combo_value(self.auth_combo, "oauth", "oauth")
        is_browser = auth_method == "browser"
        is_oauth = auth_method == "oauth"
        # access_token / cookie 登录方式才需要手填凭据
        needs_cred = auth_method in ("access_token", "cookie")
        is_relogin = action == "relogin"
        is_script = action == "browser_script"
        needs_oauth = is_oauth or is_relogin or (is_script and auth_method == "oauth")
        self.variant_wrap.setVisible(t == "newapi" and action == "api")
        self.script_wrap.setVisible(is_script)
        self.script_args_wrap.setVisible(is_script)
        self.script_timeout_wrap.setVisible(is_script)
        self.oauth_provider_wrap.setVisible(needs_oauth)
        self.oauth_account_wrap.setVisible(needs_oauth)
        can_optional_oauth = (
            (t == "sub2api" and action == "api" and auth_method == "access_token")
            or (is_script and auth_method == "browser")
        )
        self.oauth_fallback_wrap.setVisible(can_optional_oauth)
        fallback_provider, fallback_account = self._current_oauth_fallback()
        fallback_enabled = can_optional_oauth and bool(fallback_provider)
        self.uid_edit.setEnabled(needs_cred)
        self.cookie_edit.setEnabled(needs_cred)
        self.token_edit.setEnabled(needs_cred)
        # 可选 OAuth 放在站点登录状态下；不选择时仅显示登录状态，且不会启动浏览器。
        show_state = is_browser or needs_oauth or can_optional_oauth
        self.state_wrap.setVisible(show_state)
        self.state_edit.setVisible(is_browser)
        self.state_edit.setEnabled(is_browser)
        self.oauth_state_status.setVisible(needs_oauth or can_optional_oauth)
        self.browser_ops.setVisible(is_browser or needs_oauth)
        self.btn_oauth_delete.setVisible(needs_oauth)
        if needs_oauth:
            prov = self._current_oauth_provider()
            account = self._current_oauth_account()
            saved = self._oauth_state_entry(prov, account)
            state_len = len(str(saved.get("state") or ""))
            label = f"{OAUTH_PROVIDER_LABELS.get(prov, prov)} / {account}"
            if state_len:
                self.oauth_state_status.setText(f"已保存 {label} 登录态（{state_len} 字符）。")
            else:
                self.oauth_state_status.setText(f"尚未保存 {label} 登录态；请输入账号名后点击“捕获 OAuth 登录态”。")
            self.btn_capture.setText("捕获 OAuth 登录态")
            self.btn_verify.setText("检测 OAuth 登录态")
            if is_script:
                self.mode_hint.setText("💡 自定义脚本会用已保存的 OAuth 登录态启动浏览器，并由脚本控制页面点击。脚本路径请使用仓库内相对路径。")
            else:
                self.mode_hint.setText("💡 OAuth 登录态按“提供商 + 账号”保存，可被多个站点复用；浏览器重登会自动使用 OAuth 登录方式。")
        elif is_browser:
            self.btn_capture.setText("浏览器登录捕获")
            self.btn_verify.setText("检测登录态")
            if is_script and can_optional_oauth:
                if fallback_enabled:
                    saved = self._oauth_state_entry(fallback_provider, fallback_account)
                    state_len = len(str(saved.get("state") or ""))
                    label = f"{OAUTH_PROVIDER_LABELS.get(fallback_provider, fallback_provider)} / {fallback_account}"
                    self.oauth_state_status.setText(
                        f"可选 OAuth：{label}（{state_len} 字符）"
                        if state_len else f"可选 OAuth：{label}（未保存登录态）"
                    )
                else:
                    has_shared_oauth = any(
                        ((self.oauth_states.get(provider) or {}).get("accounts") or {})
                        for provider in OAUTH_PROVIDERS
                    )
                    self.oauth_state_status.setText(
                        "暂无共享 OAuth 登录态；请切换登录方式为“OAuth 登录态（共享账号）”后捕获，或点击“刷新账号”。"
                        if not has_shared_oauth else "可选 OAuth 当前未启用；可从已保存的共享账号中选择。"
                    )
                self.mode_hint.setText("💡 自定义脚本始终先使用当前站点浏览器登录态；失效后最多通过可选 OAuth 自动登录并重试一次。不选择 OAuth 时将直接提示签到失败。可选账号来自顶层共享 OAuth 登录态。")
            else:
                self.oauth_state_status.setText("")
                self.mode_hint.setText("💡 站点浏览器登录态仅用于当前站点，不会作为共享 OAuth 账号使用。")
        elif can_optional_oauth:
            if fallback_enabled:
                saved = self._oauth_state_entry(fallback_provider, fallback_account)
                state_len = len(str(saved.get("state") or ""))
                label = f"{OAUTH_PROVIDER_LABELS.get(fallback_provider, fallback_provider)} / {fallback_account}"
                self.oauth_state_status.setText(f"{label}（{state_len} 字符）" if state_len else f"{label}（未保存登录态）")
            else:
                has_shared_oauth = any(
                    ((self.oauth_states.get(provider) or {}).get("accounts") or {})
                    for provider in OAUTH_PROVIDERS
                )
                self.oauth_state_status.setText(
                    "暂无共享 OAuth 登录态；请切换登录方式为“OAuth 登录态（共享账号）”后捕获，或点击“刷新账号”。"
                    if not has_shared_oauth else ""
                )
            if is_script:
                self.mode_hint.setText("💡 自定义脚本始终先使用当前站点浏览器登录态；失效后最多通过可选 OAuth 自动登录并重试一次。不选择 OAuth 时将直接提示签到失败。可选账号来自顶层共享 OAuth 登录态。")
            else:
                self.mode_hint.setText("")
        else:
            self.oauth_state_status.setText("")
            self.btn_capture.setText("浏览器登录捕获")
            self.btn_verify.setText("检测登录态")
            self.mode_hint.setText("")

    # ── 表单回写 ──
    def _flush(self, *_args: Any) -> None:
        if self._lock or self.cur is None:
            return
        row = self.rows[self.cur]
        row["name"] = self.name_edit.text().strip()
        row["base_url"] = self.base_edit.text().strip()
        row["type"] = self._current_type()
        row["auth_method"] = self._current_auth_method()
        row["checkin_action"] = self._current_action()
        if row["checkin_action"] == "relogin":
            row["auth_method"] = "oauth"
        if row["checkin_action"] == "browser_script" and row["auth_method"] not in {"browser", "oauth"}:
            row["auth_method"] = "oauth"
        row["script"] = self.script_edit.text().strip()
        script_args_text = self.script_args_edit.toPlainText().strip() or "{}"
        row["_script_args_text"] = script_args_text
        try:
            parsed_script_args = json.loads(script_args_text)
            if isinstance(parsed_script_args, dict):
                row["script_args"] = parsed_script_args
        except json.JSONDecodeError:
            pass
        row["script_timeout"] = accounts_store.parse_script_timeout(self.script_timeout_edit.text().strip(), 120)
        row["api_variant"] = self._current_variant()
        row["oauth_provider"] = self._current_oauth_provider()
        row["oauth_account"] = self._current_oauth_account()
        fallback_provider, fallback_account = self._current_oauth_fallback()
        can_optional_oauth = (
            (row["type"] == "sub2api" and row["checkin_action"] == "api" and row["auth_method"] == "access_token")
            or (row["checkin_action"] == "browser_script" and row["auth_method"] == "browser")
        )
        row["oauth_fallback_provider"] = fallback_provider if can_optional_oauth else ""
        row["oauth_fallback_account"] = fallback_account if can_optional_oauth and fallback_provider else ""
        row["user_id"] = self.uid_edit.text().strip()
        row["access_token"] = self.token_edit.text().strip()
        row["cookie"] = self.cookie_edit.toPlainText().strip()
        row["browser_state"] = self.state_edit.toPlainText().strip() if row["auth_method"] == "browser" and row["checkin_action"] != "relogin" else ""
        row["proxy"] = self.proxy_edit.text().strip()
        row["verify_ssl"] = self.verify_ssl_check.isChecked()
        self._update_summary(row)
        self._refresh_row(self.cur)
        self._sync_dirty_state()

    # ── 脏标记 ──
    @staticmethod
    def _normalized_fallback(row: dict[str, Any], *, auth_method: str, checkin_action: str, site_type: str) -> tuple[str, str]:
        """返回当前流程实际会持久化的 OAuth 兜底配置。

        隐藏控件留下的 OAuth fallback 不属于非 browser_script/browser 流程的有效配置；
        在脏状态比较时忽略它们，避免仅切换渠道就显示“未保存”。
        """
        enabled = (
            (site_type == "sub2api" and checkin_action == "api" and auth_method == "access_token")
            or (checkin_action == "browser_script" and auth_method == "browser")
        )
        if not enabled:
            return "", ""
        provider = accounts_store.normalize_oauth_provider(row.get("oauth_fallback_provider"))
        if not provider:
            return "", ""
        return provider, accounts_store.normalize_oauth_account(row.get("oauth_fallback_account"))

    def _rows_snapshot(self) -> list[dict[str, Any]]:
        snapshot: list[dict[str, Any]] = []
        for row in self.rows:
            site_type = row.get("type") if row.get("type") in TYPES else "newapi"
            auth_method = row.get("auth_method") if row.get("auth_method") in AUTH_METHODS else "cookie"
            checkin_action = row.get("checkin_action") if row.get("checkin_action") in CHECKIN_ACTIONS else "api"
            if checkin_action == "relogin":
                auth_method = "oauth"
            if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
                auth_method = "oauth"
            api_variant = row.get("api_variant") if row.get("api_variant") in API_VARIANTS else "auto"
            oauth_provider = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
            oauth_account = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
            fallback_provider, fallback_account = self._normalized_fallback(
                row,
                auth_method=auth_method,
                checkin_action=checkin_action,
                site_type=site_type,
            )
            snapshot.append(
                {
                    "name": str(row.get("name") or "").strip(),
                    "base_url": accounts_store.normalize_base_url(str(row.get("base_url") or "")),
                    "type": site_type,
                    "auth_method": auth_method,
                    "checkin_action": checkin_action,
                    "script": str(row.get("script") or "").strip(),
                    "script_args_text": str(row.get("_script_args_text") if row.get("_script_args_text") is not None else json.dumps(accounts_store.normalize_script_args(row.get("script_args")), ensure_ascii=False, sort_keys=True)),
                    "script_timeout": accounts_store.parse_script_timeout(row.get("script_timeout"), 120),
                    "api_variant": api_variant,
                    "oauth_provider": oauth_provider,
                    "oauth_account": oauth_account,
                    "oauth_fallback_provider": fallback_provider,
                    "oauth_fallback_account": fallback_account,
                    "enabled": bool(row.get("enabled", True)),
                    "user_id": str(row.get("user_id") or "").strip(),
                    "access_token": str(row.get("access_token") or "").strip(),
                    "cookie": str(row.get("cookie") or "").strip(),
                    "browser_state": str(row.get("browser_state") or "").strip() if auth_method == "browser" and checkin_action != "relogin" else "",
                    "proxy": str(row.get("proxy") or "").strip(),
                    "verify_ssl": accounts_store.parse_enabled(row.get("verify_ssl"), True),
                }
            )
        return snapshot

    def _config_snapshot(self) -> dict[str, Any]:
        """生成完整内存配置快照，包含站点与共享 OAuth 登录态。"""
        return {
            "accounts": self._rows_snapshot(),
            "oauth_states": accounts_store.normalize_oauth_states(copy.deepcopy(self.oauth_states)),
        }

    def _set_dirty(self, dirty: bool) -> None:
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self.status.setText("● 未保存" if dirty else "● 已保存")
        self.status.setProperty("state", "dirty" if dirty else "saved")
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    def _sync_dirty_state(self) -> None:
        self._set_dirty(self._config_snapshot() != self._saved_snapshot)

    def _mark_saved(self) -> None:
        self._saved_snapshot = self._config_snapshot()
        self._set_dirty(False)

    def _shutdown_workers(self) -> None:
        """关闭前尽力停止后台任务，避免残留线程 / Playwright 子进程阻塞进程退出。

        - 请求浏览器 worker 结束（capture 模式据此跳出等待循环，让 Playwright 收尾）；
        - 清空线程池中尚未开始的排队任务（已在飞的网络任务无法安全中断，交由
          main() 的强制退出兜底）；
        - 停止定时器。
        """
        worker = getattr(self, "_worker", None)
        if worker is not None:
            try:
                worker.request_close()
            except Exception:
                pass
            try:
                if worker.isRunning():
                    worker.wait(3000)  # 最多等 3s 让浏览器优雅关闭
            except Exception:
                pass
        try:
            self._thread_pool.clear()
        except Exception:
            pass
        try:
            self._toast_timer.stop()
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # 检查是否有正在运行的站点任务
        if self._checkin_running:
            ret = QMessageBox.warning(
                self,
                "任务进行中",
                f"有 {len(self._checkin_running)} 个站点任务正在运行，强制退出可能导致任务失败。\n\n确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret == QMessageBox.No:
                event.ignore()
                return
        
        if self._dirty:
            ret = QMessageBox.question(
                self,
                "未保存",
                "有未保存的更改，确定退出？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return

        # 确认退出：先停止后台任务，再接受关闭事件
        self._shutdown_workers()
        event.accept()

    # ── CRUD ──
    def _add(self) -> None:
        t = self._ask_type()
        if not t:
            return
        if self.cur is not None:
            self._flush()
        self.rows.append(
            {
                "name": "新站点",
                "base_url": "",
                "type": t,
                "auth_method": "cookie",
                "checkin_action": "api",
                "script": "",
                "script_args": {},
                "_script_args_text": "{}",
                "script_timeout": 120,
                "api_variant": "auto",
                "oauth_provider": "linuxdo",
                "oauth_account": accounts_store.DEFAULT_OAUTH_ACCOUNT,
                "oauth_fallback_provider": "",
                "oauth_fallback_account": "",
                "enabled": True,
                "user_id": "",
                "access_token": "",
                "cookie": "",
                "browser_state": "",
                "proxy": "",
                "verify_ssl": True,
            }
        )
        self.cur = len(self.rows) - 1
        self.search_edit.clear()
        self._render_list()
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self._select_real(self.cur)
        self._sync_dirty_state()

    def _ask_type(self) -> str | None:
        dlg = TypeDialog(self)
        dlg.setStyleSheet(APP_STYLE)
        return dlg.chosen if dlg.exec() == QDialog.Accepted else None

    def _del(self) -> None:
        if self.cur is None:
            return
        name = self.rows[self.cur]["name"]
        ret = QMessageBox.question(
            self,
            "确认删除",
            f"删除「{name}」？（同时移除其凭据，保存后生效）",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        old = self.cur
        del self.rows[self.cur]
        self.cur = None
        self._render_list()
        if self.rows:
            new_idx = min(old, len(self.rows) - 1)
            pos = self._visible_pos(new_idx)
            if pos >= 0:
                self.listw.setCurrentRow(pos)
            self._select_real(new_idx)
        else:
            self._clear()
        self._sync_dirty_state()

    def _dup(self) -> None:
        if self.cur is None:
            return
        self._flush()
        nw = copy.deepcopy(self.rows[self.cur])
        nw["name"] = nw["name"] + "-副本"
        self.rows.insert(self.cur + 1, nw)
        self.cur += 1
        self.search_edit.clear()
        self._render_list()
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self._select_real(self.cur)
        self._sync_dirty_state()

    # ── 剪贴板 / 复制 ──
    def _imp(self) -> None:
        txt = QApplication.clipboard().text()
        if not txt.strip():
            QMessageBox.warning(self, "提示", "剪贴板为空。")
            return
        try:
            data = json.loads(txt)
        except json.JSONDecodeError:
            QMessageBox.critical(self, "错误", "剪贴板内容不是合法 JSON。")
            return
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            QMessageBox.critical(self, "错误", "JSON 结构无法识别。")
            return
        if "name" not in data and "base_url" not in data and len(data) == 1:
            k, v = next(iter(data.items()))
            if isinstance(v, dict):
                v.setdefault("name", k)
                data = v
        if self.cur is None:
            self._add()
        if self.cur is None:
            return

        self._lock = True
        if data.get("name"):
            self.name_edit.setText(str(data["name"]))
        if data.get("base_url"):
            self.base_edit.setText(str(data["base_url"]))
        t = str(data.get("type", "")).strip().lower()
        if t in TYPES:
            self._set_type_value(t)
        if data.get("access_token"):
            self.token_edit.setText(str(data["access_token"]))
        if data.get("user_id"):
            self.uid_edit.setText(str(data["user_id"]))
        if data.get("cookie"):
            self.cookie_edit.setPlainText(str(data["cookie"]))
        if "verify_ssl" in data:
            self.verify_ssl_check.setChecked(accounts_store.parse_enabled(data.get("verify_ssl"), True))
        self._lock = False
        self._sync_type()
        self._flush()
        self._say(f"已从剪贴板导入「{data.get('name', '?')}」")

    def _cpcred(self) -> None:
        if self.cur is None:
            return
        row = self.rows[self.cur]
        cred = {k: row[k] for k in CRED if row.get(k)}
        if not cred:
            QMessageBox.warning(self, "提示", "当前没有填写凭据。")
            return
        QApplication.clipboard().setText(json.dumps(cred, ensure_ascii=False, indent=2))
        self._say(f"已复制「{row['name']}」的凭据 JSON")

    # ── 保存 / 导出 ──
    def _validate(self) -> bool:
        for row in self.rows:
            if not row["name"]:
                QMessageBox.critical(self, "校验失败", "存在空的站点名称。")
                return False
            if not row["base_url"]:
                QMessageBox.critical(self, "校验失败", f"「{row['name']}」缺少站点地址。")
                return False
        names = [row["name"] for row in self.rows]
        if len(names) != len(set(names)):
            QMessageBox.critical(self, "校验失败", "站点名称重复，请改为唯一名称。")
            return False
        return True

    def _validate_export(self) -> bool:
        enabled_rows = [row for row in self.rows if accounts_store.parse_enabled(row.get("enabled"), True)]
        if not enabled_rows:
            QMessageBox.warning(self, "无可导出站点", "没有启用的站点可导出到 GitHub Secret。")
            return False
        for row in enabled_rows:
            if not str(row.get("name") or "").strip():
                QMessageBox.critical(self, "导出校验失败", "启用站点中存在空的站点名称。")
                return False
            if not str(row.get("base_url") or "").strip():
                QMessageBox.critical(self, "导出校验失败", f"「{row.get('name') or '未命名站点'}」缺少站点地址。")
                return False
        names = [str(row.get("name") or "").strip() for row in enabled_rows]
        if len(names) != len(set(names)):
            QMessageBox.critical(self, "导出校验失败", "启用站点名称重复，请改为唯一名称或禁用重复项。")
            return False
        return True

    def _save(self) -> None:
        if self.cur is not None:
            self._flush()
        if not self._validate():
            return
        accts: list[dict[str, Any]] = []
        for row in self.rows:
            t = row["type"] if row["type"] in TYPES else "newapi"
            auth_method = row.get("auth_method") if row.get("auth_method") in AUTH_METHODS else "cookie"
            checkin_action = row.get("checkin_action") if row.get("checkin_action") in CHECKIN_ACTIONS else "api"
            if checkin_action == "relogin":
                auth_method = "oauth"
                row["auth_method"] = "oauth"
                row["browser_state"] = ""
            if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
                auth_method = "oauth"
                row["auth_method"] = "oauth"
            script_args: dict[str, Any] = {}
            if checkin_action == "browser_script":
                script_args_text = str(row.get("_script_args_text") if row.get("_script_args_text") is not None else json.dumps(accounts_store.normalize_script_args(row.get("script_args")), ensure_ascii=False)).strip() or "{}"
                try:
                    parsed_script_args = json.loads(script_args_text)
                except json.JSONDecodeError as exc:
                    QMessageBox.critical(self, "脚本参数错误", f"「{row.get('name') or '未命名站点'}」的脚本参数不是合法 JSON：{exc}")
                    return
                if not isinstance(parsed_script_args, dict):
                    QMessageBox.critical(self, "脚本参数错误", f"「{row.get('name') or '未命名站点'}」的脚本参数必须是 JSON 对象。")
                    return
                script_args = parsed_script_args
                if not str(row.get("script") or "").strip():
                    QMessageBox.critical(self, "脚本配置缺失", f"「{row.get('name') or '未命名站点'}」选择了自定义浏览器脚本，但未填写脚本路径。")
                    return
            acct = {
                "name": row["name"],
                "base_url": accounts_store.normalize_base_url(row["base_url"]),
                "site_profile": t,
                "auth_method": auth_method,
                "checkin_action": checkin_action,
                "enabled": bool(row["enabled"]),
                "user_id": row["user_id"],
                "access_token": row["access_token"],
                "cookie": row["cookie"],
            }
            if checkin_action == "browser_script":
                acct["script"] = str(row.get("script") or "").strip()
                acct["script_args"] = script_args
                acct["script_timeout"] = accounts_store.parse_script_timeout(row.get("script_timeout"), 120)
            if auth_method == "oauth" or checkin_action == "relogin":
                acct["oauth_provider"] = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
                acct["oauth_account"] = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
            fallback_provider, fallback_account = self._normalized_fallback(
                row,
                auth_method=auth_method,
                checkin_action=checkin_action,
                site_type=t,
            )
            row["oauth_fallback_provider"] = fallback_provider
            row["oauth_fallback_account"] = fallback_account
            if fallback_provider:
                acct["oauth_fallback_provider"] = fallback_provider
                acct["oauth_fallback_account"] = fallback_account
            # 接口变体仅 newapi + 接口签到 时有意义
            if t == "newapi" and checkin_action == "api":
                variant = row.get("api_variant") if row.get("api_variant") in API_VARIANTS else "auto"
                acct["api_variant"] = variant
            # 代理（所有类型可选）
            if str(row.get("proxy") or "").strip():
                acct["proxy"] = str(row["proxy"]).strip()
            # TLS 校验默认开启；仅显式关闭时落盘，便于证书过期/链异常站点临时兜底。
            if not accounts_store.parse_enabled(row.get("verify_ssl"), True):
                acct["verify_ssl"] = False
            # 站点浏览器登录态仅 auth_method=browser 时保存；OAuth 登录态统一存在顶层 oauth_states
            state_text = str(row.get("browser_state") or "").strip()
            if state_text and auth_method == "browser" and checkin_action != "relogin":
                acct["browser_state"] = state_text
            accts.append(acct)
        try:
            accounts_store.save_accounts(accts, oauth_states=self.oauth_states)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._mark_saved()
        self._say(f"已保存：{len(accts)} 个账号配置")

    # ── 浏览器 / OAuth 登录态操作 ──
    def _browser_params(self) -> dict[str, Any] | None:
        """收集当前站点的浏览器操作参数；无有效站点返回 None。"""
        if self.cur is None:
            QMessageBox.warning(self, "提示", "请先选择一个站点。")
            return None
        self._flush()
        row = self.rows[self.cur]
        base_url = accounts_store.normalize_base_url(str(row.get("base_url") or ""))
        if not base_url:
            QMessageBox.warning(self, "提示", "请先填写站点地址。")
            return None
        row_type = str(row.get("type") or "newapi").strip()
        auth_method = str(row.get("auth_method") or "").strip() or "cookie"
        checkin_action = str(row.get("checkin_action") or "").strip() or "api"
        if checkin_action == "relogin":
            auth_method = "oauth"
            row["auth_method"] = "oauth"
            row["browser_state"] = ""
        if checkin_action == "browser_script" and auth_method not in {"browser", "oauth"}:
            auth_method = "oauth"
            row["auth_method"] = "oauth"
        oauth_provider = accounts_store.normalize_oauth_provider(row.get("oauth_provider")) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(row.get("oauth_account") or row.get("oauth_account_id"))
        browser_state = str(row.get("browser_state") or "").strip()
        if auth_method == "oauth":
            browser_state = str((((self.oauth_states.get(oauth_provider) or {}).get("accounts") or {}).get(oauth_account) or {}).get("state") or "").strip()
        return {
            "base_url": base_url,
            "name": str(row.get("name") or "").strip(),
            "site_profile": row_type,
            "auth_method": auth_method,
            "checkin_action": checkin_action,
            "script": str(row.get("script") or "").strip(),
            "script_args": accounts_store.normalize_script_args(row.get("script_args")),
            "script_timeout": accounts_store.parse_script_timeout(row.get("script_timeout"), 120),
            "api_variant": str(row.get("api_variant") or "auto").strip(),
            "cookie": str(row.get("cookie") or "").strip(),
            "access_token": str(row.get("access_token") or "").strip(),
            "user_id": str(row.get("user_id") or "").strip(),
            "browser_state": browser_state,
            "oauth_provider": oauth_provider,
            "oauth_account": oauth_account,
            "oauth_fallback_provider": str(row.get("oauth_fallback_provider") or ""),
            "oauth_fallback_account": str(row.get("oauth_fallback_account") or ""),
            "login_selector": str(row.get("login_selector") or "").strip(),
            "proxy": str(row.get("proxy") or "").strip(),
            "verify_ssl": accounts_store.parse_enabled(row.get("verify_ssl"), True),
            "fallback_uid": str(row.get("user_id") or "").strip(),
        }

    def _browser_busy(self) -> bool:
        return getattr(self, "_worker", None) is not None and self._worker.isRunning()

    def _set_browser_buttons(self, enabled: bool) -> None:
        # 浏览器专用按钮（捕获/检测）+ 通用测试签到 + 刷新/立即签到，操作期间统一禁用防重入
        for btn in (self.btn_capture, self.btn_verify, self.btn_test, self.btn_refresh, self.btn_checkin_now):
            btn.setEnabled(enabled)

    def _start_worker(self, action: str, params: dict[str, Any]) -> BrowserWorker:
        self._set_browser_buttons(False)
        worker = BrowserWorker(action, params, self)
        self._worker = worker
        worker.progress.connect(lambda msg: self._say(msg))
        worker.failed.connect(self._on_browser_failed)
        worker.finished.connect(lambda: self._set_browser_buttons(True))
        return worker

    def _on_browser_failed(self, msg: str) -> None:
        self._set_browser_buttons(True)
        action = getattr(getattr(self, "_worker", None), "action", "")
        if action == "capture":
            dlg = self._capture_dialog
            if dlg is not None and dlg.isVisible():
                dlg.done(0)
        title = {
            "query": "查询失败",
            "site_checkin": "签到失败",
            "capture": "登录态捕获失败",
            "verify": "登录态检测失败",
            "checkin": "OAuth 重登失败",
        }.get(action, "后台操作失败")
        QMessageBox.critical(self, title, f"{msg}\n\n详细日志请查看启动该 GUI 的控制台输出。")
        self._say(f"{title}：{msg}")

    def _browser_capture(self) -> None:
        if self._browser_busy():
            QMessageBox.information(self, "请稍候", "已有浏览器操作进行中。")
            return
        params = self._browser_params()
        if params is None:
            return
        cur_idx = self.cur
        if cur_idx is None:
            return
        task_key = self._try_start_site_task(cur_idx, "浏览器操作")
        if not task_key:
            return
        is_oauth = params.get("auth_method") == "oauth"
        provider_label = OAUTH_PROVIDER_LABELS.get(params.get("oauth_provider"), params.get("oauth_provider", ""))
        account = params.get("oauth_account", accounts_store.DEFAULT_OAUTH_ACCOUNT)
        self._say("正在打开浏览器，请在其中完成第三方登录…" if is_oauth else "正在打开浏览器，请完成站点登录…")
        worker = self._start_worker("capture", params)
        worker.finished.connect(lambda: self._finish_site_task(task_key))

        # 非模态对话框：用户在浏览器里登录完后点「我已完成登录」，通知 worker 收尾
        dlg = QMessageBox(self)
        dlg.setWindowTitle("OAuth 登录态捕获" if is_oauth else "浏览器登录捕获")
        if is_oauth:
            dlg.setText(
                f"已打开浏览器窗口。\n\n请在其中登录 {provider_label}（账号：{account}）。"
                "检测到有效登录态后窗口会自动关闭；若需提前检查，可点击下方按钮。"
            )
        else:
            dlg.setText(
                "已打开浏览器窗口。\n\n请在其中完成登录并回到站点控制台，"
                "然后点下方「我已完成登录」。"
            )
        done_btn = dlg.addButton("手动检查登录态" if is_oauth else "我已完成登录", QMessageBox.AcceptRole)
        dlg.setStandardButtons(QMessageBox.NoButton)
        dlg.setIcon(QMessageBox.Information)
        self._capture_dialog = dlg

        def _on_capture_done(result: dict[str, Any]) -> None:
            if dlg.isVisible():
                dlg.done(0)
            if result.get("ok") and result.get("state"):
                if params.get("auth_method") == "oauth":
                    provider = result.get("provider") or params.get("oauth_provider") or "linuxdo"
                    account = accounts_store.normalize_oauth_account(params.get("oauth_account"))
                    try:
                        provider = accounts_store.normalize_oauth_provider(provider)
                        if not provider:
                            raise ValueError("未知 OAuth 提供商")
                        provider_bucket = self.oauth_states.setdefault(provider, {"accounts": {}})
                        provider_accounts = provider_bucket.setdefault("accounts", {})
                        provider_accounts[account] = {
                            "state": str(result["state"] or "").strip(),
                            "username": str(result.get("username") or ""),
                            "updated_at": datetime.now().isoformat(timespec="seconds"),
                        }
                        self.oauth_states = accounts_store.normalize_oauth_states(self.oauth_states)
                    except Exception as exc:
                        _bg_log("ERROR", "暂存 OAuth 登录态失败", oauth_provider=provider, oauth_account=account, error=exc)
                        QMessageBox.critical(self, "暂存 OAuth 登录态失败", mask_secrets(str(exc)))
                        return
                    _bg_log("INFO", "暂存 OAuth 登录态", oauth_provider=provider, oauth_account=account, username=result.get("username", ""), state_chars=len(str(result.get("state") or "")))
                    self._refresh_oauth_account_choices(account)
                    fallback_provider, fallback_account = self._current_oauth_fallback()
                    self._refresh_oauth_fallback_choices(fallback_provider, fallback_account)
                    self._sync_type()
                    self._sync_dirty_state()
                    QMessageBox.information(
                        self, "捕获成功",
                        f"{result.get('message', 'OAuth 登录态已捕获。')}\n\n登录态已加入当前内存配置，请点击“保存全部”写入文件。",
                    )
                    self._say(f"已暂存 {provider}:{account} 登录态，请点“保存全部”")
                else:
                    if self.cur is not None:
                        self._lock = True
                        self.state_edit.setPlainText(result["state"])
                        if result.get("access_token"):
                            self.token_edit.setText(str(result["access_token"]))
                        self._lock = False
                        self._flush()
                    QMessageBox.information(
                        self, "捕获成功",
                        result.get("message", "登录态已捕获并填入「浏览器登录态」，记得点「保存全部」。"),
                    )
                    self._say("登录态已填入，请点「保存全部」")
            else:
                QMessageBox.warning(self, "未捕获到有效登录态", result.get("message", "请重试。"))

        worker.finished_ok.connect(_on_capture_done)
        worker.start()

        dlg.exec()
        # 无论自动检测完成、用户手动检查，还是用 Esc / 窗口 X 关闭对话框，都通知 worker 收尾，
        # 否则 capture 的等待循环会空转最长 600s，期间浏览器按钮禁用、站点任务锁不释放。
        if dlg.clickedButton() is done_btn:
            self._say("正在读取并打包登录态…")
        else:
            self._say("已关闭登录窗口，正在收尾…")
        worker.request_close()
        if self._capture_dialog is dlg:
            self._capture_dialog = None

    def _delete_oauth_account(self) -> None:
        provider = self._current_oauth_provider()
        account = self._current_oauth_account()
        entry = self._oauth_state_entry(provider, account)
        if not entry.get("state"):
            QMessageBox.information(self, "提示", f"{provider}:{account} 尚未保存登录态。")
            return
        ret = QMessageBox.question(
            self,
            "删除 OAuth 登录态",
            f"从当前配置删除 {provider}:{account} 的 OAuth 登录态？\n\n"
            "站点配置会保留账号名；点击“保存全部”后才会写入文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        provider_accounts = ((self.oauth_states.get(provider) or {}).get("accounts") or {})
        provider_accounts.pop(account, None)
        if provider_accounts:
            self.oauth_states[provider] = {"accounts": provider_accounts}
        else:
            self.oauth_states.pop(provider, None)
        self.oauth_states = accounts_store.normalize_oauth_states(self.oauth_states)
        _bg_log("INFO", "暂存删除 OAuth 登录态", oauth_provider=provider, oauth_account=account)
        fallback_provider, fallback_account = self._current_oauth_fallback()
        self._refresh_oauth_account_choices(account)
        self._refresh_oauth_fallback_choices(fallback_provider, fallback_account)
        self._sync_type()
        self._sync_dirty_state()
        self._say(f"已从当前配置删除 {provider}:{account} 登录态，请点“保存全部”")

    def _browser_verify(self) -> None:
        if self._browser_busy():
            QMessageBox.information(self, "请稍候", "已有浏览器操作进行中。")
            return
        params = self._browser_params()
        if params is None:
            return
        cur_idx = self.cur
        if cur_idx is None:
            return
        task_key = self._try_start_site_task(cur_idx, "检测")
        if not task_key:
            return
        try:
            if params.get("auth_method") == "oauth":
                provider = params.get("oauth_provider", "linuxdo")
                account = params.get("oauth_account", accounts_store.DEFAULT_OAUTH_ACCOUNT)
                state_text = params.get("browser_state", "")
                if not state_text:
                    _bg_log("WARN", "OAuth 登录态缺失", oauth_provider=provider, oauth_account=account)
                    QMessageBox.warning(self, "OAuth 登录态缺失", f"尚未保存 {provider}:{account} 登录态，请先捕获。")
                    self._say(f"缺少 {provider}:{account} 登录态")
                    return
                guessed = accounts_store.guess_oauth_provider(state_text)
                if guessed and guessed != provider:
                    _bg_log("WARN", "OAuth 登录态不匹配", oauth_provider=provider, oauth_account=account, guessed_provider=guessed)
                    QMessageBox.warning(self, "OAuth 登录态不匹配", f"当前选择 {provider}，但登录态看起来属于 {guessed}。")
                    return
                if params.get("site_profile") != "sub2api":
                    _bg_log("INFO", "OAuth 登录态存在", oauth_provider=provider, oauth_account=account, state_chars=len(state_text))
                    QMessageBox.information(self, "OAuth 登录态存在", f"已保存 {provider}:{account} 登录态（{len(state_text)} 字符）。")
                    self._say(f"{provider}:{account} 登录态存在")
                    return
            if not params["browser_state"]:
                # 无 state 时依赖本地 profile，verify 仍可尝试
                self._say("未填登录态，将尝试用本地浏览器登录态检测…")
            worker = self._start_worker("verify", params)
            worker.finished.connect(lambda locked_key=task_key: self._finish_site_task(locked_key))
            worker.finished_ok.connect(
                lambda r: (
                    QMessageBox.information(self, "登录态有效", r.get("message", ""))
                    if r.get("ok") else
                    QMessageBox.warning(self, "登录态无效", r.get("message", "请重新捕获登录态。"))
                )
            )
            worker.start()
            task_key = ""
        finally:
            self._finish_site_task(task_key)

    def _test_checkin(self) -> None:
        """测试签到：走统一入口 providers.run_checkin，适用于所有站点类型。"""
        if self._browser_busy():
            QMessageBox.information(self, "请稍候", "已有签到/浏览器操作进行中。")
            return
        params = self._browser_params()
        if params is None:
            return
        cur_idx = self.cur
        if cur_idx is None:
            return
        task_key = self._try_start_site_task(cur_idx, "测试签到")
        if not task_key:
            return
        self._say(f"正在测试签到（{params['site_profile']} / {params['auth_method']} / {params['checkin_action']}）…")
        worker = self._start_worker("site_checkin", params)
        worker.finished.connect(lambda: self._finish_site_task(task_key))
        key = self._status_key(self.rows[cur_idx])

        def _on_test_done(r: dict[str, Any]) -> None:
            status = r.get("status", "error")
            msg = r.get("message", "") or status
            if key and cur_idx is not None:
                self._apply_checkin_result(cur_idx, key, r)
            if status in ("success", "already_done"):
                QMessageBox.information(self, "测试签到完成", f"[{status}] {msg}")
                self._say(msg)
            elif status == "need_verification":
                QMessageBox.warning(self, "需人机验证", msg)
                self._say(f"测试签到需验证：{msg}")
            elif status == "need_login":
                QMessageBox.warning(self, "需要登录", msg)
                self._say(f"测试签到需要登录：{msg}")
            else:
                QMessageBox.warning(self, "签到未完成", f"[{status}] {msg}")
                self._say(f"测试签到失败 [{status}] {msg}")

        worker.finished_ok.connect(_on_test_done)
        worker.start()

    def _export(self) -> None:
        if self.cur is not None:
            self._flush()
        if not self._validate_export():
            return

        payload = accounts_store.build_github_secret_payload(self.rows, self.oauth_states)
        exported_accounts = payload.get("accounts") if isinstance(payload.get("accounts"), list) else []
        if not exported_accounts:
            QMessageBox.warning(self, "无可导出站点", "没有启用的有效站点可导出到 GitHub Secret。")
            return

        text = json.dumps(payload, ensure_ascii=False, indent=2)
        QApplication.clipboard().setText(text)

        disabled_count = sum(1 for row in self.rows if not accounts_store.parse_enabled(row.get("enabled"), True))
        oauth_count = 0
        oauth_states = payload.get("oauth_states") if isinstance(payload.get("oauth_states"), dict) else {}
        for data in oauth_states.values():
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                oauth_count += len(data["accounts"])
        QMessageBox.information(
            self,
            "已复制",
            "已复制最小化 GitHub Secret：ACCOUNTS。\n\n"
            f"启用站点：{len(exported_accounts)} 个\n"
            f"已剔除禁用站点：{disabled_count} 个\n"
            f"保留 OAuth 登录态：{oauth_count} 个",
        )
        self._say(f"已复制 Secret：{len(exported_accounts)} 个启用站点，{oauth_count} 个 OAuth 登录态")

    def _say(self, text: str) -> None:
        self.toast.setText(text)
        self._toast_timer.start(4000)


APP_STYLE = f"""
* {{
    font-family: "{F}", "Microsoft YaHei UI", sans-serif;
    color: {C['text']};
}}
QWidget#appRoot {{
    background: {C['bg']};
}}
QFrame#topbar, QFrame#footer {{
    background: {C['surface']};
    border: 0;
}}
QFrame#topbar {{
    border-bottom: 1px solid {C['border']};
}}
QFrame#footer {{
    border-top: 1px solid {C['border']};
}}
QLabel#mark {{
    background: {C['accent']};
    color: white;
    border-radius: 10px;
    font-size: 17px;
    font-weight: 800;
}}
QLabel#appTitle {{
    font-size: 18px;
    font-weight: 800;
}}
QLabel#saveStatus {{
    border-radius: 13px;
    padding: 5px 12px;
    font-weight: 700;
}}
QLabel#saveStatus[state="saved"] {{
    color: {C['ok']};
    background: #ecfdf5;
}}
QLabel#saveStatus[state="dirty"] {{
    color: {C['warn']};
    background: #fffbeb;
}}
QFrame#sidebar, QFrame#card, QFrame#summaryCard {{
    background: {C['surface']};
    border: 1px solid {C['border']};
    border-radius: 18px;
}}
QLabel#sectionTitle, QLabel#editTitle {{
    font-size: 17px;
    font-weight: 800;
}}
QLabel#countBadge {{
    color: {C['mute']};
    background: {C['surface_alt']};
    border-radius: 9px;
    padding: 2px 8px;
}}
QLabel#sidebarHint {{
    color: {C['mute']};
    font-size: 12px;
    padding: 0 2px 2px 2px;
}}
QListWidget#siteList {{
    background: transparent;
    border: 0;
    outline: 0;
}}
QListWidget#siteList[dragging="true"] {{
    background: #f8fafc;
    border-radius: 14px;
}}
QListWidget#siteList::item {{
    border: 0;
    padding: 0;
    margin: 0;
}}
QListWidget#siteList::item:hover {{
    background: #f8fafc;
}}
QListWidget#siteList::item:selected {{
    background: transparent;
}}
QWidget#siteItem {{
    background: transparent;
    border-radius: 14px;
    border-left: 3px solid transparent;
}}
QWidget#siteItem[selected="true"] {{
    background: {C['accent_soft']};
    border-left: 3px solid {C['accent']};
}}
QWidget#siteItem[enabledState="off"] {{
    background: #f8fafc;
}}
QWidget#siteItem[enabledState="off"] QLabel#siteName {{
    color: {C['soft']};
}}
QLabel#dragHandle {{
    color: #cbd5e1;
    font-size: 15px;
    font-weight: 800;
}}
QLabel#siteName {{
    font-size: 14px;
    font-weight: 750;
}}
QLabel#siteUrl, QLabel#summaryUrl, QLabel#hintText, QLabel#footerHint, QLabel#toast {{
    color: {C['mute']};
}}
QLabel#summaryUrl {{
    font-size: 12px;
}}
QLabel#siteUrl {{
    font-size: 12px;
}}
QLabel#summaryState {{
    border-radius: 11px;
    padding: 6px 11px;
    font-weight: 800;
}}
QLabel#summaryState[state="on"] {{
    color: #166534;
    background: #dcfce7;
}}
QLabel#summaryState[state="off"] {{
    color: {C['soft']};
    background: #f1f5f9;
}}
QLabel#summaryState[state="idle"] {{
    color: {C['mute']};
    background: #f8fafc;
}}
QFrame#quotaBox {{
    background: {C['accent_soft']};
    border: 1px solid {C['border']};
    border-radius: 12px;
}}
QLabel#quotaCaption {{
    color: {C['mute']};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#quotaValue {{
    color: {C['accent']};
}}
QLabel#quotaMini {{
    color: {C['soft']};
    font-size: 11px;
    font-weight: 700;
}}
QLabel#statusPill, QLabel#statusPillLg {{
    border-radius: 9px;
    font-weight: 700;
}}
QLabel#statusPill {{
    padding: 2px 8px;
    font-size: 11px;
}}
QLabel#statusPillLg {{
    padding: 6px 14px;
    font-size: 13px;
}}
QLabel#statusPill[kind="done"], QLabel#statusPillLg[kind="done"] {{
    color: #166534;
    background: #dcfce7;
}}
QLabel#statusPill[kind="todo"], QLabel#statusPillLg[kind="todo"] {{
    color: {C['warn']};
    background: #fffbeb;
}}
QLabel#statusPill[kind="fail"], QLabel#statusPillLg[kind="fail"] {{
    color: {C['danger']};
    background: {C['danger_soft']};
}}
QLabel#statusPill[kind="unknown"], QLabel#statusPillLg[kind="unknown"] {{
    color: {C['mute']};
    background: #f1f5f9;
}}
QPushButton#iconButton {{
    background: white;
    border: 1px solid {C['border_mid']};
    border-radius: 15px;
    font-size: 14px;
    padding: 0;
}}
QPushButton#iconButton:hover {{
    background: {C['accent_soft']};
    border-color: {C['accent']};
}}
QPushButton#iconButton:disabled {{
    color: {C['mute']};
    background: {C['surface_alt']};
}}
QLabel#cardTitle {{
    font-size: 15px;
    font-weight: 800;
}}
QLabel#dialogTitle {{
    font-size: 18px;
    font-weight: 800;
}}
QLabel#fieldLabel {{
    color: {C['soft']};
    font-size: 13px;
    font-weight: 750;
}}
QLineEdit#input, QLineEdit#searchInput, QComboBox#input, QPlainTextEdit#textInput {{
    background: white;
    border: 1px solid {C['border_mid']};
    border-radius: 11px;
    padding: 9px 11px;
    selection-background-color: {C['accent']};
}}
QLineEdit#searchInput {{
    background: {C['surface_alt']};
}}
QLineEdit#input:focus, QLineEdit#searchInput:focus, QComboBox#input:focus, QPlainTextEdit#textInput:focus {{
    border: 1px solid {C['accent']};
}}
QLineEdit#input:disabled, QComboBox#input:disabled, QPlainTextEdit#textInput:disabled {{
    background: {C['surface_alt']};
    color: {C['mute']};
    border-color: {C['border']};
}}
QComboBox#input::drop-down {{
    border: 0;
    width: 28px;
}}
QFrame#segment {{
    background: {C['surface_alt']};
    border: 1px solid {C['border']};
    border-radius: 13px;
}}
QPushButton#typeButton {{
    background: transparent;
    color: {C['soft']};
    border: 0;
    border-radius: 10px;
    padding: 9px 12px;
    font-weight: 650;
}}
QPushButton#typeButton[active="true"] {{
    background: white;
    color: {C['accent']};
    font-weight: 800;
}}
QPushButton[kind="primary"] {{
    background: {C['accent']};
    color: white;
    border: 0;
    border-radius: 11px;
    padding: 9px 16px;
    font-weight: 800;
}}
QPushButton[kind="primary"]:hover {{
    background: {C['accent_dk']};
}}
QPushButton[kind="ghost"], QPushButton[kind="tool"] {{
    background: white;
    color: {C['soft']};
    border: 1px solid {C['border_mid']};
    border-radius: 11px;
    padding: 8px 14px;
}}
QPushButton[kind="tool"] {{
    padding: 7px 11px;
}}
QPushButton[kind="ghost"]:hover, QPushButton[kind="tool"]:hover {{
    background: {C['surface_alt']};
    color: {C['text']};
}}
QPushButton[kind="danger"] {{
    background: white;
    color: {C['danger']};
    border: 1px solid #fecdd3;
    border-radius: 11px;
    padding: 7px 12px;
}}
QPushButton[kind="danger"]:hover {{
    background: {C['danger_soft']};
}}
QPushButton#stateToggle {{
    min-width: 44px;
    border: 0;
    border-radius: 10px;
    padding: 5px 9px;
    font-size: 11px;
    font-weight: 800;
}}
QPushButton#stateToggle[state="on"] {{
    color: #166534;
    background: #dcfce7;
}}
QPushButton#stateToggle[state="on"]:hover {{
    background: #bbf7d0;
}}
QPushButton#stateToggle[state="off"] {{
    color: {C['soft']};
    background: #e2e8f0;
}}
QPushButton#stateToggle[state="off"]:hover {{
    background: #cbd5e1;
}}
QPushButton:disabled {{
    color: {C['mute']};
    background: {C['surface_alt']};
    border-color: {C['border']};
}}
QCheckBox#switchCheck, QCheckBox#plainCheck {{
    color: {C['soft']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid {C['border_mid']};
    background: white;
}}
QCheckBox::indicator:checked {{
    background: {C['accent']};
    border-color: {C['accent']};
}}
QPushButton#typeOption {{
    background: white;
    border: 1px solid {C['border']};
    border-radius: 14px;
    text-align: left;
}}
QPushButton#typeOption:hover {{
    border-color: {C['accent']};
    background: {C['accent_soft']};
}}
QLabel#optionTitle {{
    font-size: 14px;
    font-weight: 800;
}}
QLabel#optionDesc {{
    color: {C['mute']};
    font-size: 12px;
}}
QScrollArea#editorScroll {{
    background: transparent;
}}
QWidget#formHost {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 36px;
}}
QScrollBar::handle:vertical:hover {{
    background: #94a3b8;
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("公益站 & 账号管理")
    app.setFont(QFont(F, 10))
    win = App()
    win.show()
    exit_code = app.exec()
    # 事件循环退出后强制终止进程：QThreadPool 中在飞的网络任务、BrowserWorker 拉起的
    # Playwright/Chromium 子进程等非守护线程会让解释器挂起无法退出。os._exit 绕过
    # 正常清理直接结束进程（OS 会回收文件锁与子进程），确保界面关闭后后台一并退出。
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)


if __name__ == "__main__":
    main()
