from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import checkin
import run__all_checkin as runner
from checkin_core.events import WorkerEvent


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
                "refresh_token": "top-secret-refresh",
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
    assert "top-secret-refresh" not in argv
    assert "top-secret-cookie" not in argv
    assert "password@proxy" not in argv
    assert task.env == {
        "CHECKIN_COOKIE": "session=top-secret-cookie",
        "CHECKIN_ACCESS_TOKEN": "top-secret-token",
        "CHECKIN_REFRESH_TOKEN": "top-secret-refresh",
        "CHECKIN_USER_ID": "42",
            "CHECKIN_PROXY": "http://user:password@proxy.invalid:8080",
            "CHECKIN_CACHE_POLICY": "ignore",
        }
    assert task.worker_protocol


def test_api_script_path_reaches_worker(monkeypatch) -> None:
    """api 的纯 HTTP 脚本必须透传给 worker，否则 GUI 可用、批量与 CI 静默失效。"""
    monkeypatch.setattr(
        runner.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: [
            {
                "name": "captcha-site",
                "base_url": "https://captcha.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "script": "scripts/newapi_captcha.py",
                "access_token": "token",
                "user_id": "42",
            }
        ],
    )
    task = runner.build_site_tasks()[0]
    argv = " ".join(task.command)
    assert "--script scripts/newapi_captcha.py" in argv
    assert "--script-timeout" not in argv
    assert "CHECKIN_SCRIPT_ARGS" not in (task.env or {})


def test_script_args_credentials_never_reach_argv(monkeypatch) -> None:
    """browser_script 的 script_args 可能含站点账号密码，必须走环境变量。

    argv 对同机其它用户可见（ps / 任务管理器），把密码放进 --script-args 会泄露。
    """
    monkeypatch.setattr(
        runner.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: [
            {
                "name": "script-site",
                "base_url": "https://script.invalid",
                "site_profile": "sub2api",
                "auth_method": "browser",
                "checkin_action": "browser_script",
                "script": "scripts/checkin/jisudeng.py",
                "script_args": {
                    "email": "user@example.test",
                    "password": "top-secret-password",
                    "start_url": "/check-in",
                },
            }
        ],
    )
    tasks = runner.build_site_tasks()
    assert len(tasks) == 1
    task = tasks[0]
    argv = " ".join(task.command)
    assert "top-secret-password" not in argv
    assert "user@example.test" not in argv
    assert "--script-args" not in argv
    # 脚本路径本身不是凭据，仍可留在 argv 便于诊断。
    assert "scripts/checkin/jisudeng.py" in argv

    payload = json.loads((task.env or {})["CHECKIN_SCRIPT_ARGS"])
    assert payload["password"] == "top-secret-password"
    assert payload["email"] == "user@example.test"
    assert payload["start_url"] == "/check-in"


def test_site_task_forwards_transport_settings(monkeypatch) -> None:
    """verify_ssl / referer_path / auto_refresh_cookie 必须透传给 worker。

    这三项此前只在直接读配置文件的路径生效，worker 模式会静默回落默认值，
    导致同一份 ACCOUNTS.json 在 GUI 与 CI 下行为不一致。
    """
    monkeypatch.setattr(
        runner.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: [
            {
                "name": "tls-site",
                "base_url": "https://expired-cert.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "access_token": "tok",
                "verify_ssl": False,
                "referer_path": "/console/token",
                "auto_refresh_cookie": False,
            }
        ],
    )
    argv = " ".join(runner.build_site_tasks()[0].command)
    assert "--no-verify-ssl" in argv
    assert "--referer-path /console/token" in argv
    assert "--no-auto-refresh-cookie" in argv


def test_site_task_omits_transport_flags_at_defaults(monkeypatch) -> None:
    """默认值不应产生冗余参数（保持 argv 简洁、便于诊断）。"""
    monkeypatch.setattr(
        runner.accounts_store,
        "load_unified_accounts",
        lambda **_kwargs: [
            {
                "name": "plain-site",
                "base_url": "https://plain.invalid",
                "site_profile": "newapi",
                "auth_method": "access_token",
                "checkin_action": "api",
                "access_token": "tok",
            }
        ],
    )
    argv = " ".join(runner.build_site_tasks()[0].command)
    assert "--no-verify-ssl" not in argv
    assert "--no-auto-refresh-cookie" not in argv
    assert "--referer-path" not in argv


# ── 阶段调用日志：批量签到成功时也必须可见 ──────────────────────────────────
def _result_with_diagnostics(diagnostics: str, *, output: str = "", returncode: int = 0) -> runner.TaskResult:
    payload = output or json.dumps(
        {
            "site": "s",
            "base_url": "https://s.invalid",
            "status": "success",
            "message": "签到成功",
            "detail": {},
        },
        ensure_ascii=False,
    )
    return runner.TaskResult(
        name="s",
        returncode=returncode,
        output=payload,
        diagnostics=diagnostics,
        worker_protocol=True,
    )


