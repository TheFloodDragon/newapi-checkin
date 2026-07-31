# -*- coding: utf-8 -*-
"""config.py 的环境变量覆盖：兑现模块 docstring 的承诺。

旧版 docstring 声称「所有值均可通过环境变量覆盖」，但文件里没有任何 os.environ
读取——CI 里设了环境变量调超时却静默无效。这里锁定覆盖真的生效，并且非法取值
必须回落默认值（一个手误的环境变量不能让所有任务瞬间超时）。

config 在导入时一次性读取环境变量，因此每个用例用 importlib.reload 重新加载。
"""

from __future__ import annotations

import importlib

import pytest


def _reload_config(monkeypatch, **env: str):
    """在给定环境变量下重新加载 config 模块。"""
    import config as config_module

    for key in list(env):
        monkeypatch.setenv(key, env[key])
    return importlib.reload(config_module)


@pytest.fixture(autouse=True)
def _restore_config():
    """用例结束后把 config 还原为无覆盖状态，避免污染其它测试。"""
    yield
    import config as config_module

    importlib.reload(config_module)


def test_defaults_apply_without_env(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch)
    assert cfg.Timeouts.HTTP_REQUEST == 30
    assert cfg.Timeouts.BROWSER_TASK == 420.0
    assert cfg.RetryConfig.MAX_ATTEMPTS == 3
    assert cfg.RetryConfig.STATUS_CODES == frozenset({429, 500, 502, 503, 504})
    assert cfg.WAFConfig.BLOCK_THRESHOLD == 2
    assert cfg.FileLockConfig.DEFAULT_TIMEOUT == 30.0
    assert cfg.OutputConfig.MAX_OUTPUT_SCAN == 4096


def test_int_and_float_overrides_take_effect(monkeypatch) -> None:
    cfg = _reload_config(
        monkeypatch,
        CHECKIN_HTTP_REQUEST="7",
        CHECKIN_BROWSER_TASK="600",
        CHECKIN_WAF_BLOCK_THRESHOLD="5",
        CHECKIN_FILE_LOCK_TIMEOUT="2.5",
        CHECKIN_MAX_OUTPUT_SCAN="8192",
    )
    assert cfg.Timeouts.HTTP_REQUEST == 7
    assert cfg.Timeouts.BROWSER_TASK == 600.0
    assert cfg.WAFConfig.BLOCK_THRESHOLD == 5
    assert cfg.FileLockConfig.DEFAULT_TIMEOUT == 2.5
    assert cfg.OutputConfig.MAX_OUTPUT_SCAN == 8192


def test_status_code_set_override(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, CHECKIN_RETRY_STATUS_CODES="429, 503")
    assert cfg.RetryConfig.STATUS_CODES == frozenset({429, 503})


def test_invalid_values_fall_back_to_defaults(monkeypatch) -> None:
    """非法取值必须被忽略，而不是让超时变成 0 或抛异常。"""
    cfg = _reload_config(
        monkeypatch,
        CHECKIN_HTTP_REQUEST="not-a-number",
        CHECKIN_BROWSER_TASK="",
        CHECKIN_RETRY_STATUS_CODES="abc,def",
        CHECKIN_FILE_LOCK_TIMEOUT="-1",
    )
    assert cfg.Timeouts.HTTP_REQUEST == 30
    assert cfg.Timeouts.BROWSER_TASK == 420.0
    assert cfg.RetryConfig.STATUS_CODES == frozenset({429, 500, 502, 503, 504})
    assert cfg.FileLockConfig.DEFAULT_TIMEOUT == 30.0


def test_below_minimum_falls_back(monkeypatch) -> None:
    """0 / 负数超时会让任务立即失败，必须回落默认值。"""
    cfg = _reload_config(monkeypatch, CHECKIN_HTTP_REQUEST="0", CHECKIN_RETRY_MAX_ATTEMPTS="0")
    assert cfg.Timeouts.HTTP_REQUEST == 30
    assert cfg.RetryConfig.MAX_ATTEMPTS == 3


def test_file_lock_uses_config_timeout(monkeypatch, tmp_path) -> None:
    """accounts_store 的锁超时必须取自 config，而非硬编码 30.0。"""
    import accounts_store

    # 签名默认值为 None，运行时才解析为 FileLockConfig.DEFAULT_TIMEOUT，
    # 这样改 config 才真的生效（旧实现把 30.0 写死在签名里）。
    import inspect

    sig = inspect.signature(accounts_store._file_lock)
    assert sig.parameters["timeout"].default is None
    sig_public = inspect.signature(accounts_store.file_lock)
    assert sig_public.parameters["timeout"].default is None

    # 仍能正常加锁写入（含可重入）。
    path = tmp_path / "x.json"
    with accounts_store.file_lock(path):
        with accounts_store.file_lock(path):
            accounts_store.atomic_write_text(path, '{"ok":1}')
    assert path.read_text(encoding="utf-8") == '{"ok":1}'


