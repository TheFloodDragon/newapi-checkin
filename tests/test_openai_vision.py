from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from browser.openai_vision import (
    OpenAIVisionClient,
    VisionClientConfig,
    VisionClientError,
    VisionPlan,
    parse_json_object,
)
from providers.base import ApiError


def _response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def test_config_precedence_and_option_api_key_is_ignored() -> None:
    env = {
        "HCAPTCHA_OPENAI_API_KEY": "hcaptcha-key",
        "OPENAI_API_KEY": "openai-key",
        "HCAPTCHA_OPENAI_BASE_URL": "https://env-h.example/v1",
        "OPENAI_BASE_URL": "https://env-o.example/v1",
        "HCAPTCHA_OPENAI_MODEL": "env-h-model",
        "OPENAI_MODEL": "env-o-model",
    }
    config = VisionClientConfig.from_options(
        {"api_key": "must-not-be-used", "base_url": "https://option.example/v1", "model": "option-model"},
        environ=env,
    )

    assert config.api_key == "hcaptcha-key"
    assert config.base_url == "https://option.example/v1"
    assert config.model == "option-model"
    assert "hcaptcha-key" not in repr(config)


def test_config_environment_fallbacks_and_missing_key() -> None:
    config = VisionClientConfig.from_options(
        SimpleNamespace(api_key="ignored", base_url="", model=""),
        environ={
            "OPENAI_API_KEY": "openai-key",
            "OPENAI_BASE_URL": "https://openai-compatible.example/v1",
            "OPENAI_MODEL": "vision-model",
        },
    )
    assert config.api_key == "openai-key"
    assert config.base_url == "https://openai-compatible.example/v1"
    assert config.model == "vision-model"

    with pytest.raises(VisionClientError, match="API key"):
        VisionClientConfig.from_options({"api_key": "option-secret"}, environ={})


