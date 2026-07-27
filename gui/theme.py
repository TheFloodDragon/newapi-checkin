# -*- coding: utf-8 -*-
"""设计令牌 + QSS 生成 + 偏好持久化。

深浅两套主题共用一份 QSS 模板；颜色全部来自令牌，杜绝散落的硬编码色值。
默认深色（“中转站控制台”视觉），可在顶栏切换并经 QSettings 记忆。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSettings

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"

DEFAULT_THEME = "dark"

_LIGHT: dict[str, Any] = {
    "bg": "#eef2f7",
    "surface": "#ffffff",
    "surface_alt": "#f6f8fb",
    "input_bg": "#ffffff",
    "border": "#e2e8f0",
    "border_mid": "#cbd5e1",
    "accent": "#0f9d8f",
    "accent_dk": "#0b8276",
    "accent_soft": "#d9f3ef",
    "on_accent": "#ffffff",
    "text": "#0f172a",
    "soft": "#475569",
    "mute": "#94a3b8",
    "hover": "#f8fafc",
    "item_off": "#f8fafc",
    "ok": "#16a34a",
    "ok_bg": "#ecfdf5",
    "warn": "#d97706",
    "warn_bg": "#fffbeb",
    "danger": "#e11d48",
    "danger_bg": "#fff1f2",
    "danger_border": "#fecdd3",
    "pill_done_fg": "#166534",
    "pill_done_bg": "#dcfce7",
    "pill_todo_fg": "#d97706",
    "pill_todo_bg": "#fffbeb",
    "pill_fail_fg": "#e11d48",
    "pill_fail_bg": "#fff1f2",
    "pill_unknown_fg": "#94a3b8",
    "pill_unknown_bg": "#f1f5f9",
    "badge_newapi_fg": "#3730a3",
    "badge_newapi_bg": "#e0e7ff",
    "badge_sub2api_fg": "#065f46",
    "badge_sub2api_bg": "#d1fae5",
    "state_on_fg": "#166534",
    "state_on_bg": "#dcfce7",
    "state_on_hover": "#bbf7d0",
    "state_off_fg": "#475569",
    "state_off_bg": "#e2e8f0",
    "state_off_hover": "#cbd5e1",
    "scroll": "#cbd5e1",
    "scroll_hover": "#94a3b8",
    "log_bg": "#101828",
    "log_fg": "#cbd5e1",
    "shadow_rgba": (15, 23, 42, 20),
}

_DARK: dict[str, Any] = {
    "bg": "#0d1424",
    "surface": "#151d31",
    "surface_alt": "#1a2440",
    "input_bg": "#101a30",
    "border": "#24304e",
    "border_mid": "#31406a",
    "accent": "#2fd6bd",
    "accent_dk": "#26bfa8",
    "accent_soft": "#103430",
    "on_accent": "#052e2b",
    "text": "#e8eef9",
    "soft": "#a9b7d0",
    "mute": "#67789a",
    "hover": "#1b2542",
    "item_off": "#121a2e",
    "ok": "#4ade80",
    "ok_bg": "#10331f",
    "warn": "#fbbf24",
    "warn_bg": "#33270b",
    "danger": "#fb7185",
    "danger_bg": "#3a1524",
    "danger_border": "#58253a",
    "pill_done_fg": "#86efac",
    "pill_done_bg": "#123524",
    "pill_todo_fg": "#fcd34d",
    "pill_todo_bg": "#33270b",
    "pill_fail_fg": "#fda4af",
    "pill_fail_bg": "#3a1524",
    "pill_unknown_fg": "#8ea2c0",
    "pill_unknown_bg": "#1e2a48",
    "badge_newapi_fg": "#a5b4fc",
    "badge_newapi_bg": "#252e5b",
    "badge_sub2api_fg": "#6ee7b7",
    "badge_sub2api_bg": "#0e3a2d",
    "state_on_fg": "#86efac",
    "state_on_bg": "#123524",
    "state_on_hover": "#174a2f",
    "state_off_fg": "#a9b7d0",
    "state_off_bg": "#24304e",
    "state_off_hover": "#31406a",
    "scroll": "#31406a",
    "scroll_hover": "#43558a",
    "log_bg": "#0a101f",
    "log_fg": "#9fb3d4",
    "shadow_rgba": (0, 0, 0, 120),
}

THEMES = {"light": _LIGHT, "dark": _DARK}


def tokens(name: str) -> dict[str, Any]:
    return THEMES.get(name, THEMES[DEFAULT_THEME])


def type_badge_colors(name: str, site_type: str) -> tuple[str, str]:
    t = tokens(name)
    if site_type == "sub2api":
        return t["badge_sub2api_fg"], t["badge_sub2api_bg"]
    return t["badge_newapi_fg"], t["badge_newapi_bg"]


def shadow_rgba(name: str) -> tuple[int, int, int, int]:
    return tokens(name)["shadow_rgba"]


# ── 偏好持久化 ────────────────────────────────────────────────────────────────
def _settings() -> QSettings:
    return QSettings("newapi-checkin", "manage-gui")


def load_pref(key: str, default: Any = None) -> Any:
    return _settings().value(key, default)


def save_pref(key: str, value: Any) -> None:
    _settings().setValue(key, value)


def load_theme() -> str:
    name = str(load_pref("theme", DEFAULT_THEME) or DEFAULT_THEME)
    return name if name in THEMES else DEFAULT_THEME


def save_theme(name: str) -> None:
    save_pref("theme", name if name in THEMES else DEFAULT_THEME)


# ── QSS 生成 ─────────────────────────────────────────────────────────────────
def build_qss(name: str) -> str:
    t = tokens(name)
    return f"""
