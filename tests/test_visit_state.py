# -*- coding: utf-8 -*-
"""visit 额度状态文件迁移与并发写入测试。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import accounts_store
from providers.actions import visit


def _state_paths(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    state_path = tmp_path / "cache" / "login_grant_state.json"
    legacy_path = tmp_path / "login_grant_state.json"
    monkeypatch.setattr(visit, "STATE_PATH", state_path)
    monkeypatch.setattr(visit, "LEGACY_STATE_PATH", legacy_path)
    return state_path, legacy_path


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_state_path_uses_results_directory() -> None:
    assert visit.STATE_PATH == accounts_store.RESULTS_DIR / "login_grant_state.json"


def test_first_update_migrates_legacy_state_without_deleting_it(tmp_path: Path, monkeypatch) -> None:
    state_path, legacy_path = _state_paths(tmp_path, monkeypatch)
    legacy_state = {"https://old.invalid": {"quota": 10}}
    _write_json(legacy_path, legacy_state)

    previous = visit._record_state("https://new.invalid", {"quota": 20})

    assert previous == {}
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        **legacy_state,
        "https://new.invalid": {"quota": 20},
    }
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_state


def test_new_state_takes_priority_when_both_files_exist(tmp_path: Path, monkeypatch) -> None:
    state_path, legacy_path = _state_paths(tmp_path, monkeypatch)
    legacy_state = {
        "legacy-only": {"quota": 1},
        "same": {"quota": 2},
    }
    new_state = {
        "new-only": {"quota": 3},
        "same": {"quota": 4},
    }
    _write_json(legacy_path, legacy_state)
    _write_json(state_path, new_state)

    assert visit._load_state() == new_state
    assert visit._record_state("added", {"quota": 5}) == {}

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted == {**new_state, "added": {"quota": 5}}
    assert "legacy-only" not in persisted
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_state


def test_concurrent_migration_updates_do_not_lose_entries(tmp_path: Path, monkeypatch) -> None:
    state_path, legacy_path = _state_paths(tmp_path, monkeypatch)
    legacy_state = {"legacy": {"quota": 0}}
    _write_json(legacy_path, legacy_state)

    def record(index: int) -> None:
        visit._record_state(f"site-{index}", {"quota": index})

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(record, range(40)))

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["legacy"] == {"quota": 0}
    assert len(persisted) == 41
    for index in range(40):
        assert persisted[f"site-{index}"] == {"quota": index}
    assert legacy_path.exists()
