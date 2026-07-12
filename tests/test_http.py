from __future__ import annotations

import urllib.request

import pytest

from providers import base


def test_http_retry_only_for_idempotent_methods(monkeypatch) -> None:
    calls: list[str] = []

    def fake_once(url, *, method, headers, body, timeout, proxy, verify_ssl=True):
        calls.append(method)
        if len(calls) == 1:
            raise base.ApiError(503, None, "temporary", transient=True)
        return {"ok": True}

    monkeypatch.setattr(base, "_http_request_once", fake_once)
    monkeypatch.setattr(base.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(base.random, "uniform", lambda _a, _b: 0)

    assert base.http_request("https://example.invalid", method="GET", max_attempts=3) == {"ok": True}
    assert calls == ["GET", "GET"]

    calls.clear()
    with pytest.raises(base.ApiError):
        base.http_request("https://example.invalid", method="POST", max_attempts=3)
    assert calls == ["POST"]


def test_non_idempotent_retry_requires_opt_in(monkeypatch) -> None:
    calls = 0

    def fake_once(url, *, method, headers, body, timeout, proxy, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise base.ApiError(503, None, "temporary", transient=True)
        return {"ok": True}

    monkeypatch.setattr(base, "_http_request_once", fake_once)
    monkeypatch.setattr(base.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(base.random, "uniform", lambda _a, _b: 0)
    assert base.http_request(
        "https://example.invalid",
        method="POST",
        max_attempts=2,
        retry_non_idempotent=True,
    ) == {"ok": True}


def test_proxy_handler_is_explicit() -> None:
    opener = base._build_url_opener("http://user:pass@proxy.invalid:8080")
    proxy_handlers = [handler for handler in opener.handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies["https"] == "http://user:pass@proxy.invalid:8080"

    direct = base._build_url_opener("")
    direct_handlers = [handler for handler in direct.handlers if isinstance(handler, urllib.request.ProxyHandler)]
    assert direct_handlers == []

    with pytest.raises(base.ApiError, match="SOCKS"):
        base._build_url_opener("socks5://127.0.0.1:1080")