def test_root_local_config_has_highest_runtime_precedence(tmp_path, monkeypatch) -> None:
    local = tmp_path / "HCAPTCHA_VISION_CONFIG.json"
    local.write_text(
        json.dumps(
            {
                "api_key": "local-key",
                "base_url": "https://local.example/v1",
                "model": "local-model",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("browser.openai_vision._LOCAL_CONFIG_PATH", local)

    config = VisionClientConfig.from_options()

    assert config.api_key == "local-key"
    assert config.base_url == "https://local.example/v1"
    assert config.model == "local-model"


def test_single_json_secret_has_highest_precedence() -> None:
    config = VisionClientConfig.from_options(
        {
            "base_url": "https://option.example/v1",
            "model": "option-model",
            "timeout_ms": 9_000,
            "max_actions": 3,
        },
        environ={
            "HCAPTCHA_VISION_CONFIG": json.dumps(
                {
                    "OPENAI_API_KEY": "json-secret-key",
                    "baseUrl": "https://json.example/v1",
                    "model": "json-model",
                    "timeoutMs": 12_000,
                    "maxActions": 7,
                }
            ),
            "HCAPTCHA_OPENAI_API_KEY": "legacy-key",
            "HCAPTCHA_OPENAI_BASE_URL": "https://legacy.example/v1",
            "HCAPTCHA_OPENAI_MODEL": "legacy-model",
        },
    )

    assert config.api_key == "json-secret-key"
    assert config.base_url == "https://json.example/v1"
    assert config.model == "json-model"
    assert config.timeout == 12
    assert config.max_actions == 7
    assert "json-secret-key" not in repr(config)


@pytest.mark.parametrize("raw", ["not-json", "[]", '"secret"'])
def test_single_json_secret_rejects_invalid_shape_without_leaking_value(raw: str) -> None:
    with pytest.raises(VisionClientError) as caught:
        VisionClientConfig.from_options(environ={"HCAPTCHA_VISION_CONFIG": raw})

    assert raw not in str(caught.value)


def test_parse_direct_fenced_and_first_balanced_json() -> None:
    expected = {"challenge_type": "unknown", "confidence": 0}
    assert parse_json_object(json.dumps(expected)) == expected
    assert parse_json_object(f"```json\n{json.dumps(expected)}\n```") == expected
    assert parse_json_object(f"explanation before {json.dumps(expected)} explanation after {{bad}}") == expected
    assert parse_json_object('prefix {"note": "a } brace", "nested": {"ok": true}} suffix')["nested"] == {"ok": True}

    with pytest.raises(VisionClientError, match="valid JSON"):
        parse_json_object("no object here")


def test_irrelevant_field_is_ignored_but_relevant_field_still_validated() -> None:
    """无关字段不得否决有效计划；该类型真正需要的字段仍严格校验。"""
    plan = VisionPlan.from_mapping(
        {
            "challenge_type": "drag",
            "confidence": 0.95,
            "drags": [{"end": {"x": 431, "y": 830}, "start": {"x": 753, "y": 475}}],
            "tile_indices": {"source": [8, 5], "target": [4, 8]},
        }
    )
    assert plan.drags == [{"start": {"x": 753, "y": 475}, "end": {"x": 431, "y": 830}}]
    assert plan.tile_indices == []

    with pytest.raises(VisionClientError, match="tile_indices must be an array"):
        VisionPlan.from_mapping(
            {"challenge_type": "grid", "confidence": 1, "tile_indices": {"a": 1}}
        )
    with pytest.raises(VisionClientError, match="1-based positive integers"):
        VisionPlan.from_mapping(
            {"challenge_type": "grid", "confidence": 1, "tile_indices": [0, -1]}
        )


def test_plan_accepts_elements_key_and_nested_point_wrappers() -> None:
    """与 hcaptcha 层一致：接受 elements 键与嵌套 point，但仍校验范围。"""
    plan = VisionPlan.from_mapping(
        {
            "challenge_type": "drag",
            "confidence": 0.95,
            "elements": [{"end": {"point": [550, 350]}, "start": {"point": [850, 390]}}],
        }
    )
    assert plan.drags == [{"start": {"x": 850, "y": 390}, "end": {"x": 550, "y": 350}}]

    point_plan = VisionPlan.from_mapping(
        {"challenge_type": "point", "confidence": 0.9, "points": [{"point": [181, 761]}]}
    )
    assert point_plan.points == [{"x": 181, "y": 761}]

    with pytest.raises(VisionClientError, match="between 0 and 1000"):
        VisionPlan.from_mapping(
            {
                "challenge_type": "drag",
                "confidence": 1,
                "elements": [{"start": {"point": [1200, 10]}, "end": {"point": [5, 5]}}],
            }
        )


def test_plan_validation_accepts_supported_actions() -> None:
    plan = VisionPlan.from_mapping(
        {
            "challenge_type": "drag",
            "confidence": 0.75,
            "drags": [{"start": {"x": 100.5, "y": 200}, "end": {"x": 900, "y": 800}}],
        },
        max_actions=4,
    )
    assert plan.tile_indices == []
    assert plan.points == []
    assert plan.drags[0]["end"] == {"x": 900, "y": 800}
    assert plan.model_dump()["type"] == "drag"
    assert plan.model_dump()["action"] == plan.drags[0]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"challenge_type": "click"}, "challenge_type"),
        ({"confidence": 1.1}, "confidence"),
        ({"tile_indices": [0]}, "1-based"),
        ({"points": [{"x": -1, "y": 2}]}, "between 0 and 1000"),
        ({"drags": [{"start": {"x": 0, "y": 0}, "end": {"x": 1001, "y": 1}}]}, "between 0 and 1000"),
    ],
)
def test_plan_validation_rejects_invalid_values(change: dict, message: str) -> None:
    raw = {"challenge_type": "grid", "confidence": 0.5, "tile_indices": [], "points": [], "drags": []}
    raw.update(change)
    with pytest.raises(VisionClientError, match=message):
        VisionPlan.from_mapping(raw)


def test_plan_validation_limits_total_actions() -> None:
    with pytest.raises(VisionClientError, match="maximum is 2"):
        VisionPlan.from_mapping(
            {
                "challenge_type": "grid",
                "confidence": 1,
                "tile_indices": [1, 2],
                "points": [{"x": 1, "y": 2}],
            },
            max_actions=2,
        )


def test_client_uses_to_thread_and_builds_chat_completions_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict]] = []
    to_thread_calls: list[object] = []

    def transport(url: str, **kwargs):
        calls.append((url, kwargs))
        return _response('{"challenge_type":"grid","confidence":0.9,"tile_indices":[2]}')

    async def fake_to_thread(function, /, *args, **kwargs):
        to_thread_calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", fake_to_thread)
    client = OpenAIVisionClient(
        VisionClientConfig(api_key="secret-key", base_url="https://compatible.example/v1/", model="vision"),
        transport=transport,
    )
    plan = asyncio.run(client.analyze(b"png", prompt="pick matching tiles"))

    assert plan.tile_indices == [2]
    assert to_thread_calls == [transport]
    assert calls[0][0] == "https://compatible.example/v1/chat/completions"
    assert calls[0][1]["method"] == "POST"
    assert calls[0][1]["max_attempts"] == 1
    assert "retry_non_idempotent" not in calls[0][1]
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret-key"
    request = json.loads(calls[0][1]["body"])
    assert request["model"] == "vision"
    assert request["response_format"] == {"type": "json_object"}
    image = request["messages"][1]["content"][1]["image_url"]["url"]
    assert image == "data:image/png;base64,cG5n"