def test_removed_dead_constants_stay_removed(monkeypatch) -> None:
    """PLAYWRIGHT_ACTION 曾是零消费者的死配置，删除后不应回归。"""
    cfg = _reload_config(monkeypatch)
    assert not hasattr(cfg.Timeouts, "PLAYWRIGHT_ACTION")
    # LOCK_SIZE 是 msvcrt 语义要求的固定值，不开放环境变量覆盖。
    assert cfg.FileLockConfig.LOCK_SIZE == 1


# ── 非有限值与上限（已复现：inf 会让 subprocess.run 抛 OverflowError）──────────
@pytest.mark.parametrize("raw", ["inf", "-inf", "Infinity", "nan", "NaN"])
def test_non_finite_floats_fall_back(monkeypatch, raw: str) -> None:
    """float('inf') 能通过旧的 >minimum 检查，一路传到 subprocess.run(timeout=)。"""
    cfg = _reload_config(monkeypatch, CHECKIN_BROWSER_TASK=raw)
    assert cfg.Timeouts.BROWSER_TASK == 420.0


def test_non_finite_timeout_stays_usable_by_subprocess(monkeypatch) -> None:
    """回落后的值必须能真正传给 subprocess（Windows 上 inf 会 OverflowError）。"""
    import subprocess
    import sys

    cfg = _reload_config(monkeypatch, CHECKIN_HTTP_TASK="inf")
    completed = subprocess.run(
        [sys.executable, "-c", "pass"],
        timeout=cfg.Timeouts.HTTP_TASK,
        capture_output=True,
    )
    assert completed.returncode == 0


@pytest.mark.parametrize(
    ("name", "raw", "attr", "expected"),
    [
        ("CHECKIN_HTTP_REQUEST", "99999", "HTTP_REQUEST", 30),
        ("CHECKIN_BROWSER_TASK", "99999999", "BROWSER_TASK", 420.0),
        ("CHECKIN_OAUTH_WAIT", "100000", "OAUTH_WAIT", 25),
    ],
)
def test_above_maximum_falls_back(monkeypatch, name: str, raw: str, attr: str, expected) -> None:
    cfg = _reload_config(monkeypatch, **{name: raw})
    assert getattr(cfg.Timeouts, attr) == expected


def test_output_scan_has_bounds(monkeypatch) -> None:
    cfg = _reload_config(monkeypatch, CHECKIN_MAX_OUTPUT_SCAN="1")
    assert cfg.OutputConfig.MAX_OUTPUT_SCAN == 4096
    cfg = _reload_config(monkeypatch, CHECKIN_MAX_OUTPUT_SCAN="99999999")
    assert cfg.OutputConfig.MAX_OUTPUT_SCAN == 4096


def test_invalid_override_is_reported(monkeypatch, capsys) -> None:
    """静默回落会让用户以为覆盖已生效；必须留一行告警。"""
    _reload_config(monkeypatch, CHECKIN_BROWSER_TASK="inf")
    assert "CHECKIN_BROWSER_TASK" in capsys.readouterr().err


def test_script_timeout_default_respects_max(monkeypatch) -> None:
    """默认值路径以前不夹取上限：缺省/非法输入可拿到超过 MAX 的超时。

    直接改 Timeouts 类属性而不是重载模块：accounts_store 在导入时就持有
    Timeouts 类的引用，reload(config) 只会造出一个新类，反而让其它模块的
    「同一实现」身份断言失败（实测污染 tests/test_convergence.py）。
    """
    import accounts_store

    monkeypatch.setattr(accounts_store.Timeouts, "BROWSER_SCRIPT_DEFAULT", 7000, raising=False)
    monkeypatch.setattr(accounts_store.Timeouts, "BROWSER_SCRIPT_MAX", 3600, raising=False)

    assert accounts_store.parse_script_timeout(None) == 3600
    assert accounts_store.parse_script_timeout("not-a-number") == 3600
    assert accounts_store.parse_script_timeout(9999) == 3600
    assert accounts_store.parse_script_timeout(120) == 120
