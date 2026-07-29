# -*- coding: utf-8 -*-
"""New API 图形验证码签到链路回归。

覆盖的是「接线」而非识别本身（识别在 test_captcha_newapi_bitmap.py）：
状态检测、失败回退、captcha_id 单次有效导致的重取、以及识别器缺失时的降级。

站点真实回执文案（jianzhile.vip 实测）：
  缺字段   → success=false, message="请输入验证码"
  答案错误 → success=false, message="验证码错误，请重试"
  id 复用  → success=false, message="验证码已失效，请刷新后重试"
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from accounts_store import site_config_from_mapping
from providers.base import ApiError, AuthInfo
from providers.profiles import newapi as NA


def _site(**extra: Any):
    base = {
        "name": "t", "base_url": "https://t.invalid", "site_profile": "newapi",
        "auth_method": "cookie", "cookie": "session=x", "user_id": "1",
        "enabled": True,
    }
    base.update(extra)
    return site_config_from_mapping(base)


class FakeClient(NA.NewApiClient):
    """替换 request()，用脚本化回放代替真实 HTTP。"""

    def __init__(self, script: list[Any], **site_kw: Any) -> None:
        super().__init__(_site(**site_kw), AuthInfo(cookie="session=x", new_api_user="1"))
        self.script = script
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: bytes | None = None, **_: Any) -> Any:
        payload = json.loads(body) if body else None
        self.calls.append((method, path, payload))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _captcha_response(captcha_id: str = "cid1") -> dict:
    # 图内容无所谓：测试里 solve 被替换掉
    return {"success": True, "data": {"captcha_id": captcha_id,
                                     "captcha_image": "data:image/png;base64,AA==",
                                     "expires_at": "1"}}


def _stub_solver(monkeypatch, answers, exact=True):
    """把识别器替换成按序返回的假实现。

    必须替换 `captcha_ocr` 包上的属性：`from captcha_ocr import newapi_bitmap`
    先导入包再取属性，只往 sys.modules 塞条目是拦不住的（真实子模块已被导入过）。
    """
    seq = list(answers)

    class R:
        def __init__(self, text: str) -> None:
            self.text = text
            self.exact = exact
            self.detail = ()

    module = type("M", (), {"solve_data_url": staticmethod(lambda _u: R(seq.pop(0)))})
    import captcha_ocr

    monkeypatch.setattr(captcha_ocr, "newapi_bitmap", module, raising=False)
    return module


# ── 状态检测 ─────────────────────────────────────────────────────────────────
def test_status_does_not_demand_human_when_solver_available() -> None:
    """能自动识别时不应再报「需人机验证」—— 否则签到链路根本走不到提交。"""
    client = FakeClient([{"success": True, "data": {
        "captcha_enabled": True, "stats": {"checked_in_today": False}}}])
    status = client.fetch_status()
    assert status.turnstile_required is False
    assert client._captcha_required is True


def test_status_demands_human_when_solver_missing(monkeypatch) -> None:
    monkeypatch.setattr(NA, "_captcha_solver_available", lambda: False)
    client = FakeClient([{"success": True, "data": {
        "captcha_enabled": True, "stats": {"checked_in_today": False}}}])
    assert client.fetch_status().turnstile_required is True


def test_status_without_captcha_is_untouched() -> None:
    client = FakeClient([{"success": True, "data": {"stats": {"checked_in_today": False}}}])
    status = client.fetch_status()
    assert status.turnstile_required is False
    assert client._captcha_required is False


# ── 验证码签到主流程 ─────────────────────────────────────────────────────────
def test_captcha_checkin_submits_recognized_answer(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([
        _captcha_response("cid1"),
        {"success": True, "data": {"quota_awarded": 5000000}},
    ])
    client._captcha_required = True
    reward = client.do_checkin()
    assert reward.quota_awarded == 5000000
    assert client.calls[0][1] == NA.CAPTCHA_ENDPOINT
    assert client.calls[1] == ("POST", "/api/user/checkin",
                               {"captcha_id": "cid1", "captcha_answer": "HRDA6"})


def test_wrong_answer_refetches_a_new_captcha(monkeypatch) -> None:
    """captcha_id 单次有效，所以重试必须重新取图而不是复用旧 id。"""
    _stub_solver(monkeypatch, ["AAAAA", "HRDA6"])
    client = FakeClient([
        _captcha_response("cid1"),
        ApiError(None, None, "验证码错误，请重试"),
        _captcha_response("cid2"),
        {"success": True, "data": {"quota_awarded": 1}},
    ])
    client._captcha_required = True
    client.do_checkin()
    fetches = [c for c in client.calls if c[1] == NA.CAPTCHA_ENDPOINT]
    submits = [c for c in client.calls if c[1] == "/api/user/checkin"]
    assert len(fetches) == 2, "每次重试都应重新取图"
    assert [s[2]["captcha_id"] for s in submits] == ["cid1", "cid2"]


def test_uncertain_recognition_skips_submission(monkeypatch) -> None:
    """识别不确定（exact=False）时应换图而不是硬猜提交。"""
    _stub_solver(monkeypatch, ["AAAAA", "BBBBB", "CCCCC", "HRDA6"], exact=False)
    client = FakeClient([
        _captcha_response("c1"), _captcha_response("c2"), _captcha_response("c3"),
        _captcha_response("c4"),
        {"success": True, "data": {"quota_awarded": 1}},
    ])
    client._captcha_required = True
    client.do_checkin()
    submits = [c for c in client.calls if c[1] == "/api/user/checkin"]
    # 前 3 次因不确定被跳过，只有最后一次（已达上限）才提交
    assert len(submits) == 1
    assert submits[0][2]["captcha_id"] == "c4"


def test_exhausted_attempts_raises_with_recognized_texts(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["AAAAA"] * NA.CAPTCHA_MAX_ATTEMPTS)
    script: list[Any] = []
    for i in range(NA.CAPTCHA_MAX_ATTEMPTS):
        script.append(_captcha_response(f"c{i}"))
        script.append(ApiError(None, None, "验证码错误，请重试"))
    client = FakeClient(script)
    client._captcha_required = True
    with pytest.raises(ApiError) as exc:
        client.do_checkin()
    assert "AAAAA" in exc.value.message
    assert "验证码错误" in exc.value.message


def test_non_captcha_error_is_not_retried(monkeypatch) -> None:
    """非验证码类错误（如未登录）不该被当成「换张图再试」。"""
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([
        _captcha_response("cid1"),
        ApiError(401, None, "Unauthorized, not logged in"),
    ])
    client._captcha_required = True
    with pytest.raises(ApiError) as exc:
        client.do_checkin()
    assert exc.value.status == 401
    assert len([c for c in client.calls if c[1] == NA.CAPTCHA_ENDPOINT]) == 1


# ── 回退：未先查状态就直签 ───────────────────────────────────────────────────
def test_missing_captcha_message_triggers_captcha_flow(monkeypatch) -> None:
    """没先查状态时，靠「请输入验证码」这一明确回执切到验证码流程。"""
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([
        ApiError(None, None, "请输入验证码"),          # legacy 直签被拒
        _captcha_response("cid1"),
        {"success": True, "data": {"quota_awarded": 7}},
    ], api_variant="legacy")
    reward = client.do_checkin()
    assert reward.quota_awarded == 7
    assert client._captcha_required is True


def test_captcha_endpoint_missing_fields_is_reported() -> None:
    client = FakeClient([{"success": True, "data": {"expires_at": "1"}}])
    client._captcha_required = True
    with pytest.raises(ApiError, match="captcha_id"):
        client.do_checkin()


def test_solver_import_failure_degrades_clearly(monkeypatch) -> None:
    """识别器不可用时要给出可操作提示，而不是在签到途中抛 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def guard(name, *a, **k):
        if name == "captcha_ocr" or name.startswith("captcha_ocr."):
            raise ImportError("simulated")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", guard)
    monkeypatch.delitem(__import__("sys").modules, "captcha_ocr.newapi_bitmap", raising=False)
    client = FakeClient([])
    client._captcha_required = True
    with pytest.raises(ApiError, match="识别器不可用"):
        client.do_checkin()
