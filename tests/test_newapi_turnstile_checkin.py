# -*- coding: utf-8 -*-
"""New API Cloudflare Turnstile 签到链路回归（脚本接线 + 诊断消息 + 超时预算）。

覆盖的是「怎么把浏览器签发的令牌接到 HTTP 签到上」，不含 Cloudflare 求解本身
（求解在 browser/turnstile.py，其响应性由 test_turnstile_responsiveness.py 覆盖）：

- 脚本 scripts/newapi_turnstile.py：站点是否启用 Turnstile 的判定、
  令牌拿到后走 legacy 提交、求解失败按可重试错误上抛；
- newapi profile：没配脚本却撞上 Turnstile 时，报错要说清怎么修；
- run__all_checkin：api + 脚本的组合要拿到浏览器级任务预算，否则浏览器
  启动就会把 HTTP_TASK 撑爆。

站点真实回执文案（实测 gorouter.app，New API v1.0.0-rc.21）：
  缺令牌 `Turnstile token 为空`；令牌无效 `Turnstile 校验失败，请刷新重试！`
  该 fork 无 /api/user/checkin/challenge 端点（404），只能走 legacy + turnstile。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from accounts_store import site_config_from_mapping
from browser import script_loader
from providers.base import ApiError, AuthInfo
from providers.profiles import newapi as NA

SCRIPT_PATH = "scripts/newapi_turnstile.py"
turnstile_script = script_loader.load_site_script(SCRIPT_PATH)


def _site(**extra: Any):
    base = {
        "name": "t", "base_url": "https://t.invalid", "site_profile": "newapi",
        "auth_method": "access_token", "access_token": "tok", "user_id": "1",
        "enabled": True,
    }
    base.update(extra)
    return site_config_from_mapping(base)


class FakeClient(NA.NewApiClient):
    """替换 request()，用脚本化回放代替真实 HTTP。"""

    def __init__(self, script: list[Any], **site_kw: Any) -> None:
        super().__init__(_site(**site_kw), AuthInfo(access_token="tok", new_api_user="1"))
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


def _status(**extra: Any) -> dict:
    """/api/status 的最小可用回执。"""
    data = {"turnstile_check": True, "turnstile_site_key": "0x4AAA_test_key"}
    data.update(extra)
    return _ok(data)


def _stub_solver(monkeypatch: pytest.MonkeyPatch, token: str) -> None:
    """把浏览器求解替换成固定令牌。

    同时替换 _solve_turnstile 与 run_sync：只替换后者会留下一个从未被 await 的
    协程对象（RuntimeWarning），把「测试自己造的噪音」混进真实告警里。
    """
    monkeypatch.setattr(turnstile_script, "_solve_turnstile", lambda *_a, **_k: token)
    monkeypatch.setattr(turnstile_script, "run_sync", lambda value: value)


# ── 站点是否启用 Turnstile 的判定 ─────────────────────────────────────────────
def test_script_skips_when_turnstile_disabled() -> None:
    """站点没开 Turnstile：脚本必须不接管，把签到还给默认 HTTP 流程。"""
    client = FakeClient([_status(turnstile_check=False, turnstile_site_key="")])
    assert turnstile_script.do_checkin(client) is None


def test_script_skips_when_sitekey_missing() -> None:
    """开了开关但没给 sitekey：无法渲染 widget，同样让默认流程接管。"""
    client = FakeClient([_status(turnstile_site_key="")])
    assert turnstile_script.do_checkin(client) is None


def test_script_skips_when_status_unreadable() -> None:
    """/api/status 读不到不能让整站签到失败：判为「未启用」并回落默认流程。"""
    client = FakeClient([ApiError(500, None, "boom")])
    assert turnstile_script.do_checkin(client) is None


# ── 拿到令牌后的提交路径 ─────────────────────────────────────────────────────
def test_script_submits_token_via_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """令牌必须以 ?turnstile= 提交 legacy 端点，且不再触碰 challenge 端点。

    该 fork 的 challenge 端点返回 404，走 auto 会白费一次往返。
    """
    _stub_solver(monkeypatch, "TOKEN-OK")
    client = FakeClient([
        _status(),
        _ok({"quota_awarded": 4154148, "checkin_date": "2026-08-07"}),
    ])
    reward = turnstile_script.do_checkin(client)

    assert reward is not None
    assert reward.quota_awarded == 4154148
    methods_paths = [(m, p) for m, p, _ in client.calls]
    assert methods_paths[0] == ("GET", "/api/status")
    assert methods_paths[1][0] == "POST"
    assert "turnstile=TOKEN-OK" in methods_paths[1][1]
    assert not any("challenge" in p for _, p in methods_paths), "不应触碰 challenge 端点"


def test_script_restores_api_variant(monkeypatch: pytest.MonkeyPatch) -> None:
    """强制 legacy 只是本次提交的手段，不能污染站点配置。"""
    _stub_solver(monkeypatch, "TOKEN-OK")
    client = FakeClient([_status(), _ok({"quota_awarded": 1})], api_variant="auto")
    turnstile_script.do_checkin(client)
    assert client.site.api_variant == "auto"


def test_script_restores_api_variant_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """签到抛错时同样要还原，否则后续重试会被永久钉在 legacy。"""
    _stub_solver(monkeypatch, "TOKEN-OK")
    client = FakeClient(
        [_status(), ApiError(None, None, "Turnstile 校验失败，请刷新重试！")],
        api_variant="auto",
    )
    with pytest.raises(ApiError):
        turnstile_script.do_checkin(client)
    assert client.site.api_variant == "auto"


def test_script_raises_transient_when_token_unsolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """求解失败是环境/风控问题，应归为可重试，而不是把当天签到判死。"""
    _stub_solver(monkeypatch, "")
    client = FakeClient([_status()])
    with pytest.raises(ApiError) as exc:
        turnstile_script.do_checkin(client)
    assert exc.value.transient is True


# ── 分类与诊断消息 ───────────────────────────────────────────────────────────
def test_turnstile_failure_is_classified_as_need_verification() -> None:
    """缺/错令牌属于人机验证，不能被 LOGIN_PATTERNS 里的宽泛 "token" 吃成 need_login。"""
    client = FakeClient([])
    assert client.classify(ApiError(None, None, "Turnstile token 为空")) == "need_verification"
    assert client.classify(ApiError(None, None, "Turnstile 校验失败，请刷新重试！")) == "need_verification"
    # 反向保护：登录类报错不能因为新增词表被误判成人机验证
    assert client.classify(ApiError(401, None, "token 验证失败")) == "need_login"


def test_default_variant_skips_node_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认 legacy 不该启动 Node challenge 子进程（每站约 1.2s 且必然 404）。"""
    client = FakeClient([_ok({"quota_awarded": 7})])

    def _boom() -> Any:
        raise AssertionError("默认路径不应调用 challenge")

    monkeypatch.setattr(client, "_challenge_checkin", _boom)
    assert client.site.api_variant == "legacy"
    assert client.do_checkin().quota_awarded == 7


