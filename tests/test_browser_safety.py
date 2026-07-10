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
