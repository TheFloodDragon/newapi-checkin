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
