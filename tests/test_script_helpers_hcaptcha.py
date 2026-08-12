# -*- coding: utf-8 -*-
"""ScriptHelpers hCaptcha 入口测试（不启动真实浏览器或模型请求）。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from browser import hcaptcha
from browser.script_helpers import ScriptHelpers


def _helpers(tmp_path: Path, script_args: dict[str, Any] | None = None, log=None) -> ScriptHelpers:
    site = SimpleNamespace(
        base_url="https://site.invalid",
        name="站点",
        script_args=script_args or {},
    )
    return ScriptHelpers(
        page=SimpleNamespace(),
        context=None,
        site=site,
        screenshot_dir=tmp_path,
        log=log,
    )


def test_helper_merges_site_options_and_call_overrides(monkeypatch: Any, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    expected = SimpleNamespace(ok=True, status="success", message="ok")

    async def fake_solve(page: Any, **kwargs: Any) -> Any:
        seen["page"] = page
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(hcaptcha, "solve", fake_solve)
    helper = _helpers(
        tmp_path,
        {
            "hcaptcha": {
                "enabled": True,
                "model": "site-model",
                "max_rounds": 2,
                "confidence_threshold": 0.65,
            }
        },
    )

    result = asyncio.run(
        helper.solve_hcaptcha(options={"model": "call-model", "max_rounds": 3})
    )

    assert result is expected
    assert seen["page"] is helper.page
    assert seen["options"] == {
        "enabled": True,
        "model": "call-model",
        "max_rounds": 3,
        "confidence_threshold": 0.65,
    }
    assert callable(seen["log"])
    assert callable(seen["screenshot"])


def test_helper_ignores_api_key_from_site_and_call_options(
    monkeypatch: Any, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    logs: list[str] = []

    async def fake_solve(_page: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(ok=False, status="not_configured", message="missing")

    monkeypatch.setattr(hcaptcha, "solve", fake_solve)
    helper = _helpers(
        tmp_path,
        {"hcaptcha": {"api_key": "site-secret", "model": "model-a"}},
        log=logs.append,
    )

    asyncio.run(
        helper.solve_hcaptcha(options={"api_key": "call-secret", "base_url": "https://v.invalid/v1"})
    )

    assert "api_key" not in seen["options"]
    assert seen["options"]["model"] == "model-a"
    assert seen["options"]["base_url"] == "https://v.invalid/v1"
    rendered = "\n".join(logs)
    assert "只允许通过环境变量" in rendered
    assert "site-secret" not in rendered
    assert "call-secret" not in rendered


def test_helper_passes_trigger_and_direct_options_object(monkeypatch: Any, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}
    direct = SimpleNamespace(enabled=True)

    async def trigger() -> None:
        return None

    async def fake_solve(_page: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return SimpleNamespace(ok=True, status="success", message="ok")

    monkeypatch.setattr(hcaptcha, "solve", fake_solve)
    helper = _helpers(tmp_path)

    asyncio.run(helper.solve_hcaptcha(trigger=trigger, options=direct))

    assert seen["trigger"] is trigger
    assert seen["options"] is direct


def test_helper_supplies_existing_screenshot_callback(monkeypatch: Any, tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    async def fake_solve(_page: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return SimpleNamespace(ok=False, status="failed", message="failed")

    async def fake_screenshot(name: str) -> str:
        return f"saved/{name}"

    monkeypatch.setattr(hcaptcha, "solve", fake_solve)
    helper = _helpers(tmp_path)
    monkeypatch.setattr(helper, "screenshot", fake_screenshot)

    asyncio.run(helper.solve_hcaptcha())
    value = asyncio.run(captured["screenshot"]("hcaptcha-failed.png"))

    assert value == "saved/hcaptcha-failed.png"
