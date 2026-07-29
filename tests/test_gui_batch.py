# -*- coding: utf-8 -*-
"""GUI 批量执行分组 + refresh_token 状态提示的纯逻辑回归。

不依赖 PySide6：只测 gui.core 里的分组语义与 FormPlan 计算。
GUI 控件层的显隐由 scripts/preview_gui.py 的离屏冒烟覆盖。
"""

from __future__ import annotations

from gui import core


def _row(name: str, base_url: str, **kwargs) -> core.SiteRow:
    return core.SiteRow(name=name, base_url=base_url, **kwargs)


def _group(rows: list[core.SiteRow]) -> list[tuple[str, list[int]]]:
    """复刻 App._collect_batch 的分组语义。

    分组用 site_group_key（同 base_url 归一组，组内串行以避免站点限流），
    互斥锁用 task_key（渠道级）。两者刻意分离，见 StatusStore 的注释。
    """
    groups: dict[str, list[int]] = {}
    for idx, row in enumerate(rows):
        if not row.enabled:
            continue
        key = core.StatusStore.site_group_key(row)
        if not key:
            continue
        groups.setdefault(key, []).append(idx)
    return list(groups.items())


# ── 批量分组：同 base_url 多账号不得丢弃 ──────────────────────────────────────
def test_same_base_url_accounts_are_all_kept() -> None:
    """旧实现对重复 base_url 只留第一个，导致 GUI「全部签到」静默漏签。"""
    rows = [
        _row("账号1", "https://same.invalid"),
        _row("账号2", "https://same.invalid"),
        _row("账号3", "https://same.invalid"),
        _row("独立", "https://other.invalid"),
    ]
    groups = _group(rows)

    assert len(groups) == 2, "两个不同 base_url 应分两组"
    by_key = dict(groups)
    assert by_key["https://same.invalid"] == [0, 1, 2], "同站三个账号必须全部保留"
    assert by_key["https://other.invalid"] == [3]
    # 覆盖的行数等于启用行数：没有任何账号被丢弃
    assert sum(len(v) for _k, v in groups) == 4


def test_disabled_rows_are_excluded_from_batch() -> None:
    rows = [
        _row("启用", "https://a.invalid"),
        _row("禁用", "https://b.invalid", enabled=False),
    ]
    groups = _group(rows)
    assert [k for k, _v in groups] == ["https://a.invalid"]


def test_rows_without_base_url_are_skipped() -> None:
    rows = [_row("无地址", ""), _row("正常", "https://a.invalid")]
    groups = _group(rows)
    # task_key 回退到 name，因此无地址行仍可入组但不会与正常行混淆
    keys = [k for k, _v in groups]
    assert "https://a.invalid" in keys
    assert len(groups) == 2


def test_task_key_is_per_channel_not_per_site() -> None:
    """互斥锁必须按渠道区分：同址多账号要能各自独立签到。

    旧实现 task_key 只用 base_url，同址三个渠道共享一把锁：单独签到其中一个，
    另外两个被判「该站点已有任务运行中」而跳过，行状态也被一起点亮/清除。
    """
    a = _row("账号A", "https://same.invalid")
    b = _row("账号B", "https://same.invalid")
    assert core.StatusStore.task_key(a) != core.StatusStore.task_key(b)
    # 状态键同样按渠道区分，否则两个账号的结果会互相覆盖
    assert core.StatusStore.status_key(a) != core.StatusStore.status_key(b)
    # 但批量分组仍按站点归一组，保住「组内串行、不并发打同一站点」的限流语义
    assert core.StatusStore.site_group_key(a) == core.StatusStore.site_group_key(b)


def test_locking_one_channel_blocks_siblings_on_same_site() -> None:
    """站点级资源独占：同址渠道全部保留但不得并发。

    渠道 key 仍然区分状态归属（避免结果互相覆盖），而站点租约保证同一 base_url
    只有一个任务在跑，与 run__all_checkin 的 site_locks 语义一致。
    """
    rows = [
        _row("渠道1", "https://multi.invalid"),
        _row("渠道2", "https://multi.invalid"),
        _row("渠道3", "https://multi.invalid"),
    ]
    registry = core.TaskLeaseRegistry()
    lease = registry.acquire_single(rows[0])

    assert lease is not None
    assert [r.name for r in rows[1:] if registry.acquire_single(r) is None] == ["渠道2", "渠道3"]
    # 释放后其余渠道依次可执行，不会丢任务
    registry.release(lease)
    assert registry.acquire_single(rows[1]) is not None


# ── refresh_token 状态提示 ───────────────────────────────────────────────────
def test_refresh_hint_shown_for_sub2api_with_token() -> None:
    row = _row(
        "有RT", "https://a.invalid", type="sub2api",
        auth_method="access_token", access_token="t", refresh_token="rt",
    )
    plan = core.build_form_plan(row, {})
    assert plan.show_refresh_status is True
    assert "已保存 refresh_token" in plan.refresh_status
    assert "纯 HTTP" in plan.refresh_status


def test_refresh_hint_prompts_recapture_when_missing() -> None:
    row = _row(
        "无RT", "https://a.invalid", type="sub2api",
        auth_method="access_token", access_token="t",
    )
    plan = core.build_form_plan(row, {})
    assert plan.show_refresh_status is True
    assert "未保存 refresh_token" in plan.refresh_status
    assert "浏览器登录捕获" in plan.refresh_status


def test_refresh_hint_hidden_for_newapi() -> None:
    """refresh_token 是 sub2api 的续期机制，newapi 站点不应看到该提示。"""
    row = _row("NewAPI", "https://a.invalid", type="newapi", auth_method="cookie", cookie="c")
    plan = core.build_form_plan(row, {})
    assert plan.show_refresh_status is False


# ── refresh_token 贯通 task_params / persist / snapshot ──────────────────────
def test_refresh_token_reaches_task_params() -> None:
    row = _row(
        "s", "https://a.invalid", type="sub2api",
        auth_method="access_token", access_token="t", refresh_token="rt-value",
    )
    assert core.task_params(row, {})["refresh_token"] == "rt-value"


def test_refresh_token_is_persisted() -> None:
    row = _row(
        "s", "https://a.invalid", type="sub2api",
        auth_method="access_token", access_token="t", refresh_token="rt-value",
    )
    assert core.persist_accounts([row])[0]["refresh_token"] == "rt-value"


def test_refresh_token_change_marks_config_dirty() -> None:
    """捕获到新 refresh_token 后必须能被脏检查发现，否则用户不知道要保存。"""
    row = _row("s", "https://a.invalid", type="sub2api", auth_method="access_token", access_token="t")
    before = core.config_snapshot([row], {})
    row.refresh_token = "newly-captured"
    assert core.config_snapshot([row], {}) != before


def test_row_from_store_loads_refresh_token() -> None:
    row = core.row_from_store(
        {
            "name": "s",
            "base_url": "https://a.invalid",
            "site_profile": "sub2api",
            "auth_method": "access_token",
            "access_token": "t",
            "refresh_token": "rt",
        }
    )
    assert row.refresh_token == "rt"