* {{
    font-family: "{FONT_FAMILY}", "Microsoft YaHei UI", sans-serif;
    color: {t['text']};
}}
QWidget#appRoot {{
    background: {t['bg']};
}}
QFrame#topbar, QFrame#footer {{
    background: {t['surface']};
    border: 0;
}}
QFrame#topbar {{
    border-bottom: 1px solid {t['border']};
}}
QFrame#footer {{
    border-top: 1px solid {t['border']};
}}
QLabel#mark {{
    background: {t['accent']};
    color: {t['on_accent']};
    border-radius: 10px;
    font-size: 16px;
    font-weight: 800;
}}
QLabel#appTitle {{
    font-size: 17px;
    font-weight: 800;
}}
QLabel#saveStatus {{
    border-radius: 13px;
    padding: 5px 12px;
    font-weight: 700;
}}
QLabel#saveStatus[state="saved"] {{
    color: {t['ok']};
    background: {t['ok_bg']};
}}
QLabel#saveStatus[state="dirty"] {{
    color: {t['warn']};
    background: {t['warn_bg']};
}}
QPushButton#themeToggle {{
    background: {t['surface_alt']};
    color: {t['soft']};
    border: 1px solid {t['border']};
    border-radius: 13px;
    padding: 5px 12px;
    font-weight: 700;
}}
QPushButton#themeToggle:hover {{
    border-color: {t['accent']};
    color: {t['text']};
}}
QFrame#overviewBar {{
    background: transparent;
}}
QFrame#statChip {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
QLabel#statValue {{
    font-size: 16px;
    font-weight: 800;
}}
QLabel#statValue[tone="ok"] {{ color: {t['ok']}; }}
QLabel#statValue[tone="warn"] {{ color: {t['warn']}; }}
QLabel#statValue[tone="danger"] {{ color: {t['danger']}; }}
QLabel#statValue[tone="accent"] {{ color: {t['accent']}; }}
QLabel#statCaption {{
    color: {t['mute']};
    font-size: 10px;
    font-weight: 700;
}}
QFrame#sidebar, QFrame#card, QFrame#summaryCard {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 16px;
}}
QLabel#sectionTitle, QLabel#editTitle {{
    font-size: 16px;
    font-weight: 800;
}}
QLabel#countBadge {{
    color: {t['mute']};
    background: {t['surface_alt']};
    border-radius: 9px;
    padding: 2px 8px;
}}
QLabel#sidebarHint {{
    color: {t['mute']};
    font-size: 12px;
    padding: 0 2px 2px 2px;
}}
QListWidget#siteList {{
    background: transparent;
    border: 0;
    outline: 0;
}}
QListWidget#siteList[dragging="true"] {{
    background: {t['hover']};
    border-radius: 14px;
}}
QListWidget#siteList::item {{
    border: 0;
    padding: 0;
    margin: 0;
}}
QListWidget#siteList::item:hover {{
    background: {t['hover']};
}}
QListWidget#siteList::item:selected {{
    background: transparent;
}}
QWidget#siteItem {{
    background: transparent;
    border-radius: 12px;
    border-left: 3px solid transparent;
}}
QWidget#siteItem[selected="true"] {{
    background: {t['accent_soft']};
    border-left: 3px solid {t['accent']};
}}
QWidget#siteItem[enabledState="off"] {{
    background: {t['item_off']};
}}
QWidget#siteItem[enabledState="off"] QLabel#siteName {{
    color: {t['soft']};
}}
QLabel#dragHandle {{
    color: {t['border_mid']};
    font-size: 14px;
    font-weight: 800;
}}
QLabel#siteName {{
    font-size: 13px;
    font-weight: 750;
}}
QLabel#siteUrl, QLabel#summaryUrl, QLabel#hintText, QLabel#toast {{
    color: {t['mute']};
}}
QLabel#siteUrl, QLabel#summaryUrl {{
    font-size: 12px;
}}
QLabel#summaryState {{
    border-radius: 11px;
    padding: 6px 11px;
    font-weight: 800;
}}
QLabel#summaryState[state="on"] {{
    color: {t['pill_done_fg']};
    background: {t['pill_done_bg']};
}}
QLabel#summaryState[state="off"] {{
    color: {t['soft']};
    background: {t['surface_alt']};
}}
QLabel#summaryState[state="idle"] {{
    color: {t['mute']};
    background: {t['surface_alt']};
}}
QFrame#quotaBox {{
    background: {t['accent_soft']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
QLabel#quotaCaption {{
    color: {t['mute']};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#quotaValue {{
    color: {t['accent']};
}}
QLabel#quotaMini {{
    color: {t['soft']};
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
    color: {t['pill_done_fg']};
    background: {t['pill_done_bg']};
}}
QLabel#statusPill[kind="todo"], QLabel#statusPillLg[kind="todo"] {{
    color: {t['pill_todo_fg']};
    background: {t['pill_todo_bg']};
}}
QLabel#statusPill[kind="fail"], QLabel#statusPillLg[kind="fail"] {{
    color: {t['pill_fail_fg']};
    background: {t['pill_fail_bg']};
}}
QLabel#statusPill[kind="unknown"], QLabel#statusPillLg[kind="unknown"] {{
    color: {t['pill_unknown_fg']};
    background: {t['pill_unknown_bg']};
}}
QLabel#statusPill[kind="running"], QLabel#statusPillLg[kind="running"] {{
    color: {t['accent']};
    background: {t['accent_soft']};
}}
QPushButton#iconButton {{
    background: {t['surface']};
    border: 1px solid {t['border_mid']};
    border-radius: 15px;
    font-size: 14px;
    padding: 0;
}}
QPushButton#iconButton:hover {{
    background: {t['accent_soft']};
    border-color: {t['accent']};
}}
QPushButton#iconButton:disabled {{
    color: {t['mute']};
    background: {t['surface_alt']};
}}
QLabel#cardTitle {{
    font-size: 14px;
    font-weight: 800;
}}
QLabel#dialogTitle {{
    font-size: 18px;
    font-weight: 800;
}}
QLabel#fieldLabel {{
    color: {t['soft']};
    font-size: 12px;
    font-weight: 750;
}}
QLineEdit#input, QLineEdit#searchInput, QComboBox#input, QPlainTextEdit#textInput {{
    background: {t['input_bg']};
    border: 1px solid {t['border_mid']};
    border-radius: 10px;
    padding: 8px 11px;
    selection-background-color: {t['accent']};
    selection-color: {t['on_accent']};
}}
QLineEdit#searchInput {{
    background: {t['surface_alt']};
}}
QLineEdit#input:focus, QLineEdit#searchInput:focus, QComboBox#input:focus, QPlainTextEdit#textInput:focus {{
    border: 1px solid {t['accent']};
}}
QLineEdit#input:disabled, QComboBox#input:disabled, QPlainTextEdit#textInput:disabled {{
    background: {t['surface_alt']};
    color: {t['mute']};
    border-color: {t['border']};
}}
QComboBox#input::drop-down {{
    border: 0;
    width: 28px;
}}
QComboBox QAbstractItemView {{
    background: {t['surface']};
    border: 1px solid {t['border_mid']};
    border-radius: 8px;
    selection-background-color: {t['accent_soft']};
    selection-color: {t['text']};
    outline: 0;
}}
QFrame#segment {{
    background: {t['surface_alt']};
    border: 1px solid {t['border']};
    border-radius: 12px;
}}
QPushButton#typeButton {{
    background: transparent;
    color: {t['soft']};
    border: 0;
    border-radius: 9px;
    padding: 8px 12px;
    font-weight: 650;
}}
QPushButton#typeButton[active="true"] {{
    background: {t['surface']};
    color: {t['accent']};
    font-weight: 800;
}}
QPushButton[kind="primary"] {{
    background: {t['accent']};
    color: {t['on_accent']};
    border: 0;
    border-radius: 10px;
    padding: 8px 16px;
    font-weight: 800;
}}
QPushButton[kind="primary"]:hover {{
    background: {t['accent_dk']};
}}
QPushButton[kind="ghost"], QPushButton[kind="tool"] {{
    background: {t['surface']};
    color: {t['soft']};
    border: 1px solid {t['border_mid']};
    border-radius: 10px;
    padding: 8px 14px;
}}
QPushButton[kind="tool"] {{
    padding: 7px 11px;
}}
QPushButton[kind="ghost"]:hover, QPushButton[kind="tool"]:hover {{
    background: {t['surface_alt']};
    color: {t['text']};
}}
QPushButton[kind="danger"] {{
    background: {t['surface']};
    color: {t['danger']};
    border: 1px solid {t['danger_border']};
    border-radius: 10px;
    padding: 7px 12px;
}}
QPushButton[kind="danger"]:hover {{
    background: {t['danger_bg']};
}}
QPushButton#stateToggle {{
    min-width: 44px;
    border: 0;
    border-radius: 9px;
    padding: 5px 9px;
    font-size: 11px;
    font-weight: 800;
}}
QPushButton#stateToggle[state="on"] {{
    color: {t['state_on_fg']};
    background: {t['state_on_bg']};
}}
QPushButton#stateToggle[state="on"]:hover {{
    background: {t['state_on_hover']};
}}
QPushButton#stateToggle[state="off"] {{
    color: {t['state_off_fg']};
    background: {t['state_off_bg']};
}}
QPushButton#stateToggle[state="off"]:hover {{
    background: {t['state_off_hover']};
}}
QPushButton:disabled {{
    color: {t['mute']};
    background: {t['surface_alt']};
    border-color: {t['border']};
}}
QCheckBox#plainCheck {{
    color: {t['soft']};
    spacing: 8px;
}}
QCheckBox::indicator {{
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid {t['border_mid']};
    background: {t['input_bg']};
}}
QCheckBox::indicator:checked {{
    background: {t['accent']};
    border-color: {t['accent']};
}}
QPushButton#typeOption {{
    background: {t['surface']};
    border: 1px solid {t['border']};
    border-radius: 14px;
    text-align: left;
}}
QPushButton#typeOption:hover {{
    border-color: {t['accent']};
    background: {t['accent_soft']};
}}
QLabel#optionTitle {{
    font-size: 14px;
    font-weight: 800;
}}
QLabel#optionDesc {{
    color: {t['mute']};
    font-size: 12px;
}}
QScrollArea#editorScroll, QWidget#formHost {{
    background: transparent;
}}
QPlainTextEdit#logPanel {{
    background: {t['log_bg']};
    color: {t['log_fg']};
    border: 1px solid {t['border']};
    border-radius: 12px;
    padding: 8px;
    font-family: "{MONO_FAMILY}";
    font-size: 11px;
}}
QDialog, QMessageBox {{
    background: {t['surface']};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {t['scroll']};
    border-radius: 5px;
    min-height: 36px;
}}
QScrollBar::handle:vertical:hover {{
    background: {t['scroll_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0;
}}
"""