def test_legacy_still_falls_back_to_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    """站点提示流程已升级时，legacy 必须自动切 challenge —— 这是保留该模式的意义。"""
    client = FakeClient([ApiError(None, None, "签到接口已升级，请使用新版流程")])
    monkeypatch.setattr(
        client, "_challenge_checkin", lambda: {"quota_awarded": 99}
    )
    assert client.do_checkin().quota_awarded == 99


def test_explicit_auto_tries_challenge_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式选 auto 仍按 challenge 优先，改默认不等于删能力。"""
    client = FakeClient([], api_variant="auto")
    monkeypatch.setattr(
        client, "_challenge_checkin", lambda: {"quota_awarded": 5}
    )
    assert client.do_checkin().quota_awarded == 5


def test_missing_detection_yields_actionable_hint() -> None:
    """公开配置漏报时应提示 turnstile 机制，而不是要求填写内置脚本。"""
    client = FakeClient(
        [ApiError(None, None, "Turnstile token 为空")], api_variant="legacy"
    )
    with pytest.raises(ApiError) as exc:
        client.do_checkin()
    assert NA.TURNSTILE_MODE_HINT in exc.value.message
    assert "Turnstile token 为空" in exc.value.message, "服务端原文要保留，便于核对"


# ── 任务预算 ─────────────────────────────────────────────────────────────────
def _budget_for(monkeypatch: pytest.MonkeyPatch, **site_extra: Any) -> float:
    """构造单站点配置，返回 run__all_checkin 分配给它的任务超时预算。"""
    import run__all_checkin as runner

    site = {
        "name": "t", "base_url": "https://t.invalid", "site_profile": "newapi",
        "auth_method": "access_token", "access_token": "tok", "user_id": "1",
        "checkin_action": "api", "enabled": True,
    }
    site.update(site_extra)
    monkeypatch.setattr(
        runner.accounts_store, "load_unified_accounts", lambda **_kwargs: [site]
    )
    tasks = runner.build_site_tasks()
    assert len(tasks) == 1
    return tasks[0].timeout


def test_api_with_script_gets_browser_task_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """api + 脚本可能要启动浏览器；沿用 HTTP_TASK 会在浏览器启动阶段就超时。"""
    from config import Timeouts

    assert _budget_for(monkeypatch, script=SCRIPT_PATH) == Timeouts.BROWSER_TASK


def test_api_without_script_gets_router_browser_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认验证路由可能按需启动 Turnstile 浏览器，因此无显式脚本也要留足硬上限。"""
    from config import Timeouts

    assert _budget_for(monkeypatch) == Timeouts.BROWSER_TASK


