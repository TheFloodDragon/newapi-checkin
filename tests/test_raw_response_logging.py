# -*- coding: utf-8 -*-
"""站点原始返回值日志与详细汇总。

为什么需要这些用例：签到失败时最有用的信息是「站点到底回了什么」。此前各层只把
message 往上传，原始返回值只留在 ApiError.payload 里且默认不打印；验证码流程的
日志更是因为前缀不在白名单里被整段丢弃（实测 sheapi 站点用了验证码却一行日志
都没有）。这里锁定三件事：原始回执会落日志、凭据仍被脱敏、细节会进汇总。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

import run__all_checkin as runner
from providers.base import ApiError, log_http_exchange


# ── 原始返回值日志 ───────────────────────────────────────────────────────────
def test_success_payload_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    log_http_exchange("测试站", "GET", "https://x.invalid/api/user/self",
                      payload={"success": True, "data": {"quota": 500000}})
    line = capsys.readouterr().err.strip()
    assert line.startswith("[http:测试站] GET https://x.invalid/api/user/self → ")
    assert '"quota":500000' in line


def test_error_payload_and_status_are_logged(capsys: pytest.CaptureFixture[str]) -> None:
    """失败分支必须带上站点原文：只有 message 时分不清验证码错误还是 WAF 换页。"""
    error = ApiError(400, {"success": False, "message": "验证码错误，请重试"}, "验证码错误，请重试")
    log_http_exchange("测试站", "POST", "https://x.invalid/api/user/checkin", error=error)
    line = capsys.readouterr().err
    assert "status=400" in line
    assert "验证码错误，请重试" in line


def test_logged_payload_is_masked(capsys: pytest.CaptureFixture[str]) -> None:
    """原始回执可能带 token，落日志前必须脱敏。"""
    log_http_exchange("测试站", "GET", "https://x.invalid/api/user/self",
                      payload={"access_token": "FAKE_TOKEN_ABCDEFGH"})
    assert "FAKE_TOKEN_ABCDEFGH" not in capsys.readouterr().err


def test_long_payload_is_truncated(capsys: pytest.CaptureFixture[str]) -> None:
    """站点可能回整页 HTML；无上限会把日志刷爆。"""
    log_http_exchange("测试站", "GET", "https://x.invalid/big", payload="x" * 100_000)
    line = capsys.readouterr().err
    assert "已截断" in line
    assert len(line) < 10_000


def test_logging_can_be_disabled_by_env(monkeypatch: pytest.MonkeyPatch,
                                        capsys: pytest.CaptureFixture[str]) -> None:
    """噪音敏感的场景要能整体关掉，而不是只能改代码。"""
    monkeypatch.setenv("CHECKIN_LOG_HTTP_BODY", "0")
    import config as config_module
    import providers.base as base_module

    importlib.reload(config_module)
    importlib.reload(base_module)
    try:
        base_module.log_http_exchange("测试站", "GET", "https://x.invalid/a", payload={"ok": 1})
        assert capsys.readouterr().err == ""
    finally:
        monkeypatch.delenv("CHECKIN_LOG_HTTP_BODY", raising=False)
        importlib.reload(config_module)
        importlib.reload(base_module)


# ── 阶段日志前缀白名单 ───────────────────────────────────────────────────────
def _result(diagnostics: str) -> runner.TaskResult:
    return runner.TaskResult("t", 0, "", diagnostics=diagnostics, worker_protocol=True)


def test_api_and_http_prefixes_are_surfaced() -> None:
    """[api:...] 曾不在白名单里，验证码脚本的全部日志因此被丢弃。"""
    picked = runner.stage_logs(_result(
        "[api:她 API] 站点签到需要图形验证码，走离线识别流程\n"
        "[http:她 API] POST https://x.invalid/api/user/checkin → {\"success\":true}\n"
        "[newapi:她 API] 开始接口签到\n"
        "无关的调试行\n"
    ))
    markers = [line.split("]", 1)[0] + "]" for line in picked]
    assert markers == ["[api:她 API]", "[http:她 API]", "[newapi:她 API]"]


def test_unrelated_prefixes_are_still_filtered() -> None:
    """白名单存在的意义是避免把可能回显凭据的整块输出无条件打出来。"""
    assert runner.stage_logs(_result("[DEBUG] internal\n[WARN] something\n")) == []


# ── 汇总渲染 ─────────────────────────────────────────────────────────────────
def test_captcha_details_appear_in_note() -> None:
    note = runner.build_detail_note(
        "success", "签到成功",
        {"checkin_source": "api+captcha", "captcha_dialect": "string_captcha",
         "captcha_attempts": 2, "captcha_answer_exact": True},
    )
    assert "验证码：string_captcha，第 2 次通过" in note
    assert "接口签到 + 图形验证码" in note


def test_uncertain_captcha_reading_is_flagged() -> None:
    note = runner.build_detail_note(
        "success", "签到成功",
        {"captcha_dialect": "bitmap_code", "captcha_attempts": 4,
         "captcha_answer_exact": False},
    )
    assert "读数不可信" in note


def test_failed_captcha_reports_attempts() -> None:
    note = runner.build_detail_note(
        "need_verification", "图形验证码连续 4 次未通过",
        {"captcha_attempts": 4, "captcha_failed": True},
    )
    assert "4 次均失败" in note


def test_quiz_outcome_appears_in_note() -> None:
    note = runner.build_detail_note(
        "success", "签到成功",
        {"checkin_source": "browser_script",
         "quiz": {"outcome": "submitted", "message": "答题 8/10，获得 $0.80", "unknown": 0}},
    )
    assert "答题：答题 8/10，获得 $0.80" in note


def test_quiz_unknown_count_is_flagged() -> None:
    """未收录题数是「该去补题库」的信号，不能只留在日志里。"""
    note = runner.build_detail_note(
        "success", "签到成功",
        {"quiz": {"outcome": "submitted", "message": "答题 6/10", "unknown": 4}},
    )
    assert "4 题未收录，已猜" in note


def test_notes_stay_empty_without_extra_details() -> None:
    """没有验证码/答题的普通站点不该多出空字段。"""
    note = runner.build_detail_note("success", "签到成功", {"checkin_source": "api"})
    assert "验证码" not in note
    assert "答题" not in note


@pytest.mark.parametrize("detail", [None, "", 0, [], "text"])
def test_note_renderers_tolerate_non_dict_detail(detail: Any) -> None:
    assert runner.captcha_note(detail) == ""
    assert runner.quiz_note(detail) == ""
