# -*- coding: utf-8 -*-
"""browser_script 执行日志的守护测试。

browser_script 曾是唯一完全静默的浏览器路径：relogin / newapi / sub2api 都会往
stderr 打进度，而站点脚本跑几十秒（启浏览器、过 Turnstile、账密登录、轮询按钮）
却没有任何输出，失败时只有一行最终结论，无从定位卡在哪一步。

这里锁定三件事：
1. helpers.log 确实把日志写到 stderr（worker 模式 stdout 是机器协议通道）；
2. 日志经过脱敏，凭据不会进日志；
3. 运行器注入的站点前缀生效，多站点并发时能区分来源。
"""

from __future__ import annotations

import io
import contextlib
from pathlib import Path
from types import SimpleNamespace

from browser.script_helpers import ScriptHelpers
from browser.script_runner import _make_log


def _helpers(log=None) -> ScriptHelpers:
    site = SimpleNamespace(base_url="https://site.invalid", name="站点")
    return ScriptHelpers(None, None, site, Path("."), log=log)


def _capture(fn) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        fn()
    return buf.getvalue()


def test_log_writes_to_stderr_not_stdout() -> None:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        _helpers().log("进度信息")
    assert "进度信息" in err.getvalue()
    # stdout 是 worker 协议通道，绝不能被日志污染。
    assert out.getvalue() == ""


def test_log_masks_credentials() -> None:
    text = _capture(lambda: _helpers().log("password=SuperSecret123 token=abcdefghijklmnopqrst"))
    assert "SuperSecret123" not in text
    assert "abcdefghijklmnopqrst" not in text


def test_log_masks_bearer_and_jwt() -> None:
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_value_here"
    text = _capture(lambda: _helpers().log(f"Authorization: Bearer {jwt}"))
    assert "eyJhbGciOiJIUzI1NiJ9" not in text


def test_log_ignores_blank_messages() -> None:
    assert _capture(lambda: _helpers().log("")) == ""
    assert _capture(lambda: _helpers().log("   ")) == ""


def test_runner_log_adds_site_prefix_and_masks() -> None:
    log = _make_log("百倍-主号")
    text = _capture(lambda: log("等待签到按钮渲染"))
    assert "[browser_script:百倍-主号]" in text
    assert "等待签到按钮渲染" in text

    masked = _capture(lambda: log("password=Secret123456"))
    assert "Secret123456" not in masked


def test_helpers_prefers_injected_log() -> None:
    """注入回调时用它（带站点前缀），否则回落到自带的 stderr 输出。"""
    seen: list[str] = []
    _helpers(log=seen.append).log("走注入回调")
    assert seen == ["走注入回调"]


def test_helpers_falls_back_when_injected_log_raises() -> None:
    """注入的回调抛错不能让脚本挂掉，应回落到自带输出。"""

    def boom(_message: str) -> None:
        raise RuntimeError("sink down")

    text = _capture(lambda: _helpers(log=boom).log("回落输出"))
    assert "回落输出" in text


def test_runner_log_handles_empty_site_name() -> None:
    text = _capture(lambda: _make_log("")("无站点名"))
    assert "[browser_script:browser_script]" in text
