# -*- coding: utf-8 -*-
"""纯展示组件：站点列表项 / 徽标 / 统计块 / 日志面板 / Toast 队列。

主题相关的内联色值统一经 set_theme() 注入，切换主题后由 App 触发整表重绘。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import core, theme

_THEME = theme.DEFAULT_THEME


def set_theme(name: str) -> None:
    global _THEME
    _THEME = name if name in theme.THEMES else theme.DEFAULT_THEME


def current_theme() -> str:
    return _THEME


def card_shadow(widget: QWidget) -> None:
    r, g, b, a = theme.shadow_rgba(_THEME)
    shadow = QGraphicsDropShadowEffect(widget)
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 8)
    shadow.setColor(QColor(r, g, b, a))
    widget.setGraphicsEffect(shadow)


def badge_style(fg: str, bg: str) -> str:
    return (
        f"QLabel {{ color: {fg}; background: {bg}; border-radius: 9px;"
        f" padding: 3px 9px; font-size: 11px; font-weight: 700; }}"
    )


def type_badge_style(site_type: str) -> str:
    fg, bg = theme.type_badge_colors(_THEME, site_type)
    return badge_style(fg, bg)


def repolish(widget: QWidget) -> None:
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class NoWheelComboBox(QComboBox):
    """禁止滚轮直接改变选项；下拉框仍可正常点击选择。"""

    def wheelEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        event.ignore()


# ── 站点列表项 ────────────────────────────────────────────────────────────────
class SiteItemWidget(QWidget):
    def __init__(
        self,
        row: core.SiteRow,
        selected: bool = False,
        on_toggle: Callable[[], None] | None = None,
        status: dict[str, Any] | None = None,
        running: bool = False,
    ):
        super().__init__()
        self._on_toggle = on_toggle
        self.setObjectName("siteItem")
        self.setProperty("selected", selected)
        self._build()
        self.update_row(row, status, running)
        self.apply_selected(selected)

    def _build(self) -> None:
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

        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(6)
        self.status_pill = QLabel()
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setAlignment(Qt.AlignCenter)
        status_row.addWidget(self.status_pill)
        self.quota_label = QLabel()
        self.quota_label.setObjectName("quotaMini")
        self.quota_label.setFont(QFont(theme.MONO_FAMILY, 10, QFont.Bold))
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
        self.state_btn.clicked.connect(self._toggle)
        badge_col.addWidget(self.state_btn)
        root.addLayout(badge_col, 0)

    def _toggle(self) -> None:
        if self._on_toggle is not None:
            self._on_toggle()

    def update_row(self, row: core.SiteRow, status: dict[str, Any] | None = None, running: bool = False) -> None:
        tokens = theme.tokens(_THEME)
        self.setProperty("enabledState", "on" if row.enabled else "off")
        self.name.setText(row.name or "（未命名）")
        self.url.setText(row.base_url or "—")
        self.url.setToolTip(row.base_url or "")
        self.dot.setStyleSheet(
            f"QLabel {{ background: {tokens['ok'] if row.enabled else tokens['mute']}; border-radius: 5px; }}"
        )
        self.state_btn.setText("启用" if row.enabled else "禁用")
        self.state_btn.setProperty("state", "on" if row.enabled else "off")
        repolish(self.state_btn)
        self.type_badge.setText(core.TYPE_LABELS.get(row.type, row.type))
        self.type_badge.setStyleSheet(type_badge_style(row.type))
        self._render_status(status, running)
        repolish(self)

    def _render_status(self, status: dict[str, Any] | None, running: bool) -> None:
        if running:
            self.status_pill.setText("⏳ 运行中")
            self.status_pill.setProperty("kind", "running")
            self.status_pill.setToolTip("")
            repolish(self.status_pill)
            return
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
                self.status_pill.setText(core.failure_label(str(status.get("status") or "error"), compact=True))
                self.status_pill.setProperty("kind", "fail")
            else:
                self.status_pill.setText("—")
                self.status_pill.setProperty("kind", "unknown")
            self.status_pill.setToolTip(message)

            if quota is not None:
                suffix = " (缓存)" if status.get("cached") else ""
                self.quota_label.setText(f"{core.format_usd(quota)}{suffix}")
                self.quota_label.setToolTip(message)
            else:
                # 失效/未取到实时额度时，回退展示失效前的历史额度（灰显标注）。
                last_quota = status.get("last_quota_usd")
                if isinstance(last_quota, (int, float)):
                    self.quota_label.setText(f"{core.format_usd(last_quota)} (失效前)")
                    self.quota_label.setToolTip(f"失效前的最后额度\n{message}" if message else "失效前的最后额度")
                else:
                    self.quota_label.setText("")
                    self.quota_label.setToolTip(message)
        repolish(self.status_pill)

    def apply_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        repolish(self)


class SiteListWidget(QListWidget):
    def __init__(self, on_reorder: Callable[[], None]):
        super().__init__()
        self._on_reorder = on_reorder

    def _set_dragging(self, value: bool) -> None:
        self.setProperty("dragging", value)
        repolish(self)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._set_dragging(True)
        super().dragEnterEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._set_dragging(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().dropEvent(event)
        self._set_dragging(False)
        self._on_reorder()

    def set_reorder_enabled(self, enabled: bool) -> None:
        self.setDragEnabled(enabled)
        self.setAcceptDrops(enabled)
        self.setDragDropMode(QAbstractItemView.InternalMove if enabled else QAbstractItemView.NoDragDrop)


# ── 概览统计块 ────────────────────────────────────────────────────────────────
class StatChip(QFrame):
    def __init__(self, caption: str, tone: str = ""):
        super().__init__()
        self.setObjectName("statChip")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8, 14, 8)
        layout.setSpacing(1)
        self.caption = QLabel(caption)
        self.caption.setObjectName("statCaption")
        self.value = QLabel("—")
        self.value.setObjectName("statValue")
        if tone:
            self.value.setProperty("tone", tone)
        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set_value(self, text: str) -> None:
        self.value.setText(text)
        repolish(self.value)


# ── 日志面板 ─────────────────────────────────────────────────────────────────
class LogPanel(QPlainTextEdit):
    def __init__(self):
        super().__init__()
        self.setObjectName("logPanel")
        self.setReadOnly(True)
        self.setMaximumBlockCount(2000)
        self.setFixedHeight(150)

    def append_line(self, line: str) -> None:
        self.appendPlainText(line)


# ── Toast 队列 ───────────────────────────────────────────────────────────────
class Toast(QObject):
    """footer 轻提示：即时消息直接展示，密集消息按队列轮播，最终自动清空。"""

    def __init__(self, label: QLabel, parent: QObject | None = None):
        super().__init__(parent)
        self._label = label
        self._queue: list[str] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

    def show(self, text: str) -> None:
        if self._timer.isActive() and self._label.text():
            self._queue.append(text)
            if len(self._queue) > 6:
                self._queue = self._queue[-6:]
            return
        self._display(text)

    def _display(self, text: str) -> None:
        self._label.setText(text)
        self._timer.start(2200 if self._queue else 4000)

    def _advance(self) -> None:
        if self._queue:
            self._display(self._queue.pop(0))
        else:
            self._label.setText("")

    def stop(self) -> None:
        self._timer.stop()
