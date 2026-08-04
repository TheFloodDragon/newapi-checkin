#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GUI 状态缓存及其并发安全持久化。"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any

import accounts_store
import time_utils

if TYPE_CHECKING:
    from .core import SiteRow


def _is_newer(candidate: Any, existing: Any) -> bool:
    """按真实时间比较两个时间戳，兼容不同时区表示。"""
    left = time_utils.parse_timestamp(candidate)
    right = time_utils.parse_timestamp(existing)
    if left is None:
        return False
    if right is None:
        return True
    return left > right


def _detail_quota_usd(detail: Any) -> float | None:
    from providers.base import detail_quota_usd

    return detail_quota_usd(detail)


class StatusStore:
    """站点状态缓存；可关闭自动写盘，由 GUI 存储队列异步持久化。"""

    def __init__(self, results_dir: Path | None = None, *, autosave: bool = True):
        self.results_dir = results_dir or accounts_store.RESULTS_DIR
        self.autosave = autosave
        self.entries: dict[str, dict[str, Any]] = {}
        self.today = time_utils.business_date()

    @staticmethod
    def status_key(row: SiteRow) -> str:
        base = accounts_store.normalize_base_url(row.base_url)
        return f"{base}|{(row.name or '').strip()}"

    @staticmethod
    def task_key(row: SiteRow) -> str:
        """渠道级互斥键：同站点的不同账号仍可作为独立任务。"""
        base = accounts_store.normalize_base_url(row.base_url)
        name = (row.name or "").strip()
        if base and name:
            return f"{base}|{name}"
        return base or name

    @staticmethod
    def site_group_key(row: SiteRow) -> str:
        """站点级批量分组键：同址账号串行、不同站点并发。"""
        return accounts_store.normalize_base_url(row.base_url) or (row.name or "").strip()

    def _ensure_today(self) -> None:
        current = time_utils.business_date()
        if current != self.today:
            self.load()

    def get(self, key: str) -> dict[str, Any] | None:
        self._ensure_today()
        return self.entries.get(key)

    def load(self) -> None:
        """事务式重载当天的批量结果与 GUI 缓存。"""
        previous_entries = self.entries
        previous_today = self.today
        self.entries = {}
        self.today = time_utils.business_date()
        try:
            self._load_batch_results()
            self._merge_gui_cache()
        except Exception:
            self.entries = previous_entries
            self.today = previous_today
            raise

    def _is_today(self, entry: dict[str, Any]) -> bool:
        stamp = str(entry.get("business_date") or "").strip()
        if stamp:
            return stamp == self.today
        return time_utils.business_date_of(entry.get("saved_at")) == self.today

    def _load_batch_results(self) -> None:
        path = self.results_dir / "checkin_result.json"
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            rows = payload.get("results", []) if isinstance(payload, dict) else []
            saved_at = str(payload.get("generated_at") or "") if isinstance(payload, dict) else ""
            business_day = str(payload.get("business_date") or "") if isinstance(payload, dict) else ""
        except Exception:
            return
        if not self._is_today({"saved_at": saved_at, "business_date": business_day}):
            return
        for item in rows:
            if not isinstance(item, dict):
                continue
            base = accounts_store.normalize_base_url(str(item.get("base_url") or ""))
            name = str(item.get("site") or "")
            if not base and not name:
                continue
            quota_usd = None
            current_quota = str(item.get("current_quota") or "").lstrip("$")
            try:
                quota_usd = float(current_quota) if current_quota else None
            except ValueError:
                quota_usd = None
            status = str(item.get("status") or "")
            ok = status in ("success", "already_done")
            self.entries[f"{base}|{name}"] = {
                "quota_usd": quota_usd if ok else None,
                "last_quota_usd": quota_usd,
                "checked_in": True if ok else None,
                "ok": ok,
                "status": status or ("success" if ok else "error"),
                "message": item.get("note") or item.get("message") or "",
                "cached": True,
                "saved_at": saved_at,
                "business_date": business_day or time_utils.business_date_of(saved_at),
            }

    def _merge_gui_cache(self) -> None:
        path = self.results_dir / "gui_status_cache.json"
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
            if not isinstance(entry, dict) or not self._is_today(entry):
                continue
            existing = self.entries.get(key)
            if existing is not None and not _is_newer(entry.get("saved_at"), existing.get("saved_at")):
                continue
            self.entries[key] = {
                "quota_usd": entry.get("quota_usd"),
                "last_quota_usd": (
                    entry.get("last_quota_usd") if entry.get("last_quota_usd") is not None else entry.get("quota_usd")
                ),
                "checked_in": entry.get("checked_in"),
                "ok": bool(entry.get("ok")),
                "status": str(entry.get("status") or "error"),
                "message": str(entry.get("message") or ""),
                "cached": True,
                "saved_at": str(entry.get("saved_at") or ""),
                "business_date": str(entry.get("business_date") or "")
                or time_utils.business_date_of(entry.get("saved_at")),
            }

    def apply_query(self, key: str, result: dict[str, Any]) -> dict[str, Any]:
        self._ensure_today()
        ok = bool(result.get("ok"))
        status = str(result.get("status") or ("success" if ok else "error"))
        message = result.get("message") or ("查询成功" if ok else "查询失败")
        previous = self.entries.get(key) or {}
        previous_quota = previous.get("quota_usd")
        if previous_quota is None:
            previous_quota = previous.get("last_quota_usd")
        entry = {
            "quota_usd": result.get("quota_usd") if ok else None,
            "last_quota_usd": result.get("quota_usd") if ok else previous_quota,
            "checked_in": result.get("checked_in") if ok else None,
            "ok": ok,
            "status": status,
            "message": message,
            "detail": result.get("detail"),
            "cached": False,
            "saved_at": time_utils.utc_iso(),
            "business_date": self.today,
        }
        self.entries[key] = entry
        if self.autosave:
            self.save()
        return entry

    def apply_checkin(self, key: str, result: dict[str, Any]) -> dict[str, Any]:
        self._ensure_today()
        status = str(result.get("status") or ("success" if result.get("ok") else "error"))
        ok = status in ("success", "already_done") or bool(result.get("ok") and status == "unknown")
        detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
        quota_usd = _detail_quota_usd(detail)
        message = str(result.get("message") or status or "签到完成")
        previous = self.entries.get(key) or {}
        last_quota = (
            quota_usd
            if quota_usd is not None
            else (previous.get("quota_usd") if previous.get("quota_usd") is not None else previous.get("last_quota_usd"))
        )
        entry = {
            "quota_usd": quota_usd,
            "last_quota_usd": last_quota,
            "checked_in": True if ok else None,
            "ok": ok,
            "status": status,
            "message": message,
            "cached": False,
            "saved_at": time_utils.utc_iso(),
            "business_date": self.today,
        }
        self.entries[key] = entry
        if self.autosave:
            self.save()
        return entry

    def snapshot_payload(self) -> dict[str, Any]:
        """在调用线程生成不可变写盘快照，后台线程不读取可变状态。"""
        self._ensure_today()
        entries: dict[str, Any] = {}
        for key, status in self.entries.items():
            if not isinstance(status, dict):
                continue
            if status.get("quota_usd") is None and status.get("last_quota_usd") is None and not status.get("status"):
                continue
            business_day = str(status.get("business_date") or "") or time_utils.business_date_of(status.get("saved_at"))
            if business_day and business_day != self.today:
                continue
            entries[key] = {
                "quota_usd": status.get("quota_usd"),
                "last_quota_usd": status.get("last_quota_usd"),
                "checked_in": status.get("checked_in"),
                "ok": bool(status.get("ok")),
                "status": str(status.get("status") or ""),
                "message": str(status.get("message") or ""),
                "saved_at": str(status.get("saved_at") or ""),
                "business_date": business_day,
            }
        return {"entries": deepcopy(entries), "business_date": self.today}

    @staticmethod
    def write_payload(results_dir: Path, payload: dict[str, Any]) -> None:
        """合并其它 GUI 实例的更新后原子写盘，避免陈旧快照覆盖新状态。"""
        path = results_dir / "gui_status_cache.json"
        results_dir.mkdir(parents=True, exist_ok=True)
        candidate_entries = payload.get("entries") if isinstance(payload, dict) else {}
        if not isinstance(candidate_entries, dict):
            candidate_entries = {}
        business_day = str(payload.get("business_date") or time_utils.business_date())

        with accounts_store.file_lock(path):
            existing_entries: dict[str, Any] = {}
            try:
                existing_payload = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
                raw_entries = existing_payload.get("entries") if isinstance(existing_payload, dict) else {}
                if isinstance(raw_entries, dict):
                    existing_entries = raw_entries
            except Exception:
                existing_entries = {}

            merged: dict[str, Any] = {}
            for key, entry in existing_entries.items():
                if not isinstance(entry, dict):
                    continue
                entry_day = str(entry.get("business_date") or "") or time_utils.business_date_of(entry.get("saved_at"))
                if entry_day == business_day:
                    merged[str(key)] = entry
            for key, entry in candidate_entries.items():
                if not isinstance(entry, dict):
                    continue
                existing = merged.get(str(key))
                if existing is None or _is_newer(entry.get("saved_at"), existing.get("saved_at")):
                    merged[str(key)] = entry
            accounts_store.atomic_write_text(path, json.dumps({"entries": merged}, ensure_ascii=False, indent=2))

    def save(self) -> None:
        """同步兼容入口；GUI 主窗口使用后台存储队列调用 ``write_payload``。"""
        self.write_payload(self.results_dir, self.snapshot_payload())


__all__ = ["StatusStore"]