def test_stage_logs_picks_known_prefixes_only() -> None:
    """只挑阶段日志行，其余 stderr（可能含凭据回显）不纳入。"""
    result = _result_with_diagnostics(
        "\n".join(
            [
                "[api_first:百倍] 尝试纯 API 签到（使用已保存的 access_token）",
                "[api_first:百倍] [token] 状态读取成功：今日已签=True 余额=$607.51",
                "[sub2api:百倍] 已通过浏览器登录态刷新 auth_token",
                "[browser_script:百倍] 已点击签到控件",
                "Cookie: session=should-not-be-picked",
                "[unknown:x] 不在白名单里的前缀",
            ]
        )
    )

    picked = runner.stage_logs(result)

    assert len(picked) == 4
    assert any("尝试纯 API 签到" in line for line in picked)
    assert not any("should-not-be-picked" in line for line in picked)
    assert not any("unknown" in line for line in picked)


def test_stage_logs_accepts_structured_worker_events() -> None:
    event = WorkerEvent(stage="api_first", site="s", message="token 已续期")

    picked = runner.stage_logs(_result_with_diagnostics(event.to_line()))

    assert picked == ["[api_first:s] token 已续期"]


def test_stage_logs_empty_without_diagnostics() -> None:
    assert runner.stage_logs(_result_with_diagnostics("")) == []


def test_print_result_shows_stage_logs_on_success(capsys) -> None:
    """回归：阶段日志此前只在 --verbose/失败时随原始输出出现，
    批量签到成功时完全看不到，用户无法确认是走纯 API 还是退化到开浏览器。"""
    result = _result_with_diagnostics("[api_first:s] 尝试纯 API 签到（使用已保存的 access_token）")

    runner.print_result(result, verbose=False)
    out = capsys.readouterr().out

    assert "调用日志：" in out
    assert "尝试纯 API 签到" in out
    # 成功任务不应打印完整原始输出
    assert "原始输出：" not in out


def test_print_result_does_not_duplicate_when_raw_shown(capsys) -> None:
    """要打印原始输出时不再单列调用日志，避免同样的行出现两次。"""
    result = _result_with_diagnostics("[api_first:s] token 阶段未能完成", returncode=1)

    runner.print_result(result, verbose=False)
    out = capsys.readouterr().out

    assert "原始输出：" in out
    assert "调用日志：" not in out


# ── 同业务日失败重试与结果合并 ────────────────────────────────────────────────
def _retry_task(name: str) -> runner.CheckinTask:
    return runner.CheckinTask(name=name, command=[sys.executable, "noop.py"], site_key=name)


def _history_summary(name: str, status: str, ok: bool) -> dict[str, object]:
    labels = {
        "success": ("成功", "✅"),
        "already_done": ("已领取", "✅"),
        "error": ("失败", "❌"),
    }
    label, icon = labels.get(status, (status, "❌"))
    return {
        "site": name,
        "task": name,
        "base_url": f"https://{name}.invalid",
        "status": status,
        "label": label,
        "icon": icon,
        "ok": ok,
        "returncode": 0 if ok else 2,
        "message": status,
        "note": status,
    }


def _executed_result(name: str, status: str, *, returncode: int | None = None) -> runner.TaskResult:
    ok = status in runner.OK_STATUSES
    payload = {
        "site": name,
        "base_url": f"https://{name}.invalid",
        "status": status,
        "message": status,
    }
    return runner.TaskResult(
        name=name,
        returncode=(0 if ok else 2) if returncode is None else returncode,
        output=json.dumps(payload, ensure_ascii=False),
        worker_protocol=True,
    )


def test_first_retry_mode_run_executes_every_current_task() -> None:
    tasks = [_retry_task("a"), _retry_task("b")]

    plan = runner.build_retry_plan(tasks, None)
    assert runner.retry_plan_tasks(plan) == tasks

    merged = runner.merge_retry_results(
        plan,
        [_executed_result("a", "success"), _executed_result("b", "already_done")],
    )
    assert [row["task"] for row in merged] == ["a", "b"]
    assert all(row["executed_this_run"] is True for row in merged)
    assert all(row["carried_forward"] is False for row in merged)
    assert all(row["retried"] is False for row in merged)


def test_same_day_plan_skips_completed_retries_failures_and_keeps_current_order() -> None:
    tasks = [_retry_task("a"), _retry_task("b"), _retry_task("new")]
    # 历史顺序故意与当前配置不同，验证按 task 名匹配而非按位置拼接。
    history = [
        _history_summary("b", "already_done", True),
        _history_summary("a", "error", False),
    ]

    plan = runner.build_retry_plan(tasks, history)
    assert [task.name for task in runner.retry_plan_tasks(plan)] == ["a", "new"]

    merged = runner.merge_retry_results(
        plan,
        [_executed_result("a", "success"), _executed_result("new", "error")],
    )
    assert [row["task"] for row in merged] == ["a", "b", "new"]
    assert merged[0]["retried"] is True
    assert merged[0]["previous_status"] == "error"
    assert merged[0]["retry_succeeded"] is True
    assert merged[1]["carried_forward"] is True
    assert merged[1]["executed_this_run"] is False
    assert merged[2]["retried"] is False
    assert merged[2]["ok"] is False

    payload = runner.build_result_payload(merged)
    assert payload["executed_this_run_count"] == 2
    assert payload["carried_forward_count"] == 1
    assert payload["retry_succeeded_count"] == 1
    assert payload["failed_count"] == 1


