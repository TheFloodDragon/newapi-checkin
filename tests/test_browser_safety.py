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
