# -*- coding: utf-8 -*-
"""主窗口装配与入口。

与旧版 manage_accounts.py 的功能对照：
- 保留：站点列表（搜索/拖拽排序/启停）、三维表单联动、OAuth 捕获/检测/删除、
  测试签到、实时查询、立即签到、保存/导出 Secret/剪贴板导入、脏状态与退出确认；
- 新增：概览统计条、「全部查询 / 全部签到」（旧版 _checkin_all 是无入口死代码）、
  GUI 内脱敏日志面板、深浅主题切换、Toast 队列；
- 移除：批量结果的长文本模态框（改为摘要 toast + 日志明细）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

import accounts_store
import time_utils
from checkin_core.batch import serial_groups
from mask_utils import mask_secrets

from . import config_store, core, theme
from . import widgets as w
from .dialogs import TypeDialog
from .workers import BrowserWorker, StorageRunner, TaskRunner


class _LogBridge(QObject):
    """把任意线程的日志行经队列信号转投到主线程日志面板。"""

    line = Signal(str)


def _button(text: str, kind: str = "ghost") -> QPushButton:
    btn = QPushButton(text)
    btn.setCursor(Qt.PointingHandCursor)
    btn.setProperty("kind", kind)
    return btn


class App(QMainWindow):
    def __init__(self, *, results_dir: Path | None = None):
        super().__init__()
        self.rows: list[core.SiteRow] = []
        self.oauth_states: dict[str, dict[str, Any]] = {}
        self.filtered_indices: list[int] = []
        self.cur: int | None = None
        self._lock = False
        self._dirty = False
        self._saved_snapshot = ""
        # 当前 GUI 会话内上次成功保存的凭据基线（按 SiteRow.runtime_id 关联）。
        # 稳定身份避免删除行后 CPython 复用对象 id，导致新行误继承旧凭据基线。
        self._saved_credentials: dict[str, dict[str, str]] = {}
        self._type_buttons: dict[str, QPushButton] = {}
        self._worker: BrowserWorker | None = None
        # 已结束、等待下次启动时统一回收的 worker。不在 finished 回调里立即
        # deleteLater：那时 QThread 可能尚未真正退出，而 capture 正处于
        # dlg.exec() 的嵌套事件循环中，会就地处理 DeferredDelete 并触发
        # 「QThread: Destroyed while thread is still running」直接终止进程。
        self._retired_worker: BrowserWorker | None = None
        self._capture_dialog: QMessageBox | None = None
        self._leases = core.TaskLeaseRegistry()
        self._batch_active = 0
        self._save_inflight = False
        self._config_load_failed = False

        self.store = core.StatusStore(results_dir=results_dir, autosave=False)
        self.store.load()
        self.runner = TaskRunner(self, max_threads=5)
        self.storage = StorageRunner(self)

        self._theme = theme.load_theme()
        w.set_theme(self._theme)
        self._shadow_targets: list[QWidget] = []

        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.timeout.connect(self._compute_dirty)
        self._status_save_timer = QTimer(self)
        self._status_save_timer.setSingleShot(True)
        self._status_save_timer.timeout.connect(self._persist_status_async)

        self._log_bridge = _LogBridge(self)
        core.add_log_sink(self._log_bridge.line.emit)

        self._win()
        self._build()
        self._hotkeys()
        self._apply_theme(initial=True)
        self._reload()

    # ── 窗口 / 主题 ──
    def _win(self) -> None:
        self.setWindowTitle("中转站控制台 · 公益站签到管理")
        self.resize(1220, 800)
        self.setMinimumSize(1000, 640)
        geometry = theme.load_pref("geometry")
        if geometry is not None:
            try:
                self.restoreGeometry(geometry)
            except Exception:
                pass

    def _apply_theme(self, initial: bool = False) -> None:
        w.set_theme(self._theme)
        self.setStyleSheet(theme.build_qss(self._theme))
        for target in self._shadow_targets:
            w.card_shadow(target)
        self.theme_btn.setText("☀ 浅色" if self._theme == "dark" else "🌙 深色")
        if not initial:
            self._render_list()
            self._update_summary()
            self._sync_type_styles()
            if self.cur is not None:
                self._apply_form_plan(self.rows[self.cur])

    def _toggle_theme(self) -> None:
        self._theme = "light" if self._theme == "dark" else "dark"
        theme.save_theme(self._theme)
        self._apply_theme()

    # ── 布局骨架 ──
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._topbar())
        root.addWidget(self._overview())

        body = QHBoxLayout()
        body.setContentsMargins(18, 4, 18, 10)
        body.setSpacing(14)
        root.addLayout(body, 1)

        body.addWidget(self._sidebar(), 0)
        body.addWidget(self._editor(), 1)

        self.log_panel = w.LogPanel()
        self.log_panel.setVisible(bool(theme.load_pref("log_visible", False) in (True, "true")))
        self._log_bridge.line.connect(self.log_panel.append_line)
        log_wrap = QHBoxLayout()
        log_wrap.setContentsMargins(18, 0, 18, 10)
        log_wrap.addWidget(self.log_panel)
        root.addLayout(log_wrap)

        root.addWidget(self._footer())

    def _topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        bar.setFixedHeight(60)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(12)

        mark = QLabel("⇌")
        mark.setObjectName("mark")
        mark.setAlignment(Qt.AlignCenter)
        mark.setFixedSize(34, 34)
        layout.addWidget(mark)

        title = QLabel("中转站控制台")
        title.setObjectName("appTitle")
        layout.addWidget(title)
        layout.addStretch(1)

        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("themeToggle")
        self.theme_btn.setCursor(Qt.PointingHandCursor)
        self.theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_btn)

        self.status = QLabel("● 已保存")
        self.status.setObjectName("saveStatus")
        self.status.setProperty("state", "saved")
        layout.addWidget(self.status)
        return bar

    def _overview(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("overviewBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 12, 18, 8)
        layout.setSpacing(10)

        self.chip_sites = w.StatChip("站点 启用/总数")
        self.chip_done = w.StatChip("今日已签", tone="ok")
        self.chip_quota = w.StatChip("已知总额度", tone="accent")
        self.chip_failed = w.StatChip("异常", tone="danger")
        for chip in (self.chip_sites, self.chip_done, self.chip_quota, self.chip_failed):
            layout.addWidget(chip)
        layout.addStretch(1)

        self.btn_query_all = _button("全部查询", "ghost")
        self.btn_query_all.clicked.connect(self._query_all)
        layout.addWidget(self.btn_query_all)
        self.btn_checkin_all = _button("全部签到", "primary")
        self.btn_checkin_all.clicked.connect(self._checkin_all)
        layout.addWidget(self.btn_checkin_all)
        return bar

    def _sidebar(self) -> QWidget:
        wrap = QFrame()
        wrap.setObjectName("sidebar")
        wrap.setFixedWidth(360)
        self._shadow_targets.append(wrap)

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
        self.search_edit.textChanged.connect(self._on_search_changed)
        layout.addWidget(self.search_edit)

        self.sidebar_hint = QLabel("拖动排序 · 点击右侧启用 / 禁用")
        self.sidebar_hint.setObjectName("sidebarHint")
        layout.addWidget(self.sidebar_hint)

        self.listw = w.SiteListWidget(self._sync_order_from_list)
        self.listw.setObjectName("siteList")
        self.listw.setSpacing(6)
        self.listw.set_reorder_enabled(True)
        self.listw.setDefaultDropAction(Qt.MoveAction)
        self.listw.setDropIndicatorShown(True)
        self.listw.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.listw.currentRowChanged.connect(self._select_visible)
        layout.addWidget(self.listw, 1)
        return wrap

    # 搜索防抖：避免每个按键全量重建列表
    def _on_search_changed(self, _text: str) -> None:
        if not hasattr(self, "_search_timer"):
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.timeout.connect(self._render_list)
        self._search_timer.start(180)

    def _editor(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        summary = QFrame()
        summary.setObjectName("summaryCard")
        self._shadow_targets.append(summary)
        scol = QVBoxLayout(summary)
        scol.setContentsMargins(18, 14, 18, 16)
        scol.setSpacing(12)

        top_row = QHBoxLayout()
        top_row.setSpacing(10)
        title_col = QVBoxLayout()
        title_col.setSpacing(3)
        title_line = QHBoxLayout()
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

        info_row = QHBoxLayout()
        info_row.setSpacing(12)
        quota_box = QFrame()
        quota_box.setObjectName("quotaBox")
        qb = QHBoxLayout(quota_box)
        qb.setContentsMargins(14, 8, 10, 8)
        qb.setSpacing(10)
        qb_text = QVBoxLayout()
        qb_text.setSpacing(1)
        qcap = QLabel("当前额度")
        qcap.setObjectName("quotaCaption")
        qb_text.addWidget(qcap)
        self.quota_value = QLabel("—")
        self.quota_value.setObjectName("quotaValue")
        self.quota_value.setFont(QFont(theme.MONO_FAMILY, 17, QFont.Bold))
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

        self.checkin_pill = QLabel("未查询")
        self.checkin_pill.setObjectName("statusPillLg")
        self.checkin_pill.setProperty("kind", "unknown")
        self.checkin_pill.setAlignment(Qt.AlignCenter)
        info_row.addWidget(self.checkin_pill, 0, Qt.AlignVCenter)
        info_row.addStretch(1)

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
        foot.setFixedHeight(60)
        layout = QHBoxLayout(foot)
        layout.setContentsMargins(24, 0, 24, 0)
        layout.setSpacing(9)

        self.toast_label = QLabel("Ctrl+S 保存 · Ctrl+N 新增 · Del 删除")
        self.toast_label.setObjectName("toast")
        layout.addWidget(self.toast_label, 1)
        self.toast = w.Toast(self.toast_label, self)

        self.btn_log = _button("日志", "ghost")
        self.btn_log.clicked.connect(self._toggle_log)
        layout.addWidget(self.btn_log)

        reload_btn = _button("重新加载", "ghost")
        reload_btn.clicked.connect(self._reload)
        layout.addWidget(reload_btn)

        export_btn = _button("导出 Secret", "ghost")
        export_btn.clicked.connect(self._export)
        layout.addWidget(export_btn)

        self.save_btn = _button("保存全部", "primary")
        self.save_btn.clicked.connect(self._save)
        layout.addWidget(self.save_btn)
        return foot

    def _toggle_log(self) -> None:
        visible = not self.log_panel.isVisible()
        self.log_panel.setVisible(visible)
        theme.save_pref("log_visible", visible)

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

        auth_wrap = self._field(site_layout, "登录方式")
        self.auth_combo = w.NoWheelComboBox()
        self.auth_combo.setObjectName("input")
        self.auth_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in core.AUTH_METHODS:
            self.auth_combo.addItem(core.AUTH_METHOD_LABELS.get(m, m), m)
        auth_wrap.layout().addWidget(self.auth_combo)

        action_wrap = self._field(site_layout, "签到方式")
        self.action_combo = w.NoWheelComboBox()
        self.action_combo.setObjectName("input")
        self.action_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in core.CHECKIN_ACTIONS:
            self.action_combo.addItem(core.ACTION_LABELS.get(m, m), m)
        action_wrap.layout().addWidget(self.action_combo)

        self.oauth_provider_wrap = self._field(site_layout, "OAuth 提供商", "共享登录态来源")
        self.oauth_provider_combo = w.NoWheelComboBox()
        self.oauth_provider_combo.setObjectName("input")
        self.oauth_provider_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in core.OAUTH_PROVIDERS:
            self.oauth_provider_combo.addItem(core.OAUTH_PROVIDER_LABELS.get(m, m), m)
        self.oauth_provider_wrap.layout().addWidget(self.oauth_provider_combo)

        self.oauth_account_wrap = self._field(site_layout, "OAuth 账号", "同一提供商可保存多个账号")
        account_row = QHBoxLayout()
        account_row.setContentsMargins(0, 0, 0, 0)
        account_row.setSpacing(8)
        self.oauth_account_combo = w.NoWheelComboBox()
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

        self.variant_wrap = self._field(site_layout, "接口变体", "仅 New API 接口签到")
        self.variant_combo = w.NoWheelComboBox()
        self.variant_combo.setObjectName("input")
        self.variant_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for m in core.API_VARIANTS:
            self.variant_combo.addItem(core.API_VARIANT_LABELS.get(m, m), m)
        self.variant_wrap.layout().addWidget(self.variant_combo)

        self.verification_wrap = self._field(
            site_layout,
            "验证方式",
            "未选择时自动识别；选择后优先该机制，不适用时回落自动分流",
        )
        self.verification_combo = w.NoWheelComboBox()
        self.verification_combo.setObjectName("input")
        self.verification_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for mode in core.VERIFICATION_MODES:
            self.verification_combo.addItem(
                core.VERIFICATION_MODE_LABELS.get(mode, mode), mode
            )
        self.verification_wrap.layout().addWidget(self.verification_combo)

        self.script_wrap = self._field(site_layout, "脚本路径", core.SCRIPT_HINT_BROWSER)
        self.script_edit = QLineEdit()
        self.script_edit.setObjectName("input")
        self.script_edit.setPlaceholderText(core.SCRIPT_PLACEHOLDER_BROWSER)
        self.script_wrap.layout().addWidget(self.script_edit)

        self.script_args_wrap = self._field(site_layout, "脚本参数 JSON", "传给 site.script_args")
        self.script_args_edit = QPlainTextEdit()
        self.script_args_edit.setObjectName("textInput")
        self.script_args_edit.setFixedHeight(88)
        self.script_args_edit.setPlaceholderText('{\n  "checkin_text": "签到"\n}')
        self.script_args_wrap.layout().addWidget(self.script_args_edit)

        self.script_timeout_wrap = self._field(
            site_layout, "脚本超时（秒）", f"默认 {core.SCRIPT_TIMEOUT_DEFAULT}"
        )
        self.script_timeout_edit = QLineEdit()
        self.script_timeout_edit.setObjectName("input")
        self.script_timeout_edit.setPlaceholderText(str(core.SCRIPT_TIMEOUT_DEFAULT))
        self.script_timeout_wrap.layout().addWidget(self.script_timeout_edit)

        self.mode_hint = QLabel("")
        self.mode_hint.setObjectName("hintText")
        self.mode_hint.setWordWrap(True)
        site_layout.addWidget(self.mode_hint)
        site_layout.addStretch(1)

        cred_card = self._card("认证凭据", "保存后写入本地 ACCOUNTS.json（已被 .gitignore）", parent_layout=columns)
        cred_layout = cred_card.layout()

        self.token_edit = self._line(cred_layout, "Access Token", mono=True)
        # refresh_token 需要可编辑：sub2api 的 access_token 只有几小时有效期，长期能
        # 免浏览器续期全靠它。以前只有一行「有/无」提示，用户即便手上有有效值也无处
        # 填写，只能靠浏览器捕获——而捕获本身可能因站点风控（如 Turnstile）失败。
        self.refresh_wrap = self._field(
            cred_layout, "Refresh Token", "sub2api 长期凭据，Token 过期后纯 HTTP 续期"
        )
        self.refresh_edit = QLineEdit()
        self.refresh_edit.setObjectName("input")
        self.refresh_edit.setFont(QFont(theme.MONO_FAMILY, 10))
        self.refresh_edit.setPlaceholderText("rt_... 由浏览器捕获自动写入，也可手工粘贴")
        self.refresh_wrap.layout().addWidget(self.refresh_edit)
        self.uid_edit = self._line(cred_layout, "用户 ID", "newapi 的 New-Api-User")

        cookie_wrap = self._field(cred_layout, "Cookie")
        self.cookie_edit = QPlainTextEdit()
        self.cookie_edit.setObjectName("textInput")
        self.cookie_edit.setFixedHeight(104)
        cookie_wrap.layout().addWidget(self.cookie_edit)

        self.state_wrap = self._field(cred_layout, "站点登录状态", "浏览器捕获产物，用于自动登录 / 刷新 token")
        self.state_edit = QPlainTextEdit()
        self.state_edit.setObjectName("textInput")
        self.state_edit.setFixedHeight(80)
        self.state_edit.setPlaceholderText("非 relogin 场景可粘贴 base64 浏览器登录态（如 sub2api 自动刷新）")
        self.state_wrap.layout().addWidget(self.state_edit)
        self.oauth_state_status = QLabel("")
        self.oauth_state_status.setObjectName("hintText")
        self.oauth_state_status.setWordWrap(True)
        self.state_wrap.layout().addWidget(self.oauth_state_status)

        # refresh_token 状态（仅 sub2api）：它决定 Token 过期时能否纯 HTTP 续期，
        # 不展示的话用户无从判断某站点是否还需要每次开浏览器。只显示有无，不显示值。
        self.refresh_token_hint = QLabel("")
        self.refresh_token_hint.setObjectName("hintText")
        self.refresh_token_hint.setWordWrap(True)
        cred_layout.addWidget(self.refresh_token_hint)

        self.oauth_fallback_wrap = self._field(cred_layout, "可选 OAuth")
        fallback_row = QHBoxLayout()
        fallback_row.setContentsMargins(0, 0, 0, 0)
        fallback_row.setSpacing(8)
        self.oauth_fallback_combo = w.NoWheelComboBox()
        self.oauth_fallback_combo.setObjectName("input")
        self.oauth_fallback_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.oauth_fallback_combo.addItem("不使用", "")
        fallback_row.addWidget(self.oauth_fallback_combo, 1)
        self.btn_oauth_fallback_refresh = _button("刷新账号", "tool")
        self.btn_oauth_fallback_refresh.clicked.connect(self._reload_oauth_accounts)
        fallback_row.addWidget(self.btn_oauth_fallback_refresh)
        self.oauth_fallback_wrap.layout().addLayout(fallback_row)

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

        self.proxy_edit = self._line(cred_layout, "代理（可选）", "如 http://user:pass@host:port")

        self.verify_ssl_wrap = self._field(cred_layout, "TLS 证书校验", "默认开启；仅证书过期/链异常站点临时关闭")
        self.verify_ssl_check = QCheckBox("校验 HTTPS 证书和主机名")
        self.verify_ssl_check.setObjectName("plainCheck")
        self.verify_ssl_check.setChecked(True)
        self.verify_ssl_wrap.layout().addWidget(self.verify_ssl_check)

        # 以下四项 checkin.py / run__all_checkin.py 一直在消费，但此前 GUI 既不展示
        # 也不写回：用户在 ACCOUNTS.json 手写的值会被「保存全部」静默抹掉。
        self.referer_wrap = self._field(
            cred_layout, "Referer 路径", f"newapi 请求头用，默认 {core.REFERER_PATH_DEFAULT}"
        )
        self.referer_edit = QLineEdit()
        self.referer_edit.setObjectName("input")
        self.referer_edit.setPlaceholderText(core.REFERER_PATH_DEFAULT)
        self.referer_wrap.layout().addWidget(self.referer_edit)

        self.cookie_file_wrap = self._field(
            cred_layout, "凭据文件路径", "可选；三行格式（Cookie / user_id / token），留空则用上面的字段"
        )
        self.cookie_file_edit = QLineEdit()
        self.cookie_file_edit.setObjectName("input")
        self.cookie_file_edit.setPlaceholderText("如 secrets/site_token.txt")
        self.cookie_file_wrap.layout().addWidget(self.cookie_file_edit)

        self.browser_profile_wrap = self._field(
            cred_layout, "浏览器 Profile 目录", f"browser / oauth 登录方式复用，默认 {core.BROWSER_PROFILE_DEFAULT}"
        )
        self.browser_profile_edit = QLineEdit()
        self.browser_profile_edit.setObjectName("input")
        self.browser_profile_edit.setPlaceholderText(core.BROWSER_PROFILE_DEFAULT)
        self.browser_profile_wrap.layout().addWidget(self.browser_profile_edit)

        self.auto_refresh_wrap = self._field(
            cred_layout, "Cookie 文件自动清理", "默认开启；关闭后仍在内存去重，但不回写凭据文件"
        )
        self.auto_refresh_check = QCheckBox("自动回写去重后的 Cookie")
        self.auto_refresh_check.setObjectName("plainCheck")
        self.auto_refresh_check.setChecked(True)
        self.auto_refresh_wrap.layout().addWidget(self.auto_refresh_check)

        actions = QHBoxLayout()
        actions.setContentsMargins(0, 4, 0, 0)
        imp_btn = _button("从剪贴板导入", "tool")
        imp_btn.clicked.connect(self._imp)
        actions.addWidget(imp_btn)
        cp_btn = _button("复制凭据 JSON", "tool")
        cp_btn.clicked.connect(self._cpcred)
        actions.addWidget(cp_btn)
        self.btn_test = _button("测试签到", "tool")
        self.btn_test.clicked.connect(self._test_checkin)
        actions.addWidget(self.btn_test)
        actions.addStretch(1)
        cred_layout.addLayout(actions)

        self.form.addStretch(1)

        # 信号
        self.name_edit.textChanged.connect(self._flush)
        self.base_edit.textChanged.connect(self._flush)
        self.auth_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.action_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.oauth_provider_combo.currentIndexChanged.connect(self._on_oauth_provider_changed)
        self.oauth_account_combo.currentIndexChanged.connect(self._on_oauth_account_changed)
        self.oauth_fallback_combo.currentIndexChanged.connect(self._on_combo_changed)
        if self.oauth_account_combo.lineEdit():
            self.oauth_account_combo.lineEdit().editingFinished.connect(self._on_combo_changed)
        self.variant_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.verification_combo.currentIndexChanged.connect(self._on_combo_changed)
        self.script_edit.textChanged.connect(self._flush)
        self.script_args_edit.textChanged.connect(self._flush)
        self.script_timeout_edit.textChanged.connect(self._flush)
        self.token_edit.textChanged.connect(self._flush)
        self.refresh_edit.textChanged.connect(self._flush)
        self.uid_edit.textChanged.connect(self._flush)
        self.cookie_edit.textChanged.connect(self._flush)
        self.state_edit.textChanged.connect(self._flush)
        self.proxy_edit.textChanged.connect(self._flush)
        self.verify_ssl_check.stateChanged.connect(self._flush)
        self.cookie_file_edit.textChanged.connect(self._flush)
        self.referer_edit.textChanged.connect(self._flush)
        self.browser_profile_edit.textChanged.connect(self._flush)
        self.auto_refresh_check.stateChanged.connect(self._flush)

    def _card(self, title: str, subtitle: str = "", parent_layout=None) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        card.setMinimumWidth(0)
        self._shadow_targets.append(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(22, 18, 22, 22)
        layout.setSpacing(12)

        header = QHBoxLayout()
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

        (parent_layout or self.form).addWidget(card)
        return card

    def _field(self, parent_layout, label: str, hint: str = "") -> QWidget:
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)
        top = QHBoxLayout()
        lab = QLabel(label)
        lab.setObjectName("fieldLabel")
        top.addWidget(lab)
        if hint:
            h = QLabel(hint)
            h.setObjectName("hintText")
            h.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            top.addWidget(h, 1)
            # 存下来供 _set_field_hint 改写：同一个字段在不同签到方式下含义不同
            # （脚本路径在 api 与 browser_script 下要给不同的示例）。
            wrap.setProperty("hintLabel", h)
        top.addStretch(0)
        lay.addLayout(top)
        parent_layout.addWidget(wrap)
        return wrap

    @staticmethod
    def _set_field_hint(wrap: QWidget, text: str) -> None:
        label = wrap.property("hintLabel")
        if label is not None:
            label.setText(text)

    def _line(self, parent_layout, label: str, hint: str = "", mono: bool = False) -> QLineEdit:
        wrap = self._field(parent_layout, label, hint)
        edit = QLineEdit()
        edit.setObjectName("input")
        if mono:
            edit.setFont(QFont(theme.MONO_FAMILY, 10))
        wrap.layout().addWidget(edit)
        return edit

    def _type_segment(self, parent_layout) -> None:
        wrap = self._field(parent_layout, "站点类型")
        seg = QFrame()
        seg.setObjectName("segment")
        row = QHBoxLayout(seg)
        row.setContentsMargins(3, 3, 3, 3)
        row.setSpacing(3)
        self.type_group = QButtonGroup(self)
        self.type_group.setExclusive(True)
        for t in core.TYPES:
            btn = QPushButton(core.TYPE_LABELS[t])
            btn.setObjectName("typeButton")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, tt=t: self._set_type(tt))
            self.type_group.addButton(btn)
            self._type_buttons[t] = btn
            row.addWidget(btn, 1)
        wrap.layout().addWidget(seg)

    def _hotkeys(self) -> None:
        QShortcut(QKeySequence("Ctrl+S"), self, self._save)
        QShortcut(QKeySequence("Ctrl+N"), self, self._add)
        QShortcut(QKeySequence("Ctrl+L"), self, self._reload)
        QShortcut(QKeySequence("Delete"), self, self._del)

    # ── 数据装载 / 列表 ──
    def _reload(self) -> None:
        if self.cur is not None:
            self._flush()
        if self._dirty_timer.isActive():
            self._dirty_timer.stop()
            self._compute_dirty()
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "放弃未保存更改？",
                "重新加载会放弃当前未保存的更改，确定继续吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        try:
            loaded = config_store.load_configuration()
        except Exception as exc:
            # 事务式失败：保留当前 rows / oauth_states / 保存基线，绝不清空后再标成已保存。
            self._config_load_failed = True
            QMessageBox.critical(self, "重新加载失败", mask_secrets(str(exc)))
            self._say("重新加载失败，已保留当前内存配置；修复文件并成功重载前不会写盘")
            return

        self._config_load_failed = False
        self.rows = loaded.rows
        self.oauth_states = loaded.oauth_states
        self.store.load()
        self.cur = None
        self.search_edit.clear()
        self._render_list()
        if self.rows:
            self.listw.setCurrentRow(0)
            self._select_real(0)
        else:
            self._clear()
        self._mark_saved()
        self._update_overview()

    def _matches_filter(self, row: core.SiteRow, query: str) -> bool:
        if not query:
            return True
        haystack = " ".join(
            part.lower() for part in (row.name, row.base_url, row.type, core.TYPE_LABELS.get(row.type, ""))
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
        self.listw.set_reorder_enabled(drag_enabled)
        self.sidebar_hint.setText(
            "拖动排序 · 点击右侧启用 / 禁用" if drag_enabled else "搜索结果中不可排序，清空搜索后可拖动排序"
        )

        self.listw.blockSignals(True)
        self.listw.clear()
        for real_idx in self.filtered_indices:
            row = self.rows[real_idx]
            item = QListWidgetItem()
            item.setData(Qt.UserRole, real_idx)
            widget = w.SiteItemWidget(
                row,
                real_idx == self.cur,
                on_toggle=lambda idx=real_idx: self._toggle_enabled(idx),
                status=self.store.get(core.StatusStore.status_key(row)),
                running=self._leases.is_channel_running(core.StatusStore.task_key(row)),
            )
            item.setSizeHint(widget.sizeHint())
            self.listw.addItem(item)
            self.listw.setItemWidget(item, widget)
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self.listw.blockSignals(False)
        shown, total = len(self.filtered_indices), len(self.rows)
        self.count.setText(f"{shown}/{total}" if query else str(total))
        self._update_overview()

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
        self._schedule_dirty()
        self._say("已更新站点顺序，保存后生效")

    def _toggle_enabled(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.rows):
            return
        if self.cur is not None:
            self._flush()
        self.rows[idx].enabled = not self.rows[idx].enabled
        self._refresh_row(idx)
        if idx == self.cur:
            self._update_summary(self.rows[idx])
        self._schedule_dirty()
        self._update_overview()
        self._say(f"已{'启用' if self.rows[idx].enabled else '关闭'}「{self.rows[idx].name or '未命名站点'}」")

    def _refresh_row(self, idx: int) -> None:
        pos = self._visible_pos(idx)
        if pos < 0 or pos >= self.listw.count():
            return
        widget = self.listw.itemWidget(self.listw.item(pos))
        if isinstance(widget, w.SiteItemWidget):
            row = self.rows[idx]
            widget.update_row(
                row,
                self.store.get(core.StatusStore.status_key(row)),
                running=self._leases.is_channel_running(core.StatusStore.task_key(row)),
            )
            widget.apply_selected(idx == self.cur)

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

    # ── 表单读写 ──
    def _load(self, idx: int) -> None:
        self._lock = True
        row = self.rows[idx]
        self.name_edit.setText(row.name)
        self.base_edit.setText(row.base_url)
        self._set_type_value(row.type)
        self._set_combo_value(self.auth_combo, row.auth_method, "cookie")
        self._set_combo_value(self.action_combo, row.checkin_action, "api")
        self._set_combo_value(self.variant_combo, row.api_variant, "auto")
        self._set_combo_value(
            self.verification_combo, row.verification_mode, "auto"
        )
        self._set_combo_value(self.oauth_provider_combo, row.oauth_provider, "linuxdo")
        self._refresh_oauth_account_choices(row.oauth_account or core.DEFAULT_OAUTH_ACCOUNT)
        self._refresh_oauth_fallback_choices(row.oauth_fallback_provider, row.oauth_fallback_account)
        self.script_edit.setText(row.script)
        self.script_args_edit.setPlainText(row.script_args_text)
        self.script_timeout_edit.setText(str(row.script_timeout))
        self.state_edit.setPlainText(row.browser_state)
        self.proxy_edit.setText(row.proxy)
        self.cookie_file_edit.setText(row.cookie_file)
        self.referer_edit.setText(row.referer_path)
        self.browser_profile_edit.setText(row.browser_profile)
        self.auto_refresh_check.setChecked(row.auto_refresh_cookie)
        self.verify_ssl_check.setChecked(row.verify_ssl)
        self.uid_edit.setText(row.user_id)
        self.token_edit.setText(row.access_token)
        self.refresh_edit.setText(row.refresh_token)
        self.cookie_edit.setPlainText(row.cookie)
        self._update_summary(row)
        self._set_actions_enabled(True)
        self._lock = False
        self._apply_form_plan(row)
        self._sync_type_styles()

    def _clear(self) -> None:
        self._lock = True
        self.cur = None
        for edit in (self.name_edit, self.base_edit, self.script_edit, self.script_timeout_edit,
                     self.state_edit, self.proxy_edit, self.uid_edit, self.token_edit,
                     self.refresh_edit):
            edit.clear()
        self._set_type_value("newapi")
        self._set_combo_value(self.auth_combo, "cookie", "cookie")
        self._set_combo_value(self.action_combo, "api", "api")
        self._set_combo_value(self.variant_combo, "auto", "auto")
        self._set_combo_value(self.verification_combo, "auto", "auto")
        self._set_combo_value(self.oauth_provider_combo, "linuxdo", "linuxdo")
        self._refresh_oauth_account_choices(core.DEFAULT_OAUTH_ACCOUNT)
        self._refresh_oauth_fallback_choices()
        self.script_args_edit.setPlainText("{}")
        self.script_timeout_edit.setText(str(core.SCRIPT_TIMEOUT_DEFAULT))
        self.cookie_edit.clear()
        # 这几项有非空默认值，必须显式回到默认而不是 clear()，否则新站点会继承
        # 上一个站点的值（或拿到空串而非 CLI 默认）。
        self.cookie_file_edit.clear()
        self.referer_edit.setText(core.REFERER_PATH_DEFAULT)
        self.browser_profile_edit.setText(core.BROWSER_PROFILE_DEFAULT)
        self.auto_refresh_check.setChecked(True)
        self.verify_ssl_check.setChecked(True)
        self._update_summary(None)
        self._set_actions_enabled(False)
        self._lock = False
        self._apply_form_plan(None)
        self._sync_type_styles()

    def _flush(self, *_args: Any) -> None:
        if self._lock or self.cur is None:
            return
        row = self.rows[self.cur]
        row.name = self.name_edit.text().strip()
        row.base_url = self.base_edit.text().strip()
        row.type = self._current_type()
        action = self._combo_value(self.action_combo, core.CHECKIN_ACTIONS, "api")
        auth = core.effective_auth(action, self._combo_value(self.auth_combo, core.AUTH_METHODS, "cookie"))
        row.checkin_action = action
        row.auth_method = auth
        row.script = self.script_edit.text().strip()
        text = self.script_args_edit.toPlainText().strip() or "{}"
        row.script_args_text = text
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                row.script_args = parsed
        except json.JSONDecodeError:
            pass
        row.script_timeout = accounts_store.parse_script_timeout(self.script_timeout_edit.text().strip())
        row.api_variant = self._combo_value(self.variant_combo, core.API_VARIANTS, "auto")
        row.verification_mode = self._combo_value(
            self.verification_combo, core.VERIFICATION_MODES, "auto"
        )
        row.oauth_provider = self._combo_value(self.oauth_provider_combo, core.OAUTH_PROVIDERS, "linuxdo")
        row.oauth_account = self._current_oauth_account()
        fallback_provider, fallback_account = self._current_oauth_fallback()
        if core.can_optional_oauth(row.type, action, auth):
            row.oauth_fallback_provider = fallback_provider
            row.oauth_fallback_account = fallback_account if fallback_provider else ""
        else:
            row.oauth_fallback_provider = ""
            row.oauth_fallback_account = ""
        row.user_id = self.uid_edit.text().strip()
        row.access_token = self.token_edit.text().strip()
        row.refresh_token = self.refresh_edit.text().strip()
        row.cookie = self.cookie_edit.toPlainText().strip()
        row.browser_state = (
            self.state_edit.toPlainText().strip() if auth == "browser" and action != "relogin" else ""
        )
        row.proxy = self.proxy_edit.text().strip()
        row.cookie_file = self.cookie_file_edit.text().strip()
        # 空输入回落默认值：这两项在 CLI 侧有明确默认（/profile、.browser_profile），
        # 存空串会让 SiteConfig 拿到空值而不是默认值。
        row.referer_path = self.referer_edit.text().strip() or core.REFERER_PATH_DEFAULT
        row.browser_profile = self.browser_profile_edit.text().strip() or core.BROWSER_PROFILE_DEFAULT
        row.auto_refresh_cookie = self.auto_refresh_check.isChecked()
        row.verify_ssl = self.verify_ssl_check.isChecked()
        self._update_summary(row)
        self._refresh_row(self.cur)
        self._schedule_dirty()

    # ── 组合控件辅助 ──
    @staticmethod
    def _combo_value(combo, valid: tuple[str, ...], default: str) -> str:
        data = combo.currentData()
        return data if data in valid else default

    def _set_combo_value(self, combo, value: str, default: str) -> None:
        idx = combo.findData(value)
        if idx < 0:
            idx = combo.findData(default)
        combo.blockSignals(True)
        combo.setCurrentIndex(max(idx, 0))
        combo.blockSignals(False)

    def _current_type(self) -> str:
        for t, btn in self._type_buttons.items():
            if btn.isChecked():
                return t
        return "newapi"

    def _set_type(self, t: str) -> None:
        if self._lock:
            return
        self._set_type_value(t)
        self._sync_type_styles()
        self._flush()
        if self.cur is not None:
            self._apply_form_plan(self.rows[self.cur])

    def _set_type_value(self, t: str) -> None:
        if t not in core.TYPES:
            t = "newapi"
        for tt, btn in self._type_buttons.items():
            btn.setChecked(tt == t)

    def _sync_type_styles(self) -> None:
        t = self._current_type()
        for tt, btn in self._type_buttons.items():
            btn.setProperty("active", tt == t)
            w.repolish(btn)

    def _current_oauth_account(self) -> str:
        text = self.oauth_account_combo.currentText().strip()
        idx = self.oauth_account_combo.currentIndex()
        current_label = self.oauth_account_combo.itemText(idx).strip() if idx >= 0 else ""
        data = self.oauth_account_combo.currentData()
        if data and (not text or text == current_label):
            return accounts_store.normalize_oauth_account(data)
        return accounts_store.normalize_oauth_account(text or data)

    def _current_oauth_fallback(self) -> tuple[str, str]:
        data = str(self.oauth_fallback_combo.currentData() or "")
        if ":" not in data:
            return "", ""
        provider, account = data.split(":", 1)
        provider = accounts_store.normalize_oauth_provider(provider)
        if not provider:
            return "", ""
        return provider, accounts_store.normalize_oauth_account(account)

    def _refresh_oauth_account_choices(self, selected: str | None = None) -> None:
        selected_key = accounts_store.normalize_oauth_account(selected or self._current_oauth_account())
        provider = self._combo_value(self.oauth_provider_combo, core.OAUTH_PROVIDERS, "linuxdo")
        accounts = dict(((self.oauth_states.get(provider) or {}).get("accounts") or {}))
        names = sorted(accounts)
        if core.DEFAULT_OAUTH_ACCOUNT in names:
            names.remove(core.DEFAULT_OAUTH_ACCOUNT)
            names.insert(0, core.DEFAULT_OAUTH_ACCOUNT)
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

    def _refresh_oauth_fallback_choices(self, selected_provider: str = "", selected_account: str = "") -> None:
        selected_provider = accounts_store.normalize_oauth_provider(selected_provider)
        selected_account = accounts_store.normalize_oauth_account(selected_account) if selected_provider else ""
        selected_data = f"{selected_provider}:{selected_account}" if selected_provider else ""
        self.oauth_fallback_combo.blockSignals(True)
        self.oauth_fallback_combo.clear()
        self.oauth_fallback_combo.addItem("不使用", "")
        saved_count = 0
        for provider in core.OAUTH_PROVIDERS:
            accounts = dict(((self.oauth_states.get(provider) or {}).get("accounts") or {}))
            names = sorted(accounts)
            if core.DEFAULT_OAUTH_ACCOUNT in names:
                names.remove(core.DEFAULT_OAUTH_ACCOUNT)
                names.insert(0, core.DEFAULT_OAUTH_ACCOUNT)
            for account in names:
                entry = accounts.get(account) or {}
                username = str(entry.get("username") or "").strip()
                label = f"{core.OAUTH_PROVIDER_LABELS.get(provider, provider)} / {account}"
                if username and username != account:
                    label += f" · {username}"
                self.oauth_fallback_combo.addItem(label, f"{provider}:{account}")
                saved_count += 1
        if not saved_count:
            self.oauth_fallback_combo.addItem("暂无共享 OAuth 登录态（请先捕获）", "")
        idx = self.oauth_fallback_combo.findData(selected_data)
        self.oauth_fallback_combo.setCurrentIndex(max(idx, 0))
        self.oauth_fallback_combo.blockSignals(False)

    def _reload_oauth_accounts(self) -> None:
        selected = self._current_oauth_account()
        try:
            self.oauth_states = accounts_store.load_oauth_states()
        except Exception as exc:
            core.bg_log("ERROR", "刷新 OAuth 账号列表失败", error=exc)
            QMessageBox.critical(self, "刷新 OAuth 账号失败", mask_secrets(str(exc)))
            return
        fallback_provider, fallback_account = self._current_oauth_fallback()
        self._refresh_oauth_account_choices(selected)
        self._refresh_oauth_fallback_choices(fallback_provider, fallback_account)
        self._flush()
        if self.cur is not None:
            self._apply_form_plan(self.rows[self.cur])
        self._say("已刷新 OAuth 账号列表")

    def _on_oauth_provider_changed(self, *_args: Any) -> None:
        self._refresh_oauth_account_choices(core.DEFAULT_OAUTH_ACCOUNT)
        self._on_combo_changed()

    def _on_oauth_account_changed(self, *_args: Any) -> None:
        # 下拉选择 OAuth 账号时优先保留 item data，避免 editable combo 读到旧文本。
        idx = self.oauth_account_combo.currentIndex()
        line = self.oauth_account_combo.lineEdit()
        if idx >= 0 and line is not None:
            label = self.oauth_account_combo.itemText(idx)
            if line.text() != label:
                line.blockSignals(True)
                line.setText(label)
                line.blockSignals(False)
        self._on_combo_changed()

    def _on_combo_changed(self, *_args: Any) -> None:
        if self._lock:
            return
        action = self._combo_value(self.action_combo, core.CHECKIN_ACTIONS, "api")
        auth = self._combo_value(self.auth_combo, core.AUTH_METHODS, "cookie")
        coerced = core.effective_auth(action, auth)
        if coerced != auth:
            self._set_combo_value(self.auth_combo, coerced, "oauth")
        self._flush()
        if self.cur is not None:
            self._apply_form_plan(self.rows[self.cur])

    # ── 表单联动（声明式）──
    def _apply_form_plan(self, row: core.SiteRow | None) -> None:
        plan = core.build_form_plan(row, self.oauth_states) if row is not None else core.FormPlan()
        self.variant_wrap.setVisible(plan.show_variant)
        self.verification_wrap.setVisible(plan.show_verification)
        self.script_wrap.setVisible(plan.show_script)
        self.script_args_wrap.setVisible(plan.show_script_args)
        self.script_timeout_wrap.setVisible(plan.show_script_timeout)
        self._set_field_hint(self.script_wrap, plan.script_hint)
        self.script_edit.setPlaceholderText(plan.script_placeholder)
        self.oauth_provider_wrap.setVisible(plan.show_oauth)
        self.oauth_account_wrap.setVisible(plan.show_oauth)
        self.oauth_fallback_wrap.setVisible(plan.show_fallback)
        self.uid_edit.setEnabled(plan.creds_enabled)
        self.cookie_edit.setEnabled(plan.creds_enabled)
        # token / refresh_token 走 token_enabled：它们是接口凭据，sub2api 即使用
        # browser/oauth 登录方式也会先走纯 API，禁用会让手工粘贴的有效值无法保存。
        self.token_edit.setEnabled(plan.token_enabled)
        self.refresh_wrap.setVisible(plan.show_refresh_input)
        self.refresh_edit.setEnabled(plan.token_enabled)
        self.browser_profile_wrap.setVisible(plan.show_browser_profile)
        self.state_wrap.setVisible(plan.show_state_box)
        self.state_edit.setVisible(plan.state_editable)
        self.state_edit.setEnabled(plan.state_editable)
        self.oauth_state_status.setVisible(plan.show_oauth_status)
        self.refresh_token_hint.setVisible(plan.show_refresh_status)
        self.refresh_token_hint.setText(plan.refresh_status)
        self.browser_ops.setVisible(plan.show_browser_ops)
        self.btn_oauth_delete.setVisible(plan.show_delete_oauth)
        self.btn_capture.setText(plan.capture_text)
        self.btn_verify.setText(plan.verify_text)
        self.oauth_state_status.setText(plan.oauth_status)
        self.mode_hint.setText(plan.mode_hint)

    # ── 汇总卡 ──
    def _update_summary(self, row: core.SiteRow | None = None) -> None:
        if row is None and self.cur is not None and 0 <= self.cur < len(self.rows):
            row = self.rows[self.cur]
        if not row:
            self.edit_title.setText("未选择站点")
            self.summary_url.setText("从左侧选择一个站点，或点击新增开始配置。")
            self.summary_badge.setText("—")
            tokens = theme.tokens(self._theme)
            self.summary_badge.setStyleSheet(w.badge_style(tokens["mute"], tokens["surface_alt"]))
            self.summary_state.setText("未选择")
            self.summary_state.setProperty("state", "idle")
            w.repolish(self.summary_state)
            self._render_summary_status(None)
            return
        self.edit_title.setText(row.name or "（未命名）")
        self.summary_url.setText(row.base_url or "尚未填写站点地址")
        self.summary_badge.setText(core.TYPE_LABELS.get(row.type, row.type))
        self.summary_badge.setStyleSheet(w.type_badge_style(row.type))
        self.summary_state.setText("自动签到已启用" if row.enabled else "自动签到已关闭")
        self.summary_state.setProperty("state", "on" if row.enabled else "off")
        w.repolish(self.summary_state)
        self._render_summary_status(self.store.get(core.StatusStore.status_key(row)))

    def _render_summary_status(self, status: dict[str, Any] | None) -> None:
        has_site = self.cur is not None
        running = has_site and self._leases.is_channel_running(
            core.StatusStore.task_key(self.rows[self.cur])
        )
        self.btn_refresh.setEnabled(has_site and not running)
        self.btn_checkin_now.setEnabled(has_site and not running)
        checked_in_now = False
        if running:
            self.quota_value.setText("…")
            self.checkin_pill.setText("任务运行中")
            self.checkin_pill.setProperty("kind", "running")
        elif not status:
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
                self.quota_value.setText(core.format_usd(quota))
                self.quota_value.setToolTip(
                    ("上次签到缓存（点🔄实时刷新）" if cached else "实时查询结果") + (f"\n{message}" if message else "")
                )
            elif failed and status.get("last_quota_usd") is not None:
                # 登录失效等失败：仍展示失效前的最后额度，比清空更有参考价值。
                last = status.get("last_quota_usd")
                self.quota_value.setText(f"{core.format_usd(last)} ⚠")
                self.quota_value.setToolTip(
                    "这是失效前的最后额度，非当前实时值。\n"
                    + core.failure_toast(str(status.get("status") or "error"), message)
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
                self.checkin_pill.setText(core.failure_label(str(status.get("status") or "error")))
                self.checkin_pill.setProperty("kind", "fail")
            else:
                self.checkin_pill.setText("—")
                self.checkin_pill.setProperty("kind", "unknown")
            self.checkin_pill.setToolTip(
                core.failure_toast(str(status.get("status") or "error"), message) if failed else message
            )
            if cached and not failed:
                self.checkin_pill.setText(self.checkin_pill.text() + " (缓存)")
        w.repolish(self.checkin_pill)
        if checked_in_now:
            self.btn_checkin_now.setText("重新签到")
            self.btn_checkin_now.setProperty("kind", "tool")
        else:
            self.btn_checkin_now.setText("立即签到")
            self.btn_checkin_now.setProperty("kind", "primary")
        w.repolish(self.btn_checkin_now)

    def _update_overview(self) -> None:
        stats = core.summarize(self.rows, self.store)
        self.chip_sites.set_value(f"{stats.enabled}/{stats.total}")
        self.chip_done.set_value(str(stats.done))
        self.chip_quota.set_value(core.format_usd(stats.quota_sum) if stats.quota_known else "—")
        self.chip_quota.caption.setText(f"已知总额度（{stats.quota_known} 站）" if stats.quota_known else "已知总额度")
        self.chip_failed.set_value(str(stats.failed))

    # ── 站点任务（查询 / 签到）──
    def _row_index(self, row_id: str) -> int | None:
        for index, row in enumerate(self.rows):
            if row.runtime_id == row_id:
                return index
        return None

    def _try_lock(self, idx: int, label: str) -> core.TaskLease | None:
        if idx < 0 or idx >= len(self.rows):
            return None
        lease = self._leases.acquire_single(self.rows[idx])
        if lease is None:
            self._say(f"该站点已有任务运行中，已跳过新的{label}")
            return None
        self._refresh_row(idx)
        if idx == self.cur:
            self._update_summary(self.rows[idx])
        return lease

    def _unlock(self, lease: core.TaskLease | None, row_id: str) -> None:
        self._leases.release(lease)
        idx = self._row_index(row_id)
        if idx is not None:
            self._refresh_row(idx)
            if idx == self.cur:
                self._update_summary(self.rows[idx])

    def _task_params_for_row(self, row: core.SiteRow) -> dict[str, Any]:
        explicit = core.changed_credential_fields(row, self._saved_credentials.get(row.runtime_id))
        return core.task_params(
            row,
            self.oauth_states,
            explicit_credential_fields=explicit,
        )

    def _params_for_current(self) -> dict[str, Any] | None:
        if self.cur is None:
            QMessageBox.warning(self, "提示", "请先选择一个站点。")
            return None
        self._flush()
        row = self.rows[self.cur]
        if not accounts_store.normalize_base_url(row.base_url):
            QMessageBox.warning(self, "提示", "请先填写站点地址。")
            return None
        return self._task_params_for_row(row)

    def _refresh_status(self) -> None:
        params = self._params_for_current()
        if params is None or self.cur is None:
            return
        cur_idx = self.cur
        row_id = self.rows[cur_idx].runtime_id
        key = core.StatusStore.status_key(self.rows[cur_idx])
        lease = self._try_lock(cur_idx, "查询")
        if lease is None:
            return
        self._say("正在查询额度…")

        def on_done(result: dict[str, Any]) -> None:
            try:
                entry = self.store.apply_query(key, result)
                self._schedule_status_save()
                current_idx = self._row_index(row_id)
                if current_idx is not None:
                    self._refresh_row(current_idx)
                    if current_idx == self.cur:
                        self._update_summary(self.rows[current_idx])
                self._update_overview()
                ok = bool(result.get("ok"))
                self._say(entry["message"] if ok else core.failure_toast(entry["status"], str(entry["message"])))
            finally:
                self._unlock(lease, row_id)

        self.runner.submit("query", params, on_done)

    def _checkin_current(self) -> None:
        if self.cur is None:
            QMessageBox.information(self, "提示", "请先选择一个站点。")
            return
        params = self._params_for_current()
        if params is None:
            return
        idx = self.cur
        row_id = self.rows[idx].runtime_id
        key = core.StatusStore.status_key(self.rows[idx])
        lease = self._try_lock(idx, "签到")
        if lease is None:
            return
        name = self.rows[idx].name or "未命名站点"
        self._say(f"「{name}」签到中…")

        def on_done(result: dict[str, Any]) -> None:
            try:
                self.store.apply_checkin(key, result)
                self._schedule_status_save()
                current_idx = self._row_index(row_id)
                if current_idx is not None:
                    self._refresh_row(current_idx)
                    if current_idx == self.cur:
                        self._update_summary(self.rows[current_idx])
                self._update_overview()
                status = result.get("status", "error")
                message = result.get("message", "")
                self._say(
                    f"{name}: {status} - {message}"
                    if result.get("ok")
                    else f"{name}: 签到失败 [{status}] {message}"
                )
            finally:
                self._unlock(lease, row_id)

        self.runner.submit("checkin", params, on_done)

    def _test_checkin(self) -> None:
        """测试签到：与立即签到同一执行路径，但结果弹窗展示详细分类。"""
        params = self._params_for_current()
        if params is None or self.cur is None:
            return
        cur_idx = self.cur
        row_id = self.rows[cur_idx].runtime_id
        key = core.StatusStore.status_key(self.rows[cur_idx])
        lease = self._try_lock(cur_idx, "测试签到")
        if lease is None:
            return
        self._say(f"正在测试签到（{params['site_profile']} / {params['auth_method']} / {params['checkin_action']}）…")

        def on_done(result: dict[str, Any]) -> None:
            try:
                self.store.apply_checkin(key, result)
                self._schedule_status_save()
                current_idx = self._row_index(row_id)
                if current_idx is not None:
                    self._refresh_row(current_idx)
                    if current_idx == self.cur:
                        self._update_summary(self.rows[current_idx])
                self._update_overview()
                status = result.get("status", "error")
                msg = result.get("message", "") or status
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
            finally:
                self._unlock(lease, row_id)

        self.runner.submit("checkin", params, on_done)

    # ── 批量任务 ──
    def _collect_batch(self, label: str) -> list[list[core.TaskSnapshot]]:
        """把启用渠道按站点分组，返回不可变任务快照列表。

        同一 base_url 下可能配了多个账号：组内串行执行（与 CLI 的 site_locks 语义
        一致），组间并发。快照在提交时固定身份，回调不再依赖可变行号——运行中删除、
        排序、改名都不会写错行或泄漏锁。已被其它任务占用的站点整组跳过：站点级资源
        必须独占，否则单签与批量会并发打同一站点。
        """
        if self.cur is not None:
            self._flush()
        snapshots: list[core.TaskSnapshot] = []
        skipped_invalid = 0
        for row in self.rows:
            if not row.enabled:
                continue
            if not accounts_store.normalize_base_url(row.base_url):
                skipped_invalid += 1
                continue
            snapshots.append(
                core.make_task_snapshot(
                    row,
                    self.oauth_states,
                    explicit_credential_fields=core.changed_credential_fields(
                        row, self._saved_credentials.get(row.runtime_id)
                    ),
                )
            )
        groups = serial_groups(snapshots, lambda snapshot: snapshot.site_group_key)
        runnable: list[list[core.TaskSnapshot]] = []
        skipped_running = 0
        for group in groups:
            group_key = group[0].site_group_key
            if self._leases.is_site_running(group_key):
                skipped_running += len(group)
                continue
            runnable.append(group)
        skipped_parts = []
        if skipped_invalid:
            skipped_parts.append(f"{skipped_invalid} 个缺少有效地址的草稿")
        if skipped_running:
            skipped_parts.append(f"{skipped_running} 个运行中的站点任务")
        if skipped_parts:
            self._say("已跳过 " + "、".join(skipped_parts) + "。")
        if not runnable:
            QMessageBox.information(self, "提示", f"没有可{label}的启用站点（可能都在运行中）。")
        return runnable

    def _set_batch_buttons(self) -> None:
        idle = self._batch_active == 0
        self.btn_query_all.setEnabled(idle)
        self.btn_checkin_all.setEnabled(idle)

    def _refresh_snapshot_row(self, snapshot: core.TaskSnapshot) -> None:
        idx = self._row_index(snapshot.row_id)
        if idx is None:
            return
        self._refresh_row(idx)
        if idx == self.cur:
            self._update_summary(self.rows[idx])

    def _run_batch(self, action: str, label: str) -> None:
        groups = self._collect_batch(label)
        if not groups:
            return
        leases: list[core.TaskLease] = []
        active_groups: list[list[core.TaskSnapshot]] = []
        for snapshots in groups:
            lease = self._leases.acquire_group(snapshots)
            if lease is None:
                continue
            leases.append(lease)
            active_groups.append(snapshots)
        if not active_groups:
            QMessageBox.information(self, "提示", f"没有可{label}的启用站点（可能都在运行中）。")
            return

        self._batch_active += 1
        self._set_batch_buttons()
        for snapshots in active_groups:
            for snapshot in snapshots:
                self._refresh_snapshot_row(snapshot)
        self._update_summary()

        total = sum(len(snapshots) for snapshots in active_groups)
        core.bg_log("INFO", f"批量{label}开始", sites=total, groups=len(active_groups))
        self._say(f"批量{label}：共 {total} 个站点…")

        completed = [0]
        failures = [0]

        def finish_batch() -> None:
            self._batch_active = max(0, self._batch_active - 1)
            self._set_batch_buttons()
            self._update_overview()
            summary = f"批量{label}完成：{total - failures[0]}/{total} 成功"
            if failures[0]:
                summary += f"，{failures[0]} 个失败或需处理（详见日志）"
            core.bg_log("INFO", summary)
            self._say(summary)

        def run_group(
            snapshots: list[core.TaskSnapshot],
            lease: core.TaskLease,
            position: int = 0,
        ) -> None:
            """同站渠道依次执行；整组跑完才释放站点租约。"""
            if position >= len(snapshots):
                self._leases.release(lease)
                for snapshot in snapshots:
                    self._refresh_snapshot_row(snapshot)
                if completed[0] >= total:
                    finish_batch()
                return

            snapshot = snapshots[position]

            def on_done(result: dict[str, Any]) -> None:
                try:
                    # 结果归属提交时的身份：即使该行已被删除或改名，也不会写到别的渠道。
                    if action == "query":
                        self.store.apply_query(snapshot.status_key, result)
                    else:
                        self.store.apply_checkin(snapshot.status_key, result)
                    self._schedule_status_save()
                    self._refresh_snapshot_row(snapshot)
                    ok = bool(result.get("ok"))
                    if not ok:
                        failures[0] += 1
                    core.bg_log(
                        "INFO" if ok else "WARN",
                        f"批量{label}结果",
                        site=snapshot.name,
                        status=result.get("status"),
                        result_message=result.get("message"),
                    )
                finally:
                    completed[0] += 1
                    self._say(f"{label}进度：{completed[0]}/{total}")
                    run_group(snapshots, lease, position + 1)

            self.runner.submit(action, snapshot.params, on_done)

        for snapshots, lease in zip(active_groups, leases):
            run_group(snapshots, lease)

    def _query_all(self) -> None:
        self._run_batch("query", "查询")

    def _checkin_all(self) -> None:
        self._run_batch("checkin", "签到")

    # ── CRUD ──
    def _set_actions_enabled(self, on: bool) -> None:
        self.summary_state.setEnabled(on)
        for btn in (self.btn_dup, self.btn_del):
            btn.setEnabled(on)

    def _add(self) -> None:
        dlg = TypeDialog(self, stylesheet=theme.build_qss(self._theme))
        if dlg.exec() != QDialog.Accepted or not dlg.chosen:
            return
        if self.cur is not None:
            self._flush()
        self.rows.append(core.new_row(dlg.chosen))
        self.cur = len(self.rows) - 1
        self.search_edit.clear()
        self._render_list()
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self._select_real(self.cur)
        self._schedule_dirty()

    def _del(self) -> None:
        if self.cur is None:
            return
        name = self.rows[self.cur].name
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
        self._schedule_dirty()

    def _dup(self) -> None:
        if self.cur is None:
            return
        self._flush()
        nw = self.rows[self.cur].copy()
        nw.name += "-副本"
        self.rows.insert(self.cur + 1, nw)
        self.cur += 1
        self.search_edit.clear()
        self._render_list()
        pos = self._visible_pos(self.cur)
        if pos >= 0:
            self.listw.setCurrentRow(pos)
        self._select_real(self.cur)
        self._schedule_dirty()

    # ── 剪贴板 ──
    def _imp(self) -> None:
        data, error = core.parse_clipboard_site(QApplication.clipboard().text())
        if error:
            QMessageBox.warning(self, "导入失败", error)
            return
        assert data is not None
        if self.cur is None:
            self._add()
        if self.cur is None:
            return
        try:
            updated = core.merge_clipboard_site(self.rows[self.cur], data)
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", mask_secrets(str(exc)))
            return
        self.rows[self.cur] = updated
        self._load(self.cur)
        self._refresh_row(self.cur)
        self._update_overview()
        self._schedule_dirty()
        self._say(f"已从剪贴板导入「{updated.name or '?'}」")

    def _cpcred(self) -> None:
        if self.cur is None:
            return
        row = self.rows[self.cur]
        text = core.cred_json(row)
        if text is None:
            QMessageBox.warning(self, "提示", "当前没有填写凭据。")
            return
        QApplication.clipboard().setText(text)
        self._say(f"已复制「{row.name}」的凭据 JSON")

    # ── 后台持久化 / 脏状态 ──
    def _schedule_status_save(self) -> None:
        """合并短时间内的多条任务结果，避免每个回调都同步写盘。"""
        self._status_save_timer.start(150)

    def _persist_status_async(self) -> None:
        payload = self.store.snapshot_payload()
        results_dir = self.store.results_dir
        self.storage.submit(lambda: core.StatusStore.write_payload(results_dir, payload))

    def _schedule_dirty(self) -> None:
        self._dirty_timer.start(250)

    def _compute_dirty(self) -> None:
        self._set_dirty(core.config_snapshot(self.rows, self.oauth_states) != self._saved_snapshot)

    def _set_dirty(self, dirty: bool) -> None:
        if self._dirty == dirty:
            return
        self._dirty = dirty
        self.status.setText("● 未保存" if dirty else "● 已保存")
        self.status.setProperty("state", "dirty" if dirty else "saved")
        w.repolish(self.status)

    def _mark_saved(self) -> None:
        self._dirty_timer.stop()
        self._saved_snapshot = core.config_snapshot(self.rows, self.oauth_states)
        self._saved_credentials = core.credential_snapshots(self.rows)
        self._set_dirty(False)

    # ── 保存 / 导出 ──
    def _save(self) -> None:
        if self._config_load_failed:
            QMessageBox.critical(
                self,
                "保存已阻止",
                "最近一次配置加载失败。为避免用不完整内存状态覆盖原文件，请先修复配置并重新加载成功。",
            )
            return
        if self._save_inflight:
            self._say("配置正在后台保存，请稍候…")
            return
        if self.cur is not None:
            self._flush()
        error = core.validate_rows(self.rows)
        if error:
            QMessageBox.critical(self, "校验失败", error)
            return

        request = config_store.build_save_request(self.rows, self.oauth_states, self._saved_credentials)
        self._save_inflight = True
        self.save_btn.setEnabled(False)
        self._say("正在后台保存配置…")

        def on_saved(_result: object, save_error: BaseException | None) -> None:
            self._save_inflight = False
            self.save_btn.setEnabled(True)
            if save_error is not None:
                core.bg_log("ERROR", "保存账号配置失败", error=save_error)
                QMessageBox.critical(self, "保存失败", mask_secrets(str(save_error)))
                self._say("保存失败，当前更改仍未保存")
                return

            # 只把实际写入的冻结快照设为基线；保存期间继续编辑的内容仍保持“未保存”。
            self._saved_snapshot = request.snapshot
            self._saved_credentials = request.credentials
            if self._dirty_timer.isActive():
                self._dirty_timer.stop()
            self._compute_dirty()
            suffix = "；保存期间的新更改尚未保存" if self._dirty else ""
            self._say(f"已保存：{len(request.accounts)} 个账号配置{suffix}")

        self.storage.submit(request.persist, on_saved)

    def _export(self) -> None:
        if self.cur is not None:
            self._flush()
        error = core.validate_export(self.rows)
        if error:
            QMessageBox.critical(self, "导出校验失败", error)
            return
        payload = accounts_store.build_github_secret_payload(
            [row.to_legacy() for row in self.rows], self.oauth_states
        )
        exported = payload.get("accounts") if isinstance(payload.get("accounts"), list) else []
        if not exported:
            QMessageBox.warning(self, "无可导出站点", "没有启用的有效站点可导出到 GitHub Secret。")
            return
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False, indent=2))
        disabled_count = sum(1 for row in self.rows if not row.enabled)
        oauth_count = 0
        oauth_states = payload.get("oauth_states") if isinstance(payload.get("oauth_states"), dict) else {}
        for data in oauth_states.values():
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                oauth_count += len(data["accounts"])
        QMessageBox.information(
            self,
            "已复制",
            "已复制最小化 GitHub Secret：ACCOUNTS。\n\n"
            f"启用站点：{len(exported)} 个\n"
            f"已剔除禁用站点：{disabled_count} 个\n"
            f"保留 OAuth 登录态：{oauth_count} 个",
        )
        self._say(f"已复制 Secret：{len(exported)} 个启用站点，{oauth_count} 个 OAuth 登录态")

    # ── 浏览器 / OAuth 登录态操作 ──
    def _browser_busy(self) -> bool:
        return self._worker is not None and self._worker.isRunning()

    def _set_browser_buttons(self, enabled: bool) -> None:
        for btn in (self.btn_capture, self.btn_verify, self.btn_test, self.btn_refresh, self.btn_checkin_now):
            btn.setEnabled(enabled)

    def _start_worker(self, action: str, params: dict[str, Any]) -> BrowserWorker:
        self._set_browser_buttons(False)
        self._reap_retired_worker()
        worker = BrowserWorker(action, params, self)
        self._worker = worker
        worker.progress.connect(self._say)
        worker.failed.connect(self._on_browser_failed)
        worker.finished.connect(lambda current=worker: self._on_browser_finished(current))
        return worker

    def _reap_retired_worker(self) -> None:
        """回收上一个已结束的 worker；此时线程一定已退出，删除是安全的。"""
        worker = self._retired_worker
        self._retired_worker = None
        if worker is None:
            return
        try:
            if worker.isRunning():
                worker.wait(3000)
            worker.deleteLater()
        except RuntimeError:
            pass

    def _on_browser_finished(self, worker: BrowserWorker) -> None:
        self._set_browser_buttons(True)
        if self._worker is worker:
            self._worker = None
        # 只登记待回收，实际删除推迟到下次启动或窗口关闭，避免销毁未退出的线程。
        self._retired_worker = worker

    def _on_browser_failed(self, msg: str) -> None:
        self._set_browser_buttons(True)
        action = getattr(self._worker, "action", "")
        if action == "capture":
            dlg = self._capture_dialog
            if dlg is not None and dlg.isVisible():
                dlg.done(0)
        title = {"capture": "登录态捕获失败", "verify": "登录态检测失败"}.get(action, "后台操作失败")
        QMessageBox.critical(self, title, f"{msg}\n\n详细日志可点击底部「日志」查看。")
        self._say(f"{title}：{msg}")

    def _browser_capture(self) -> None:
        if self._browser_busy():
            QMessageBox.information(self, "请稍候", "已有浏览器操作进行中。")
            return
        params = self._params_for_current()
        if params is None or self.cur is None:
            return
        cur_idx = self.cur
        row_id = self.rows[cur_idx].runtime_id
        lease = self._try_lock(cur_idx, "浏览器操作")
        if lease is None:
            return
        is_oauth = params.get("auth_method") == "oauth"
        provider_label = core.OAUTH_PROVIDER_LABELS.get(params.get("oauth_provider"), params.get("oauth_provider", ""))
        account = params.get("oauth_account", core.DEFAULT_OAUTH_ACCOUNT)
        self._say("正在打开浏览器，请在其中完成第三方登录…" if is_oauth else "正在打开浏览器，请完成站点登录…")
        worker = self._start_worker("capture", params)
        worker.finished.connect(lambda: self._unlock(lease, row_id))

        dlg = QMessageBox(self)
        dlg.setWindowTitle("OAuth 登录态捕获" if is_oauth else "浏览器登录捕获")
        if is_oauth:
            dlg.setText(
                f"已打开浏览器窗口。\n\n请在其中登录 {provider_label}（账号：{account}）。"
                "检测到有效登录态后窗口会自动关闭；若需提前检查，可点击下方按钮。"
            )
        else:
            dlg.setText("已打开浏览器窗口。\n\n请在其中完成登录并回到站点控制台，然后点下方「我已完成登录」。")
        done_btn = dlg.addButton("手动检查登录态" if is_oauth else "我已完成登录", QMessageBox.AcceptRole)
        dlg.setStandardButtons(QMessageBox.NoButton)
        dlg.setIcon(QMessageBox.Information)
        self._capture_dialog = dlg

        def on_capture_done(result: dict[str, Any]) -> None:
            if dlg.isVisible():
                dlg.done(0)
            if result.get("ok") and result.get("state"):
                if params.get("auth_method") == "oauth":
                    provider = result.get("provider") or params.get("oauth_provider") or "linuxdo"
                    account_key = accounts_store.normalize_oauth_account(params.get("oauth_account"))
                    try:
                        provider = accounts_store.normalize_oauth_provider(provider)
                        if not provider:
                            raise ValueError("未知 OAuth 提供商")
                        bucket = self.oauth_states.setdefault(provider, {"accounts": {}})
                        bucket.setdefault("accounts", {})[account_key] = {
                            "state": str(result["state"] or "").strip(),
                            "username": str(result.get("username") or ""),
                            "updated_at": time_utils.utc_iso(),
                        }
                        self.oauth_states = accounts_store.normalize_oauth_states(self.oauth_states)
                    except Exception as exc:
                        core.bg_log("ERROR", "暂存 OAuth 登录态失败", oauth_provider=provider, oauth_account=account_key, error=exc)
                        QMessageBox.critical(self, "暂存 OAuth 登录态失败", mask_secrets(str(exc)))
                        return
                    core.bg_log(
                        "INFO",
                        "暂存 OAuth 登录态",
                        oauth_provider=provider,
                        oauth_account=account_key,
                        username=result.get("username", ""),
                        state_chars=len(str(result.get("state") or "")),
                    )
                    self._refresh_oauth_account_choices(account_key)
                    fb_provider, fb_account = self._current_oauth_fallback()
                    self._refresh_oauth_fallback_choices(fb_provider, fb_account)
                    if self.cur is not None:
                        self._apply_form_plan(self.rows[self.cur])
                    self._schedule_dirty()
                    QMessageBox.information(
                        self,
                        "捕获成功",
                        f"{result.get('message', 'OAuth 登录态已捕获。')}\n\n登录态已加入当前内存配置，请点击“保存全部”写入文件。",
                    )
                    self._say(f"已暂存 {provider}:{account_key} 登录态，请点“保存全部”")
                else:
                    target_idx = self._row_index(row_id)
                    if target_idx is None:
                        QMessageBox.warning(self, "捕获完成", "原渠道已被删除，捕获结果未写入任何账号。")
                        self._say("捕获完成，但原渠道已删除，结果已丢弃")
                        return
                    # 若目标仍是当前编辑行，先保存其它表单改动；随后直接更新目标模型，
                    # 不能再依赖“当前选中行”的输入框，否则切换列表后会写错渠道。
                    if target_idx == self.cur:
                        self._flush()
                    target = self.rows[target_idx]
                    target.browser_state = str(result["state"] or "").strip()
                    access_token = str(result.get("access_token") or "").strip()
                    refresh_token = str(result.get("refresh_token") or "").strip()
                    if access_token:
                        target.access_token = access_token
                    if refresh_token:
                        target.refresh_token = refresh_token
                    if target_idx == self.cur:
                        self._load(target_idx)
                    self._refresh_row(target_idx)
                    self._schedule_dirty()
                    QMessageBox.information(
                        self, "捕获成功", result.get("message", "登录态已捕获并填入目标渠道，记得点「保存全部」。")
                    )
                    self._say(f"已把登录态填入「{target.name}」，请点「保存全部」")
            else:
                QMessageBox.warning(self, "未捕获到有效登录态", result.get("message", "请重试。"))

        worker.finished_ok.connect(on_capture_done)
        worker.start()

        dlg.exec()
        # 无论自动完成、手动检查还是 Esc/X 关闭，都通知 worker 收尾，
        # 否则 capture 的等待循环会空转最长 600s，期间按钮禁用、站点任务锁不释放。
        if dlg.clickedButton() is done_btn:
            self._say("正在读取并打包登录态…")
        else:
            self._say("已关闭登录窗口，正在收尾…")
        try:
            if worker.isRunning():
                worker.request_close()
        except RuntimeError:
            # 极端情况下 QThread 的 C++ 对象已被回收；此时无需再请求收尾。
            pass
        if self._capture_dialog is dlg:
            self._capture_dialog = None

    def _delete_oauth_account(self) -> None:
        provider = self._combo_value(self.oauth_provider_combo, core.OAUTH_PROVIDERS, "linuxdo")
        account = self._current_oauth_account()
        entry = core.oauth_state_entry(self.oauth_states, provider, account)
        if not entry.get("state"):
            QMessageBox.information(self, "提示", f"{provider}:{account} 尚未保存登录态。")
            return
        ret = QMessageBox.question(
            self,
            "删除 OAuth 登录态",
            f"从当前配置删除 {provider}:{account} 的 OAuth 登录态？\n\n站点配置会保留账号名；点击“保存全部”后才会写入文件。",
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
        core.bg_log("INFO", "暂存删除 OAuth 登录态", oauth_provider=provider, oauth_account=account)
        fb_provider, fb_account = self._current_oauth_fallback()
        self._refresh_oauth_account_choices(account)
        self._refresh_oauth_fallback_choices(fb_provider, fb_account)
        if self.cur is not None:
            self._apply_form_plan(self.rows[self.cur])
        self._schedule_dirty()
        self._say(f"已从当前配置删除 {provider}:{account} 登录态，请点“保存全部”")

    def _browser_verify(self) -> None:
        if self._browser_busy():
            QMessageBox.information(self, "请稍候", "已有浏览器操作进行中。")
            return
        params = self._params_for_current()
        if params is None or self.cur is None:
            return
        cur_idx = self.cur
        row_id = self.rows[cur_idx].runtime_id
        lease = self._try_lock(cur_idx, "检测")
        if lease is None:
            return
        try:
            if params.get("auth_method") == "oauth":
                provider = params.get("oauth_provider", "linuxdo")
                account = params.get("oauth_account", core.DEFAULT_OAUTH_ACCOUNT)
                state_text = params.get("browser_state", "")
                if not state_text:
                    core.bg_log("WARN", "OAuth 登录态缺失", oauth_provider=provider, oauth_account=account)
                    QMessageBox.warning(self, "OAuth 登录态缺失", f"尚未保存 {provider}:{account} 登录态，请先捕获。")
                    self._say(f"缺少 {provider}:{account} 登录态")
                    return
                guessed = accounts_store.guess_oauth_provider(state_text)
                if guessed and guessed != provider:
                    core.bg_log(
                        "WARN", "OAuth 登录态不匹配", oauth_provider=provider, oauth_account=account, guessed_provider=guessed
                    )
                    QMessageBox.warning(self, "OAuth 登录态不匹配", f"当前选择 {provider}，但登录态看起来属于 {guessed}。")
                    return
                if params.get("site_profile") != "sub2api":
                    QMessageBox.information(
                        self, "OAuth 登录态存在", f"已保存 {provider}:{account} 登录态（{len(state_text)} 字符）。"
                    )
                    self._say(f"{provider}:{account} 登录态存在")
                    return
            if not params["browser_state"]:
                self._say("未填登录态，将尝试用本地浏览器登录态检测…")
            worker = self._start_worker("verify", params)
            worker.finished.connect(lambda locked=lease: self._unlock(locked, row_id))
            worker.finished_ok.connect(
                lambda r: (
                    QMessageBox.information(self, "登录态有效", r.get("message", ""))
                    if r.get("ok")
                    else QMessageBox.warning(self, "登录态无效", r.get("message", "请重新捕获登录态。"))
                )
            )
            worker.start()
            lease = None
        finally:
            if lease is not None:
                self._unlock(lease, row_id)

    # ── 其它 ──
    def _say(self, text: str) -> None:
        self.toast.show(text)

    def _shutdown_workers(self) -> None:
        """关闭前尽力停止后台任务，避免残留线程 / Playwright 子进程阻塞退出。"""
        worker = self._worker
        if worker is not None:
            try:
                worker.request_close()
            except Exception:
                pass
            try:
                if worker.isRunning():
                    worker.wait(3000)
            except Exception:
                pass
        try:
            self._reap_retired_worker()
        except Exception:
            pass
        try:
            self.runner.shutdown(5000)
        except Exception:
            pass
        try:
            if self._status_save_timer.isActive():
                self._status_save_timer.stop()
                self._persist_status_async()
            self.storage.shutdown(5000)
        except Exception:
            pass
        try:
            self.toast.stop()
        except Exception:
            pass
        core.remove_log_sink(self._log_bridge.line.emit)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        running_channels = self._leases.running_channels
        if running_channels:
            ret = QMessageBox.warning(
                self,
                "任务进行中",
                f"有 {running_channels} 个站点任务正在运行，强制退出可能导致任务失败。\n\n确定要退出吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if ret == QMessageBox.No:
                event.ignore()
                return
        # 关闭确认前先结清防抖中的脏检查，避免误判
        if self._dirty_timer.isActive():
            self._dirty_timer.stop()
            self._compute_dirty()
        if self._dirty:
            ret = QMessageBox.question(
                self, "未保存", "有未保存的更改，确定退出？", QMessageBox.Yes | QMessageBox.No, QMessageBox.No
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return
        theme.save_pref("geometry", self.saveGeometry())
        self._shutdown_workers()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("中转站控制台")
    app.setFont(QFont(theme.FONT_FAMILY, 10))
    win = App()
    win.show()
    return app.exec()
