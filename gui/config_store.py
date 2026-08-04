#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 配置的事务式加载与不可变保存请求。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import accounts_store

from . import core


@dataclass(frozen=True)
class LoadedConfiguration:
    """一次完整读取的配置；只有两部分都成功后才交给主窗口替换。"""

    rows: list[core.SiteRow] = field(repr=False)
    oauth_states: dict[str, dict[str, Any]] = field(repr=False)


def load_configuration() -> LoadedConfiguration:
    """先在临时对象中完整读取，任何异常都不污染 GUI 当前内存状态。"""
    rows = core.load_rows()
    oauth_states = accounts_store.load_oauth_states()
    return LoadedConfiguration(rows=rows, oauth_states=oauth_states)


@dataclass(frozen=True)
class SaveRequest:
    """提交给单线程存储队列的不可变配置快照。"""

    accounts: list[dict[str, Any]] = field(repr=False)
    oauth_states: dict[str, Any] = field(repr=False)
    rows: list[core.SiteRow] = field(repr=False)
    previous_credentials: dict[str, dict[str, str]] = field(repr=False)
    snapshot: str
    credentials: dict[str, dict[str, str]] = field(repr=False)

    def persist(self) -> int:
        """原子保存账号文档，再标记与旧基线相比发生变化的缓存凭据。"""
        accounts_store.save_accounts(self.accounts, oauth_states=self.oauth_states)
        return core.apply_credential_cache_changes(self.rows, self.previous_credentials)


def build_save_request(
    rows: list[core.SiteRow],
    oauth_states: dict[str, Any],
    previous_credentials: dict[str, dict[str, str]],
) -> SaveRequest:
    """在主线程冻结所有数据；后台线程不再读取可变表单模型。"""
    frozen_rows = deepcopy(rows)
    frozen_oauth = deepcopy(oauth_states)
    return SaveRequest(
        accounts=core.persist_accounts(frozen_rows),
        oauth_states=frozen_oauth,
        rows=frozen_rows,
        previous_credentials=deepcopy(previous_credentials),
        snapshot=core.config_snapshot(frozen_rows, frozen_oauth),
        credentials=core.credential_snapshots(frozen_rows),
    )


__all__ = ["LoadedConfiguration", "SaveRequest", "build_save_request", "load_configuration"]
