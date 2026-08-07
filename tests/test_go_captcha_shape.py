from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from captcha_ocr.go_captcha_shape import _decode_image, _detect_target_count, solve_challenge
from scripts import newapi_captcha, newapi_turnstile

FIXTURES = Path(__file__).parent / "fixtures" / "go_captcha_shape"


def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def test_click_shape_real_samples() -> None:
    """两张真实 GoCaptcha 挑战必须稳定还原按序点位。"""
    expected = {
        1: [(32, 198), (122, 127), (223, 32)],
        2: [(85, 30), (158, 31), (200, 30)],
    }
    for index, points in expected.items():
        main = _b64(FIXTURES / f"sample{index}_main.jpg")
        thumb = _b64(FIXTURES / f"sample{index}_thumb.png")
        assert _detect_target_count(_decode_image(thumb)) == 3
        assert solve_challenge(main, thumb) == points


class CaptchaFlagClient:
    def get_checkin_status_raw(self) -> dict[str, Any]:
        return {"data": {"captcha_enabled": False}}

    def request(self, method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        assert (method, path) == ("GET", "/api/status")
        return {
            "data": {
                "captcha_checkin_enabled": True,
                "captcha_type": "click-shape",
            }
        }


def test_captcha_required_accepts_reversed_flag_name() -> None:
    """向量引擎使用 captcha_checkin_enabled，不能只认 checkin_captcha_enabled。"""
    assert newapi_captcha.captcha_required(CaptchaFlagClient()) is True


def test_turnstile_script_delegates_to_captcha(monkeypatch) -> None:
    """全局 Turnstile=false 但签到验证码=true 时，应委派而不是裸签。"""
    client = SimpleNamespace()
    client.request = lambda *_args, **_kwargs: {
        "data": {
            "turnstile_check": False,
            "turnstile_site_key": "",
            "captcha_checkin_enabled": True,
            "captcha_type": "click-shape",
        }
    }
    sentinel = object()
    monkeypatch.setattr(
        newapi_captcha,
        "mode_checkin",
        lambda _client, mode, log=None: sentinel if mode == "click_shape" else None,
    )
    assert newapi_turnstile.do_checkin(client) is sentinel


class ClickShapeClient:
    base_url = "https://example.com"
    referer = "https://example.com/console"
    site = SimpleNamespace(proxy="", verify_ssl=True)
    auth = SimpleNamespace(access_token="token", new_api_user="1", cookie="")

    def __init__(self) -> None:
        self.paths: list[str] = []

    def request(self, method: str, path: str, **_kwargs: Any) -> dict[str, Any]:
        self.paths.append(path)
        if path == newapi_captcha.GO_CAPTCHA_DATA_PATH:
            return {
                "code": 0,
                "captcha_key": "key",
                "image_base64": "main",
                "thumb_base64": "thumb",
            }
        assert method == "POST"
        return {"success": True, "data": {"quota_awarded": 123}}


def test_click_shape_token_is_submitted_to_checkin(monkeypatch) -> None:
    client = ClickShapeClient()
    monkeypatch.setattr(
        "captcha_ocr.go_captcha_shape.solve_challenge",
        lambda _image, _thumb, log=None: [(10, 20), (30, 40)],
    )
    monkeypatch.setattr("captcha_ocr.go_captcha_shape.available", lambda: True)
    monkeypatch.setattr(
        newapi_captcha,
        "_go_captcha_request",
        lambda _client, fields: {"code": 0, "token": "verified-token"},
    )
    stats: dict[str, Any] = {}
    data = newapi_captcha.click_shape_checkin(client, stats=stats)
    assert data == {"quota_awarded": 123}
    assert client.paths[-1].endswith("captcha_token=verified-token")
    assert stats["captcha_dialect"] == "click_shape"
    assert stats["captcha_points"] == [(10, 20), (30, 40)]
