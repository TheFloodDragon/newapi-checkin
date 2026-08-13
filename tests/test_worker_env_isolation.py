# -*- coding: utf-8 -*-
"""批量 worker 的任务级环境隔离。

回归背景：run_task 过去用 {**os.environ, **task.env} 构造子进程环境，而
build_site_tasks 只在站点凭据非空时写入 task.env。于是父进程里任何一个
CHECKIN_ACCESS_TOKEN / CHECKIN_COOKIE 都会漏给「没配这项凭据」的站点，
把 A 账号的凭据发到 B 站点。这里用假凭据锁定隔离语义。
"""

from __future__ import annotations

import sys

import run__all_checkin as runner

FAKE_PARENT_TOKEN = "FAKE_PARENT_TOKEN"
FAKE_SITE_TOKEN = "FAKE_SITE_TOKEN"


def _echo_task(*names: str, env: dict[str, str] | None = None) -> runner.CheckinTask:
    """子进程只回显指定环境变量，便于断言隔离结果。"""
    code = "import os;print('|'.join(os.getenv(n, '') for n in %r))" % (names,)
    return runner.CheckinTask("probe", [sys.executable, "-c", code], env=env)


def _run(task: runner.CheckinTask) -> list[str]:
    return runner.run_task(task).output.strip().split("|")


def test_task_scoped_env_covers_all_credential_vars() -> None:
    assert runner.TASK_SCOPED_ENV == {
        "CHECKIN_ACCESS_TOKEN",
        "CHECKIN_REFRESH_TOKEN",
        "CHECKIN_COOKIE",
        "CHECKIN_USER_ID",
        "CHECKIN_BROWSER_STATE",
        # 站点配置里的原始 browser_state：用于让子进程按配置基线计算 state basis，
        # 与注入的运行期登录态分离。同样属任务级敏感值，不能从父环境继承。
        "CHECKIN_CONFIGURED_BROWSER_STATE",
        "CHECKIN_SCRIPT_ARGS",
        "CHECKIN_CACHE_POLICY",
    }
    # 代理是设计上的全局回退，不能被当作任务级凭据清掉。
    assert "CHECKIN_PROXY" not in runner.TASK_SCOPED_ENV


def test_parent_credentials_are_not_inherited(monkeypatch) -> None:
    monkeypatch.setenv("CHECKIN_ACCESS_TOKEN", FAKE_PARENT_TOKEN)
    monkeypatch.setenv("CHECKIN_COOKIE", "session=FAKE_PARENT_COOKIE")
    monkeypatch.setenv("CHECKIN_REFRESH_TOKEN", "FAKE_PARENT_RT")
    monkeypatch.setenv("CHECKIN_USER_ID", "999")
    monkeypatch.setenv("CHECKIN_BROWSER_STATE", "FAKE_PARENT_STATE")
    monkeypatch.setenv("CHECKIN_SCRIPT_ARGS", '{"password":"FAKE_PARENT_PW"}')

    values = _run(
        _echo_task(
            "CHECKIN_ACCESS_TOKEN",
            "CHECKIN_COOKIE",
            "CHECKIN_REFRESH_TOKEN",
            "CHECKIN_USER_ID",
            "CHECKIN_BROWSER_STATE",
            "CHECKIN_SCRIPT_ARGS",
            env={"CHECKIN_CACHE_POLICY": "ignore"},
        )
    )

    assert values == ["", "", "", "", "", ""]


def test_task_env_still_reaches_worker(monkeypatch) -> None:
    monkeypatch.setenv("CHECKIN_ACCESS_TOKEN", FAKE_PARENT_TOKEN)

    values = _run(
        _echo_task(
            "CHECKIN_ACCESS_TOKEN",
            "CHECKIN_CACHE_POLICY",
            env={"CHECKIN_ACCESS_TOKEN": FAKE_SITE_TOKEN, "CHECKIN_CACHE_POLICY": "ignore"},
        )
    )

    assert values == [FAKE_SITE_TOKEN, "ignore"]


def test_env_isolation_applies_without_task_env(monkeypatch) -> None:
    """没有任何任务级凭据的站点同样不得继承父环境。"""
    monkeypatch.setenv("CHECKIN_ACCESS_TOKEN", FAKE_PARENT_TOKEN)

    values = _run(_echo_task("CHECKIN_ACCESS_TOKEN"))

    assert values == [""]


def test_global_proxy_is_still_inherited(monkeypatch) -> None:
    monkeypatch.setenv("CHECKIN_PROXY", "http://127.0.0.1:7897")

    values = _run(_echo_task("CHECKIN_PROXY"))

    assert values == ["http://127.0.0.1:7897"]


def test_unrelated_env_is_preserved(monkeypatch) -> None:
    """只清凭据，不能顺手清掉 PATH / 编码等运行必需变量。"""
    monkeypatch.setenv("CHECKIN_UNRELATED_MARKER", "keep-me")

    env = runner.build_task_env(runner.CheckinTask("probe", [sys.executable, "-c", "pass"]))

    assert env["CHECKIN_UNRELATED_MARKER"] == "keep-me"
    assert "PATH" in env or "Path" in env