def test_duplicate_task_names_are_matched_with_queues() -> None:
    tasks = [_retry_task("same"), _retry_task("same")]
    history = [
        _history_summary("same", "success", True),
        _history_summary("same", "error", False),
    ]

    plan = runner.build_retry_plan(tasks, history)

    assert [item.carried_forward for item in plan] == [True, False]
    assert len(runner.retry_plan_tasks(plan)) == 1
    merged = runner.merge_retry_results(plan, [_executed_result("same", "success")])
    assert merged[0]["carried_forward"] is True
    assert merged[1]["retried"] is True
    assert merged[1]["retry_succeeded"] is True


def test_retry_success_marker_survives_a_third_carried_run() -> None:
    task = _retry_task("retry-site")
    retry_plan = runner.build_retry_plan(
        [task],
        [_history_summary("retry-site", "error", False)],
    )
    retried = runner.merge_retry_results(
        retry_plan,
        [_executed_result("retry-site", "success")],
    )[0]

    third_plan = runner.build_retry_plan([task], [retried])
    third = runner.merge_retry_results(third_plan, [])[0]

    assert third["retry_succeeded"] is True
    assert third["carried_forward"] is True
    assert third["executed_this_run"] is False
    assert third["retried"] is False
    assert "🔁 重试成功" in runner.summary_run_label(third)
    assert "本轮跳过" in runner.summary_run_label(third)
    assert runner.result_run_counts([third]) == (0, 1, 0)


def test_retry_history_requires_current_business_day_and_valid_structure(tmp_path: Path) -> None:
    result_path = tmp_path / "checkin_result.json"
    today = "2026-07-29"
    valid = {
        "business_date": today,
        "results": [_history_summary("a", "success", True)],
    }
    result_path.write_text(json.dumps(valid), encoding="utf-8")
    assert runner.load_retry_history(result_path, business_day=today) == valid["results"]

    valid["business_date"] = "2026-07-28"
    result_path.write_text(json.dumps(valid), encoding="utf-8")
    assert runner.load_retry_history(result_path, business_day=today) is None

    result_path.write_text("{broken", encoding="utf-8")
    assert runner.load_retry_history(result_path, business_day=today) is None

    result_path.write_text(json.dumps({"business_date": today, "results": [{"task": "a"}]}), encoding="utf-8")
    assert runner.load_retry_history(result_path, business_day=today) is None


def test_invalid_retry_history_falls_back_to_full_execution(tmp_path: Path) -> None:
    result_path = tmp_path / "checkin_result.json"
    result_path.write_text("not-json", encoding="utf-8")
    tasks = [_retry_task("a"), _retry_task("b")]

    history = runner.load_retry_history(result_path, business_day="2026-07-29")
    plan = runner.build_retry_plan(tasks, history)

    assert history is None
    assert runner.retry_plan_tasks(plan) == tasks


def test_main_does_not_mask_a_failed_retry(monkeypatch, capsys) -> None:
    task = _retry_task("still-bad")
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: SimpleNamespace(workers=1, verbose=False, retry_failed=True),
    )
    monkeypatch.setattr(runner, "discover_tasks", lambda: [task])
    monkeypatch.setattr(
        runner,
        "load_retry_history",
        lambda: [_history_summary("still-bad", "error", False)],
    )

    def fake_run(tasks, workers, verbose=False):
        captured["tasks"] = tasks
        return [_executed_result("still-bad", "error")]

    monkeypatch.setattr(runner, "run_tasks", fake_run)
    monkeypatch.setattr(runner, "write_result_file", lambda rows: captured.setdefault("rows", rows))

    assert runner.main() == 2
    assert [item.name for item in captured["tasks"]] == ["still-bad"]
    rows = captured["rows"]
    assert rows[0]["retried"] is True
    assert rows[0]["retry_succeeded"] is False
    assert rows[0]["ok"] is False
    assert "本轮重试" in capsys.readouterr().out


def test_auto_checkin_workflow_runs_twice_with_retry_mode() -> None:
    workflow = (ROOT / ".github" / "workflows" / "auto_checkin.yml").read_text(encoding="utf-8")

    assert workflow.count("cron: '30 1 * * *'") == 1
    assert workflow.count("cron: '30 7 * * *'") == 1
    assert workflow.count("run__all_checkin.py --retry-failed") == 2
    assert "cancel-in-progress: false" in workflow
