from __future__ import annotations

import asyncio
import base64
import gzip
import json

import pytest

from browser import session, state


def _valid_state() -> dict:
    return {
        "cookies": [
            {
                "name": "session",
                "value": "secret",
                "domain": ".example.invalid",
                "path": "/",
            }
        ],
        "origins": [
            {
                "origin": "https://example.invalid",
                "localStorage": [{"name": "token", "value": "value"}],
            }
        ],
    }


def test_oauth_capture_auto_finishes_only_with_authenticated_cookie(monkeypatch) -> None:
    class FakePage:
        url = "https://linux.do"

        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def title(self) -> str:
            return "Linux.do"

        async def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        async def new_page(self):
            return self.page

        async def cookies(self):
            return [{"name": "_t", "value": "token", "domain": ".linux.do", "path": "/"}]

        async def storage_state(self):
            return {"cookies": await self.cookies(), "origins": []}

    class FakeBrowser:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    browser = FakeBrowser()
    context = FakeContext()

    async def launch(**_kwargs):
        return browser, context

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(session.bypass, "launch_camoufox", launch)

    result = asyncio.run(session.capture_oauth_state("linuxdo", wait_for_close=wait_forever))

    assert result["ok"] is True
    assert state.decode_state(result["state"])["cookies"][0]["name"] == "_t"
    assert browser.closed is True


def test_oauth_capture_rejects_anonymous_provider_cookie(monkeypatch) -> None:
    class FakePage:
        url = "https://linux.do"

        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def cookies(self):
            return [{"name": "_forum_session", "value": "anonymous", "domain": ".linux.do"}]

        async def storage_state(self):
            raise AssertionError("匿名状态不应被导出")

    class FakeBrowser:
        async def close(self) -> None:
            return None

    async def launch(**_kwargs):
        return FakeBrowser(), FakeContext()

    async def finish_now() -> None:
        return None

    original_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(session.bypass, "launch_camoufox", launch)
    monkeypatch.setattr(session.asyncio, "sleep", fast_sleep)

    result = asyncio.run(session.capture_oauth_state("linuxdo", wait_for_close=finish_now))

    assert result["ok"] is False
    assert result["state"] == ""
    assert "有效认证 Cookie" in result["message"]


def test_state_roundtrip_and_schema_validation() -> None:
    encoded = state.encode_state(_valid_state())
    assert state.decode_state(encoded) == _valid_state()

    with pytest.raises(state.BrowserStateError):
        state.encode_state({"cookies": [{"name": 1, "value": "x"}]})
    with pytest.raises(state.BrowserStateError):
        state.decode_state("not base64!")
    with pytest.raises(state.BrowserStateError):
        state.decode_state(base64.b64encode(b"not-gzip").decode("ascii"))

    oversized = _valid_state()
    oversized["origins"][0]["localStorage"][0]["value"] = "x" * (4 * 1024 * 1024)
    packed = gzip.compress(json.dumps(oversized).encode("utf-8"), compresslevel=9)
    with pytest.raises(state.BrowserStateError, match="解压后数据过大"):
        state.decode_state(base64.b64encode(packed).decode("ascii"))


def test_restore_storage_state_isolates_local_storage_by_origin() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.cookie_calls: list[list[dict]] = []
            self.init_scripts: list[str] = []

        async def add_cookies(self, cookies: list[dict]) -> None:
            self.cookie_calls.append(cookies)

        async def add_init_script(self, script: str) -> None:
            self.init_scripts.append(script)

    cookies = _valid_state()["cookies"]
    storage_state = {
        "cookies": cookies,
        "origins": [
            {
                "origin": "https://one.invalid",
                "localStorage": [
                    {"name": "shared", "value": "one"},
                    {"name": "only_one", "value": "1"},
                ],
            },
            {
                "origin": "https://two.invalid",
                "localStorage": [
                    {"name": "shared", "value": "two"},
                    {"name": "only_two", "value": "2"},
                ],
            },
            {
                "origin": "https://one.invalid",
                "localStorage": [{"name": "shared", "value": "one-new"}],
            },
            {"origin": "", "localStorage": [{"name": "ignored", "value": "x"}]},
            {"origin": "https://empty.invalid", "localStorage": [{"name": "", "value": "x"}]},
        ],
    }
    context = FakeContext()

    asyncio.run(state.restore_storage_state(context, storage_state))

    assert context.cookie_calls == [cookies]
    assert len(context.init_scripts) == 1
    script = context.init_scripts[0]
    encoded_states = script.split("const states = ", 1)[1].split(";", 1)[0]
    states = json.loads(encoded_states)
    assert states == {
        "https://one.invalid": {"shared": "one-new", "only_one": "1"},
        "https://two.invalid": {"shared": "two", "only_two": "2"},
    }
    assert "const pairs = states[location.origin] || {};" in script
    assert "Object.entries(states)" not in script
    assert "only_two" not in states["https://one.invalid"]
    assert "only_one" not in states["https://two.invalid"]


