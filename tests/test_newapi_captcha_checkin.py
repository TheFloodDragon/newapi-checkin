# -*- coding: utf-8 -*-
"""New API 图形验证码签到链路回归（脚本 + 接线，不含识别本身）。

识别的准确率在 test_captcha_newapi_bitmap.py / test_captcha_base64.py。这里覆盖的是
「怎么把它接到签到上」：

- 脚本 scripts/newapi_captcha.py：方言探测、字段名、重取、不确定就换图；
- api action 的脚本钩子：什么时候调、脚本不接管时如何回落、异常如何归类；
- newapi profile：没配脚本却撞上验证码时，报错要说清怎么修。

站点真实回执文案（实测）：
  jianzhile.vip：缺字段 `请输入验证码`；答案错 `验证码错误，请重试`；id 复用 `验证码已失效，请刷新后重试`
  sheapi.top   ：缺字段 `图形验证码不能为空`；答案错 `图形验证码错误或已过期`
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from accounts_store import site_config_from_mapping
from browser import script_loader
from providers.base import ApiError, AuthInfo, CheckinReward
from providers.profiles import newapi as NA

SCRIPT_PATH = "scripts/newapi_captcha.py"
captcha_script = script_loader.load_site_script(SCRIPT_PATH)


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


def _ok(data: Any) -> dict:
    return {"success": True, "data": data}


def _checkin_captcha(captcha_id: str = "cid1") -> dict:
    """jianzhile 方言的取图回执（图内容无所谓：识别在测试里被替换）。"""
    return _ok({"captcha_id": captcha_id, "captcha_image": "data:image/png;base64,AA==",
                "expires_at": "1"})


def _scene_captcha(captcha_id: str = "uuid-1") -> dict:
    """sheapi 方言的取图回执：字段名是 image，不是 captcha_image。"""
    return _ok({"captcha_id": captcha_id, "image": "data:image/png;base64,AA==",
                "expires_in": 120})


def _stub_solver(monkeypatch, answers, exact=True):
    """把识别器替换成按序返回的假实现。

    替换的是脚本的 `solve_image`（「给我 dataURL，返回答案与是否可信」）而不是某个
    具体识别模块：脚本按图像尺寸在 newapi_bitmap / base64_captcha 之间派发，替换单个
    模块拦不住，而且这个函数正是脚本真正依赖的那道边界。
    """
    seq = list(answers)
    monkeypatch.setattr(
        captcha_script, "solve_image", lambda _url, log=None: (seq.pop(0), exact)
    )
    monkeypatch.setattr(captcha_script, "solver_available", lambda: True)
    return seq


def _endpoints(client: FakeClient) -> list[str]:
    return [path for _m, path, _b in client.calls if path != NA_CHECKIN and "captcha" in path]


NA_CHECKIN = "/api/user/checkin"
DIALECT_A, DIALECT_B = captcha_script.DIALECTS


# ── 开关探测：两套方言把它放在不同地方 ────────────────────────────────────────
def test_captcha_flag_from_checkin_status() -> None:
    """jianzhile 系：签到状态里的 captcha_enabled。"""
    client = FakeClient([_ok({"captcha_enabled": True, "stats": {"checked_in_today": False}})])
    assert captcha_script.captcha_required(client) is True


def test_captcha_flag_from_site_status() -> None:
    """sheapi 系：签到状态里没有该字段，开关只在 /api/status。

    只看签到状态会漏判 —— 实测就是这样一路走到用错端点、报 Invalid URL。
    """
    client = FakeClient([
        _ok({"enabled": True, "stats": {"checked_in_today": False}}),
        _ok({"checkin_enabled": True, "checkin_captcha_enabled": True}),
    ])
    assert captcha_script.captcha_required(client) is True
    assert client.calls[-1][1] == captcha_script.STATUS_PATH


def test_captcha_flag_absent_means_not_required() -> None:
    client = FakeClient([
        _ok({"stats": {"checked_in_today": False}}),
        _ok({"checkin_enabled": True, "checkin_captcha_enabled": False}),
    ])
    assert captcha_script.captcha_required(client) is False


def test_status_lookup_failure_is_not_fatal() -> None:
    """两个探测端点都失败时按「不需要验证码」处理，交给签到回执兜底。"""
    client = FakeClient([ApiError(500, None, "boom"), ApiError(500, None, "boom")])
    assert captcha_script.captcha_required(client) is False


# ── 验证码签到主流程 ─────────────────────────────────────────────────────────
def test_submits_recognized_answer_with_dialect_field(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([_checkin_captcha("cid1"), _ok({"quota_awarded": 5000000})])
    data = captcha_script.captcha_checkin(client)

    assert data == {"quota_awarded": 5000000}
    assert client.calls[0][1] == DIALECT_A.endpoint
    assert client.calls[1] == ("POST", NA_CHECKIN, {"captcha_id": "cid1", "captcha_answer": "HRDA6"})


def test_wrong_answer_refetches_a_new_captcha(monkeypatch) -> None:
    """captcha_id 单次有效，所以重试必须重新取图而不是复用旧 id。"""
    _stub_solver(monkeypatch, ["AAAAA", "HRDA6"])
    client = FakeClient([
        _checkin_captcha("cid1"),
        ApiError(None, None, "验证码错误，请重试"),
        _checkin_captcha("cid2"),
        _ok({"quota_awarded": 1}),
    ])
    captcha_script.captcha_checkin(client)

    assert len(_endpoints(client)) == 2, "每次重试都应重新取图"
    submits = [b for _m, path, b in client.calls if path == NA_CHECKIN]
    assert [s["captcha_id"] for s in submits] == ["cid1", "cid2"]


def test_uncertain_recognition_skips_submission(monkeypatch) -> None:
    """识别不确定时应换图而不是硬猜：取图不消耗签到机会，猜错却会作废一次。"""
    _stub_solver(monkeypatch, ["A", "B", "C", "HRDA6"], exact=False)
    script = [_checkin_captcha(f"c{i}") for i in range(captcha_script.MAX_ATTEMPTS)]
    script.append(_ok({"quota_awarded": 1}))
    client = FakeClient(script)
    captcha_script.captcha_checkin(client)

    submits = [b for _m, path, b in client.calls if path == NA_CHECKIN]
    assert len(submits) == 1, "只有用到最后一次机会时才带着不确定的读数提交"
    assert submits[0]["captcha_id"] == f"c{captcha_script.MAX_ATTEMPTS - 1}"


def test_exhausted_attempts_reports_what_was_tried(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["AAAAA"] * captcha_script.MAX_ATTEMPTS)
    script: list[Any] = []
    for i in range(captcha_script.MAX_ATTEMPTS):
        script.append(_checkin_captcha(f"c{i}"))
        script.append(ApiError(None, None, "验证码错误，请重试"))
    client = FakeClient(script)

    with pytest.raises(ApiError) as exc:
        captcha_script.captcha_checkin(client)
    assert "AAAAA" in exc.value.message
    assert "验证码错误" in exc.value.message


def test_non_captcha_error_is_not_retried(monkeypatch) -> None:
    """非验证码类错误（如未登录）不该被当成「换张图再试」。"""
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([_checkin_captcha("cid1"), ApiError(401, None, "Unauthorized, not logged in")])

    with pytest.raises(ApiError) as exc:
        captcha_script.captcha_checkin(client)
    assert exc.value.status == 401
    assert len(_endpoints(client)) == 1


def test_missing_captcha_fields_are_reported() -> None:
    client = FakeClient([_ok({"expires_at": "1"})])
    with pytest.raises(ApiError, match="captcha_id"):
        captcha_script.captcha_checkin(client)


def test_solver_unavailable_degrades_with_actionable_message(monkeypatch) -> None:
    monkeypatch.setattr(captcha_script, "solver_available", lambda: False)
    with pytest.raises(ApiError, match="识别器不可用"):
        captcha_script.captcha_checkin(FakeClient([]))


# ── 方言探测 ─────────────────────────────────────────────────────────────────
def test_scene_dialect_is_used_when_first_endpoint_is_absent(monkeypatch) -> None:
    """sheapi 系没有 /api/user/checkin/captcha，应改走 /api/captcha?scene=checkin。

    实测该站对前者回 `Invalid URL (POST /api/user/checkin/captcha)`。
    """
    _stub_solver(monkeypatch, ["6459"])
    client = FakeClient([
        ApiError(404, {"error": {"message": "Invalid URL (POST /api/user/checkin/captcha)"}},
                 "Invalid URL (POST /api/user/checkin/captcha)"),
        _scene_captcha("uuid-1"),
        _ok({"quota_awarded": 164231}),
    ])
    data = captcha_script.captcha_checkin(client)

    assert data == {"quota_awarded": 164231}
    assert _endpoints(client) == [DIALECT_A.endpoint, DIALECT_B.endpoint]
    submit = [b for _m, path, b in client.calls if path == NA_CHECKIN][0]
    assert submit == {"captcha_id": "uuid-1", "captcha_code": "6459"}, "scene 方言的答案字段是 captcha_code"


def test_dialect_is_remembered_across_retries(monkeypatch) -> None:
    """命中一次后不该再对已知失败的端点试错，否则每次重试都多一个 404 往返。"""
    _stub_solver(monkeypatch, ["AAAA", "6459"])
    client = FakeClient([
        ApiError(404, None, "Invalid URL"),
        _scene_captcha("u1"),
        ApiError(None, None, "图形验证码错误或已过期"),
        _scene_captcha("u2"),
        _ok({"quota_awarded": 1}),
    ])
    captcha_script.captcha_checkin(client)

    assert _endpoints(client) == [DIALECT_A.endpoint, DIALECT_B.endpoint, DIALECT_B.endpoint]


def test_business_error_while_probing_is_not_swallowed(monkeypatch) -> None:
    """取图被拒于未登录时必须原样抛出，不能掩盖成「站点不支持验证码」。"""
    _stub_solver(monkeypatch, ["6459"])
    client = FakeClient([ApiError(401, None, "Unauthorized, not logged in")])
    with pytest.raises(ApiError) as exc:
        captcha_script.captcha_checkin(client)
    assert exc.value.status == 401


def test_no_known_dialect_reports_endpoints_tried(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["6459"])
    client = FakeClient([
        ApiError(404, None, "Invalid URL"),
        ApiError(200, None, "验证码场景无效"),
    ])
    with pytest.raises(ApiError, match="未提供已知的签到验证码端点"):
        captcha_script.captcha_checkin(client)


# ── 脚本钩子 do_checkin ──────────────────────────────────────────────────────
def test_hook_returns_none_when_site_needs_no_captcha() -> None:
    """返回 None = 「本站不需要我接管」，通用层照原样走默认签到。"""
    client = FakeClient([
        _ok({"stats": {"checked_in_today": False}}),
        _ok({"checkin_captcha_enabled": False}),
    ])
    assert captcha_script.do_checkin(client) is None


def test_hook_returns_reward_with_awarded_quota(monkeypatch) -> None:
    _stub_solver(monkeypatch, ["HRDA6"])
    client = FakeClient([
        _ok({"captcha_enabled": True, "stats": {"checked_in_today": False}}),
        _checkin_captcha("cid1"),
        _ok({"quota_awarded": 5000000, "quota": 12345}),
    ])
    reward = captcha_script.do_checkin(client)

    assert isinstance(reward, CheckinReward)
    assert (reward.quota_awarded, reward.current_quota) == (5000000, 12345)


# ── api action 的接线 ───────────────────────────────────────────────────────
def test_action_skips_hook_without_script_path(monkeypatch) -> None:
    """纯 API 站点（没填脚本路径）不该尝试加载脚本。"""
    from providers.actions import api as api_action

    def explode(_path: str) -> Any:
        raise AssertionError("没有 script 时不该加载脚本")

    monkeypatch.setattr(script_loader, "load_site_script", explode)
    assert api_action._script_checkin(_site(), FakeClient([]), "") is None


def test_action_uses_script_reward(monkeypatch) -> None:
    from providers.actions import api as api_action

    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: captcha_script)
    monkeypatch.setattr(captcha_script, "do_checkin",
                        lambda _client, log=None: CheckinReward(quota_awarded=42))
    reward = api_action._script_checkin(_site(script=SCRIPT_PATH), FakeClient([]), "")
    assert reward is not None and reward.quota_awarded == 42


def test_action_falls_back_when_script_declines(monkeypatch) -> None:
    from providers.actions import api as api_action

    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: captcha_script)
    monkeypatch.setattr(captcha_script, "do_checkin", lambda _client, log=None: None)
    assert api_action._script_checkin(_site(script=SCRIPT_PATH), FakeClient([]), "") is None


def test_action_falls_back_when_script_cannot_load(monkeypatch, capsys) -> None:
    """脚本路径写错不该让签到直接失败 —— 回落默认流程并把原因打进日志。"""
    from providers.actions import api as api_action

    def boom(_path: str) -> Any:
        raise script_loader.ScriptLoadError("脚本文件不存在：scripts/checkin/nope.py")

    monkeypatch.setattr(script_loader, "load_site_script", boom)
    assert api_action._script_checkin(_site(script="scripts/checkin/nope.py"), FakeClient([]), "") is None
    assert "加载站点脚本失败" in capsys.readouterr().err


def test_action_lets_script_errors_propagate_for_classification(monkeypatch) -> None:
    """脚本抛的 ApiError 必须原样上抛，由 classify 归类（验证码类 → need_verification）。"""
    from providers.actions import api as api_action

    def boom(_client: Any, log: Any = None) -> Any:
        raise ApiError(None, None, "图形验证码连续 4 次未通过")

    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: captcha_script)
    monkeypatch.setattr(captcha_script, "do_checkin", boom)
    with pytest.raises(ApiError, match="图形验证码"):
        api_action._script_checkin(_site(script=SCRIPT_PATH), FakeClient([]), "")


def test_captcha_failure_is_classified_as_need_verification() -> None:
    """缺/错验证码属于人机验证，不该被 LOGIN_PATTERNS 里的宽泛词吃成 need_login。"""
    client = FakeClient([])
    assert client.classify(ApiError(None, None, "图形验证码不能为空")) == "need_verification"
    assert client.classify(ApiError(None, None, "验证码错误，请重试")) == "need_verification"
    # 反向保护：登录类报错不能因为新增词表而被误判成人机验证
    assert client.classify(ApiError(401, None, "token 验证失败")) == "need_login"


# ── 自动路由漏判验证码：报错要说清怎么修 ─────────────────────────────────────
def test_missing_detection_yields_actionable_hint() -> None:
    """公开配置漏报时应提示 verification_mode，而不是要求填写内置脚本。"""
    client = FakeClient([ApiError(None, None, "图形验证码不能为空")], api_variant="legacy")
    with pytest.raises(ApiError) as exc:
        client.do_checkin()
    assert NA.CAPTCHA_MODE_HINT in exc.value.message
    assert "图形验证码不能为空" in exc.value.message, "服务端原文要保留，便于核对"
