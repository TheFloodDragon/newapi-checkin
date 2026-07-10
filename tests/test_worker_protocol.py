from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import checkin
import run__all_checkin as runner


ROOT = Path(__file__).resolve().parents[1]


def test_corrupt_unified_config_does_not_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        checkin.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: (_ for _ in ()).throw(checkin.accounts_store.ConfigError("broken")),
    )
    with pytest.raises(checkin.accounts_store.ConfigError, match="broken"):
        checkin.load_sites(tmp_path / "sites.json")


def test_checkin_worker_emits_one_json_object() -> None:
    env = os.environ.copy()
    env.pop("CHECKIN_COOKIE", None)
    env.pop("CHECKIN_ACCESS_TOKEN", None)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "checkin.py"),
            "--base-url",
            "https://example.invalid",
            "--auth-method",
            "cookie",
            "--worker",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert completed.returncode == 2
    assert len(completed.stdout.splitlines()) == 1
    payload = json.loads(completed.stdout)
    assert payload["status"] == "need_login"
    assert set(("site", "base_url", "status", "message")) <= payload.keys()


def test_summary_rejects_unstructured_zero_exit() -> None:
    result = runner.TaskResult("legacy", 0, "looks fine but is not JSON")
    summary = runner.task_result_to_summary(result)
    assert summary["status"] == "error"
    assert not summary["ok"]
    assert "协议错误" in summary["message"]


def test_summary_uses_last_valid_payload() -> None:
    output = '\n'.join([
        '{"site":"old","base_url":"https://old","status":"error","message":"old"}',
        'diagnostic',
        '{"site":"new","base_url":"https://new","status":"success","message":"ok"}',
    ])
    result = runner.TaskResult("task", 0, output)
    summary = runner.task_result_to_summary(result)
    assert summary["site"] == "new"
    assert summary["ok"]


def test_site_task_keeps_secrets_out_of_argv(monkeypatch) -> None:
    monkeypatch.setattr(
        runner.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: [
            {
                "name": "secret-site",
                "base_url": "https://example.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "access_token": "top-secret-token",
                "cookie": "session=top-secret-cookie",
                "user_id": "42",
                "proxy": "http://user:password@proxy.invalid:8080",
            }
        ],
    )
    tasks = runner.build_site_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    argv = " ".join(task.command)
    assert "top-secret-token" not in argv
    assert "top-secret-cookie" not in argv
    assert "password@proxy" not in argv
    assert task.env == {
        "CHECKIN_COOKIE": "session=top-secret-cookie",
        "CHECKIN_ACCESS_TOKEN": "top-secret-token",
        "CHECKIN_USER_ID": "42",
        "CHECKIN_PROXY": "http://user:password@proxy.invalid:8080",
    }
    assert task.worker_protocol