# ── 求解时序与人工介入 ───────────────────────────────────────────────────────
_READY_SLOT = {"x": 40, "y": 120, "w": 300, "h": 71.5}


class FakeMouse:
    def __init__(self, sink: list[str]) -> None:
        self.sink = sink

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.sink.append(f"move({x:.0f},{y:.0f})")

    async def click(self, x: float, y: float) -> None:
        self.sink.append(f"click({x:.0f},{y:.0f})")


class FakePage:
    """按脚本回放 widget 状态；记录鼠标动作与等待次数。"""

    def __init__(self, states: list[dict]) -> None:
        self.states = states
        self.actions: list[str] = []
        self.mouse = FakeMouse(self.actions)
        self.waits = 0
        self.reads = 0

    async def evaluate(self, _js: str, *_args: Any) -> Any:
        self.reads += 1
        # 状态用尽后重复最后一个，模拟「一直停在该状态」
        idx = min(self.reads - 1, len(self.states) - 1)
        return self.states[idx]

    async def wait_for_timeout(self, _ms: int) -> None:
        self.waits += 1


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def test_click_happens_immediately_when_widget_ready() -> None:
    """可见 widget 挂载后应立即自动点击，不等待额外观察窗。"""
    page = FakePage([
        {"state": "rendered", "token": "", "error": "", "slot": _READY_SLOT},
        {"state": "rendered", "token": "TOK", "error": "", "slot": _READY_SLOT},
    ])
    token, _ = _run(turnstile_script._one_attempt(page, 1, log=lambda _m: None))
    assert token == "TOK"
    assert page.waits == 0
    assert "click(70,156)" in page.actions
    # Camoufox 会把每个 steps 再人类化一次；生产值必须保持经 A/B 验证的最小 2/2。
    moves = [action for action in page.actions if action.startswith("move(")]
    assert len(moves) == 2


def test_auto_phase_gives_up_after_error_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误只宽限短时观察，不进入人工阶段，也不空耗整个任务预算。"""
    monkeypatch.setattr(turnstile_script, "_ERROR_GRACE_MS", 0)
    page = FakePage([
        {"state": "rendered", "token": "", "error": "", "slot": _READY_SLOT},
        {"state": "error", "token": "", "error": "600010", "slot": _READY_SLOT},
    ])
    token, reason = _run(turnstile_script._one_attempt(page, 1, log=lambda _m: None))
    assert token == ""
    assert "600010" in reason


def test_attempt_bails_when_container_lost() -> None:
    """容器丢失说明页面已跳转，继续等没有意义。"""
    page = FakePage([{"state": "missing", "token": "", "error": "", "slot": None}])
    token, reason = _run(turnstile_script._one_attempt(page, 1, log=lambda _m: None))
    assert token == ""
    assert "容器丢失" in reason


def test_token_read_before_click() -> None:
    """managed/invisible 模式若已自动签发，不应再产生鼠标动作。"""
    page = FakePage([
        {"state": "done", "token": "FIELD-TOK", "error": "", "slot": _READY_SLOT},
    ])
    token, _ = _run(turnstile_script._one_attempt(page, 1, log=lambda _m: None))
    assert token == "FIELD-TOK"
    assert page.actions == []


def test_state_js_scans_response_fields() -> None:
    """callback 未触发时也必须读取 Turnstile 自建的隐藏字段。"""
    assert "cf-turnstile-response" in turnstile_script._STATE_JS
    assert "data-token" in turnstile_script._STATE_JS


def test_solver_has_no_manual_phase() -> None:
    """当前要求全程自动化：脚本不应再保留人工等待入口。"""
    assert not hasattr(turnstile_script, "_manual_wait_ms")
    assert "CHECKIN_TURNSTILE_MANUAL_WAIT" not in Path(SCRIPT_PATH).read_text(encoding="utf-8")