def test_grid_image_is_sent_as_second_image_with_instruction() -> None:
    captured: list[dict] = []

    def transport(_url: str, **kwargs):
        captured.append(json.loads(kwargs["body"]))
        return _response('{"challenge_type":"point","confidence":0.9,"points":[{"x":10,"y":20}]}')

    client = OpenAIVisionClient(
        VisionClientConfig(api_key="k", base_url="https://v.example/v1", model="m"),
        transport=transport,
    )
    plan = asyncio.run(
        client.solve_hcaptcha(image=b"main", task_type="point", grid_image=b"grid", round=1)
    )

    assert plan.challenge_type == "point"
    content = captured[0]["messages"][1]["content"]
    images = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
    assert len(images) == 2
    assert images[0] == "data:image/png;base64,bWFpbg=="
    assert images[1] == "data:image/png;base64,Z3JpZA=="
    texts = " ".join(part["text"] for part in content if part["type"] == "text")
    assert "coordinate grid" in texts


def test_analyze_without_grid_image_sends_single_image() -> None:
    captured: list[dict] = []

    def transport(_url: str, **kwargs):
        captured.append(json.loads(kwargs["body"]))
        return _response('{"challenge_type":"unknown","confidence":0}')

    client = OpenAIVisionClient(
        VisionClientConfig(api_key="k", base_url="https://v.example/v1", model="m"),
        transport=transport,
    )
    asyncio.run(client.analyze(b"only"))

    content = captured[0]["messages"][1]["content"]
    assert sum(1 for part in content if part["type"] == "image_url") == 1


def test_hcaptcha_adapter_and_keyword_configuration() -> None:
    captured: list[dict] = []

    def transport(_url: str, **kwargs):
        captured.append(json.loads(kwargs["body"]))
        return _response('{"challenge_type":"grid","confidence":1,"tile_indices":[1]}')

    client = OpenAIVisionClient(
        model="option-model",
        base_url="https://option.example/v1",
        transport=transport,
        environ={"OPENAI_API_KEY": "env-key"},
    )
    plan = asyncio.run(
        client.solve_hcaptcha(
            image=b"png",
            prompt="select buses",
            task_type="grid",
            tiles=[{"index": 1}, {"index": 2}],
            round=2,
        )
    )

    assert plan.model_dump()["actions"] == [1]
    request_prompt = captured[0]["messages"][1]["content"][0]["text"]
    assert "select buses" in request_prompt
    assert "2 numbered tiles" in request_prompt


def test_response_format_400_retries_without_response_format() -> None:
    requests: list[dict] = []

    def transport(_url: str, **kwargs):
        body = json.loads(kwargs["body"])
        requests.append(body)
        if len(requests) == 1:
            raise ApiError(400, {"error": "unsupported response_format"}, "bad request secret-key")
        return _response("```json\n{\"challenge_type\":\"unknown\",\"confidence\":0}\n```")

    client = OpenAIVisionClient(VisionClientConfig(
            api_key="secret-key",
            base_url="https://compatible.example/v1",
            model="vision",
        ), transport=transport)
    plan = asyncio.run(client.analyze("aGVsbG8="))

    assert plan.challenge_type == "unknown"
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


def test_non_400_error_does_not_retry_or_leak_key() -> None:
    calls = 0

    def transport(_url: str, **_kwargs):
        nonlocal calls
        calls += 1
        raise ApiError(401, {"message": "secret-key invalid"}, "secret-key rejected")

    client = OpenAIVisionClient(VisionClientConfig(
            api_key="secret-key",
            base_url="https://compatible.example/v1",
            model="vision",
        ), transport=transport)
    with pytest.raises(VisionClientError) as caught:
        asyncio.run(client.analyze(b"image"))

    assert calls == 1
    assert caught.value.status == 401
    assert "secret-key" not in str(caught.value)
    assert "secret-key" not in repr(caught.value)


def test_cancel_waits_for_single_background_transport_to_finish() -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def transport(_url: str, **_kwargs):
        started.set()
        try:
            release.wait(timeout=2)
            return _response('{"challenge_type":"unknown","confidence":0}')
        finally:
            finished.set()

    client = OpenAIVisionClient(
        VisionClientConfig(
            api_key="secret-key",
            base_url="https://compatible.example/v1",
            model="vision",
            timeout=1,
        ),
        transport=transport,
    )

    async def scenario() -> None:
        task = asyncio.create_task(client.analyze(b"image"))
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()

    asyncio.run(scenario())


def test_second_failure_after_400_does_not_leak_key() -> None:
    calls = 0

    def transport(_url: str, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ApiError(400, None, "unsupported")
        raise RuntimeError("transport exposed secret-key")

    client = OpenAIVisionClient(VisionClientConfig(
            api_key="secret-key",
            base_url="https://compatible.example/v1",
            model="vision",
        ), transport=transport)
    with pytest.raises(VisionClientError) as caught:
        asyncio.run(client.analyze(b"image"))

    assert calls == 2
    assert "secret-key" not in str(caught.value)