def test_restore_storage_state_accepts_empty_state() -> None:
    class FakeContext:
        async def add_cookies(self, _cookies) -> None:
            raise AssertionError("空 cookies 不应调用 add_cookies")

        async def add_init_script(self, _script) -> None:
            raise AssertionError("空 origins 不应调用 add_init_script")

    asyncio.run(state.restore_storage_state(FakeContext(), {"cookies": [], "origins": []}))
    asyncio.run(state.restore_storage_state(FakeContext(), None))


def test_run_sync_inside_running_event_loop() -> None:
    async def outer() -> str:
        return session.run_sync(asyncio.sleep(0, result="ok"))

    assert asyncio.run(outer()) == "ok"


def test_run_sync_propagates_nested_exception() -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    async def outer() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            session.run_sync(fail())

    asyncio.run(outer())


def test_site_success_message_extracts_agentrouter_toast() -> None:
    assert session._site_success_message(["dom: 登录成功，今日奖励已发放"]) == "登录成功，今日奖励已发放"
    assert session._site_success_message(["dom: 今日已签到", "dom: 登录失败，请重试"]) == ""


def test_attach_site_errors_separates_success_toast_from_errors() -> None:
    target: dict = {}

    session._attach_site_errors(
        target,
        ["dom: 登录成功，今日奖励已发放", "response: HTTP 429 https://example.invalid/api/user"],
    )

    assert target["site_success_message"] == "登录成功，今日奖励已发放"
    assert target["site_errors"] == ["response: HTTP 429 https://example.invalid/api/user"]
    assert "登录成功" not in target["site_error"]


def test_wait_for_site_success_message_captures_delayed_toast() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, _script: str) -> list[str]:
            self.calls += 1
            return [] if self.calls == 1 else ["登录成功，获得每日额度"]

    page = FakePage()
    target: dict = {}
    message = asyncio.run(
        session._wait_for_site_success_message(
            page,
            {"items": [], "tasks": []},
            target,
            timeout_ms=500,
        )
    )

    assert message == "登录成功，获得每日额度"
    assert target["site_success_message"] == message
    assert page.calls == 2


def test_oauth_result_uses_success_toast_when_quota_is_unchanged() -> None:
    result = session._oauth_checkin_result(
        1_000_000,
        1_000_000,
        {"landed_back": True, "site_success_message": "登录成功，今日奖励已发放"},
    )

    assert result["status"] == "success"
    assert result["message"] == "签到成功（站点弹窗：登录成功，今日奖励已发放）。"


def test_oauth_result_without_success_toast_remains_already_done() -> None:
    result = session._oauth_checkin_result(1_000_000, 1_000_000, {"landed_back": True})

    assert result["status"] == "already_done"


# ── refresh_token 快照兜底 ───────────────────────────────────────────────────
# 背景（实测时序）：access_token 过期时 sub2api 前端收到 401 会清空 localStorage
# 再跳登录页，而 add_init_script 每次导航又重新注入，两者约每 2 秒来回竞争。
# 只读“活”存储会随机拿到空值，误报 refresh_token not found 并退化成开浏览器
# 重登。解码后的登录态快照是静态的，不受该竞争影响，必须作为兜底来源。

def _state_with_refresh(origin: str = "https://site.invalid", value: str = "rt_abc123") -> dict:
    return {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [
                    {"name": "auth_token", "value": "jwt"},
                    {"name": "refresh_token", "value": value},
                ],
            }
        ],
    }


def test_storage_refresh_token_reads_from_snapshot() -> None:
    assert session.storage_refresh_token(_state_with_refresh()) == "rt_abc123"


def test_storage_refresh_token_survives_cleared_live_storage() -> None:
    """活存储被前端清空（origins 里没有 refresh_token）时返回空，需由调用方回落快照。"""
    cleared = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://site.invalid",
                "localStorage": [{"name": "sub2api_site_usage_notice_v1", "value": "accepted"}],
            }
        ],
    }
    assert session.storage_refresh_token(cleared) == ""
    # 调用方的兜底表达式：活存储为空时取快照里的值
    snapshot = _state_with_refresh()
    assert (session.storage_refresh_token(cleared)
            or session.storage_refresh_token(snapshot)) == "rt_abc123"


def test_storage_refresh_token_handles_malformed_input() -> None:
    for bad in (None, {}, {"origins": None}, {"origins": [None]},
                {"origins": [{"localStorage": [None]}]},
                {"origins": [{"localStorage": [{"name": "refresh_token", "value": ""}]}]}):
        assert session.storage_refresh_token(bad) == ""
