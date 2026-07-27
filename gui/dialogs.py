# -*- coding: utf-8 -*-
"""对话框：新增站点选型。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from . import widgets

_TYPE_OPTIONS = [
    ("newapi", "New API", "Cookie / Access Token · 可选 OAuth 重登/访问保活"),
    ("sub2api", "Sub2API", "Bearer Token（localStorage auth_token）"),
]


class TypeDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, stylesheet: str = ""):
        super().__init__(parent)
        self.chosen: str | None = None
        self.setWindowTitle("新增站点")
        self.setModal(True)
        self.setFixedSize(430, 380)
        if stylesheet:
            self.setStyleSheet(stylesheet)
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

        for site_type, title_text, desc_text in _TYPE_OPTIONS:
            root.addWidget(self._option(site_type, title_text, desc_text))

        root.addItem(QSpacerItem(1, 1, QSizePolicy.Minimum, QSizePolicy.Expanding))

    def _option(self, site_type: str, title_text: str, desc_text: str) -> QPushButton:
        btn = QPushButton()
        btn.setObjectName("typeOption")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setMinimumHeight(64)
        layout = QHBoxLayout(btn)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(13)

        badge = QLabel(title_text)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(widgets.type_badge_style(site_type))
        layout.addWidget(badge)

        col = QVBoxLayout()
        col.setSpacing(3)
        name = QLabel(title_text)
        name.setObjectName("optionTitle")
        sub = QLabel(desc_text)
        sub.setObjectName("optionDesc")
        col.addWidget(name)
        col.addWidget(sub)
        layout.addLayout(col, 1)

        btn.clicked.connect(lambda: self._pick(site_type))
        return btn

    def _pick(self, site_type: str) -> None:
        self.chosen = site_type
        self.accept()
