# -*- coding: utf-8 -*-
"""稳定任务身份与同站租约（纯逻辑，不依赖 PySide6）。"""

from __future__ import annotations

from gui import core


def _row(name: str, base_url: str, **kwargs) -> core.SiteRow:
    return core.SiteRow(name=name, base_url=base_url, **kwargs)


def test_runtime_id_is_unique_and_not_persisted() -> None:
    a = _row("a", "https://a.invalid")
    b = _row("b", "https://b.invalid")
    assert a.runtime_id and a.runtime_id != b.runtime_id
    assert "runtime_id" not in a.to_legacy()
    assert "runtime_id" not in core.persist_accounts([a])[0]
    assert a.runtime_id not in core.config_snapshot([a], {})


def test_credential_baselines_use_runtime_identity() -> None:
    row = _row("s", "https://s.invalid", access_token="a.b.c")

    snapshots = core.credential_snapshots([row])

    assert set(snapshots) == {row.runtime_id}
    assert snapshots[row.runtime_id]["access_token"] == "a.b.c"
    assert id(row) not in snapshots


def test_copy_creates_new_runtime_identity() -> None:
    original = _row("orig", "https://a.invalid")
    clone = original.copy()
    assert clone.runtime_id != original.runtime_id


def test_snapshot_keeps_submit_time_identity_after_rename() -> None:
    row = _row("旧名", "https://a.invalid")
    snapshot = core.make_task_snapshot(row, {})
    row.name = "新名"

    assert snapshot.name == "旧名"
    assert snapshot.status_key == "https://a.invalid|旧名"
    assert snapshot.row_id == row.runtime_id
    # 改名后当前状态键已变，但快照仍指向提交时身份，结果不会写到新身份上。
    assert core.StatusStore.status_key(row) != snapshot.status_key


def test_lease_blocks_second_task_on_same_site() -> None:
    registry = core.TaskLeaseRegistry()
    first = _row("渠道1", "https://same.invalid")
    second = _row("渠道2", "https://same.invalid")

    lease = registry.acquire_single(first)
    assert lease is not None
    # 同址的另一个渠道不得与之并发：站点资源必须独占。
    assert registry.acquire_single(second) is None
    assert registry.is_site_running("https://same.invalid") is True
    assert registry.is_channel_running(core.StatusStore.task_key(first)) is True

    assert registry.release(lease) is True
    assert registry.acquire_single(second) is not None


def test_lease_allows_different_sites_concurrently() -> None:
    registry = core.TaskLeaseRegistry()
    assert registry.acquire_single(_row("a", "https://a.invalid")) is not None
    assert registry.acquire_single(_row("b", "https://b.invalid")) is not None
    assert registry.running_channels == 2


def test_batch_group_lease_covers_all_channels() -> None:
    registry = core.TaskLeaseRegistry()
    rows = [
        _row("渠道1", "https://multi.invalid"),
        _row("渠道2", "https://multi.invalid"),
        _row("渠道3", "https://multi.invalid"),
    ]
    snapshots = [core.make_task_snapshot(row, {}) for row in rows]

    lease = registry.acquire_group(snapshots)
    assert lease is not None
    # 批量占用整站后，单签不能插入同一站点
    assert registry.acquire_single(rows[1]) is None
    for row in rows:
        assert registry.is_channel_running(core.StatusStore.task_key(row)) is True

    registry.release(lease)
    assert registry.running_channels == 0


def test_stale_lease_cannot_release_new_task() -> None:
    registry = core.TaskLeaseRegistry()
    row = _row("s", "https://s.invalid")
    first = registry.acquire_single(row)
    registry.release(first)
    second = registry.acquire_single(row)

    # 旧回调迟到时不得释放新任务的租约
    assert registry.release(first) is False
    assert registry.is_site_running("https://s.invalid") is True
    assert registry.release(second) is True


# ── 运行中修改行列表：回调必须按稳定身份定位 ──────────────────────────────────
class _RowsHarness:
    """复刻 App 的「按 runtime_id 重新定位 + 租约释放」回调语义（不依赖 Qt）。"""

    def __init__(self, rows: list[core.SiteRow]) -> None:
        self.rows = rows
        self.leases = core.TaskLeaseRegistry()
        self.refreshed: list[str] = []

    def row_index(self, row_id: str) -> int | None:
        for index, row in enumerate(self.rows):
            if row.runtime_id == row_id:
                return index
        return None

    def submit(self, row: core.SiteRow):
        lease = self.leases.acquire_single(row)
        assert lease is not None
        return core.make_task_snapshot(row, {}), lease

    def on_done(self, snapshot: core.TaskSnapshot, lease: core.TaskLease) -> None:
        idx = self.row_index(snapshot.row_id)
        if idx is not None:
            self.refreshed.append(self.rows[idx].name)
        self.leases.release(lease)


def test_callback_follows_row_after_insert_and_reorder() -> None:
    target = _row("目标", "https://t.invalid")
    harness = _RowsHarness([_row("前置", "https://a.invalid"), target])
    snapshot, lease = harness.submit(target)

    harness.rows.insert(0, _row("新插入", "https://n.invalid"))
    harness.rows.reverse()
    harness.on_done(snapshot, lease)

    assert harness.refreshed == ["目标"]
    assert harness.leases.running_channels == 0


def test_callback_after_row_deletion_releases_lease_without_error() -> None:
    target = _row("将被删除", "https://t.invalid")
    harness = _RowsHarness([target, _row("其它", "https://o.invalid")])
    snapshot, lease = harness.submit(target)

    harness.rows.remove(target)
    harness.on_done(snapshot, lease)

    # 行已消失：不刷新任何行，但租约必须释放，否则该站点永久卡「运行中」
    assert harness.refreshed == []
    assert harness.leases.running_channels == 0
    assert harness.leases.is_site_running("https://t.invalid") is False


def test_result_keeps_submit_identity_when_row_renamed_mid_flight() -> None:
    target = _row("旧名", "https://t.invalid")
    harness = _RowsHarness([target])
    snapshot, lease = harness.submit(target)

    target.name = "新名"
    harness.on_done(snapshot, lease)

    # 结果归属提交时的状态键，不会被写到改名后的新身份上
    assert snapshot.status_key == "https://t.invalid|旧名"
    assert core.StatusStore.status_key(target) == "https://t.invalid|新名"
    assert harness.leases.running_channels == 0
