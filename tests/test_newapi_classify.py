from __future__ import annotations

import pytest

from providers.base import ApiError, AuthInfo, SiteConfig
from providers.profiles.newapi import NewApiClient


def _client() -> NewApiClient:
    site = SiteConfig(name="t", base_url="https://newapi.invalid")
    return NewApiClient(site, AuthInfo(access_token="tok", new_api_user="1"))


def test_turnstile_empty_message_is_need_verification() -> None:
    # 服务端要求人机验证时返回「Turnstile token 为空」；message 含宽泛的 "token"，
    # 不能被 LOGIN_PATTERNS 误判为 need_login，应归为 need_verification。
    from providers.base import ApiError

    error = ApiError(None, {"message": "Turnstile token 为空", "success": False}, "Turnstile token 为空")
    assert _client().classify(error) == "need_verification"


def test_http_401_is_need_login() -> None:
    from providers.base import ApiError

    error = ApiError(401, {"message": "unauthorized"}, "unauthorized")
    assert _client().classify(error) == "need_login"


def test_login_message_without_verification_is_need_login() -> None:
    from providers.base import ApiError

    error = ApiError(None, {"message": "未登录"}, "未登录")
    assert _client().classify(error) == "need_login"


def test_already_done_message() -> None:
    from providers.base import ApiError

    error = ApiError(None, {"message": "今日已签到"}, "今日已签到")
    assert _client().classify(error) == "already_done"


def test_auto_challenge_waf_falls_back_to_legacy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node challenge 被 CF 单独拦截时，auto 模式仍应尝试 legacy API。"""
    client = _client()
    legacy_result = {"quota_awarded": 250000}

    def blocked_challenge():
        raise ApiError(
            None,
            "<!DOCTYPE html><title>Just a moment...</title>",
            "接口返回非 JSON：<!DOCTYPE html><title>Just a moment...</title>",
        )

    legacy_calls: list[str] = []
    monkeypatch.setattr(client, "_challenge_checkin", blocked_challenge)
    monkeypatch.setattr(
        client,
        "_legacy_checkin",
        lambda turnstile="": legacy_calls.append(turnstile) or legacy_result,
    )

    assert client._challenge_with_fallback("token") == legacy_result
    assert legacy_calls == ["token"]


def test_legacy_checkin_sends_daily_code_in_body_only_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带口令时按 {"code": ...} 提交；不带时保持原来的空对象体。"""
    sent: list[dict] = []

    def fake_request(url: str, **kwargs: object) -> object:
        sent.append({"url": url, "body": kwargs.get("body")})
        return {"success": True, "data": {"quota_awarded": 1}}

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        SiteConfig(name="code-site", base_url="https://code.invalid"),
        AuthInfo(access_token="tok"),
    )

    client._legacy_checkin(code="TODAY42")
    client._legacy_checkin()

    assert sent[0]["body"] == b'{"code": "TODAY42"}'
    assert sent[1]["body"] == b"{}"
    assert all(item["url"].endswith("/api/user/checkin") for item in sent)


def test_cloudflare_response_refreshes_cookie_once_and_retries_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """access_token 仍是身份凭据，过期 cf_clearance 只触发一次浏览器辅助刷新。"""
    site = SiteConfig(
        name="waf-site",
        base_url="https://waf.invalid",
        cookie="session=old; cf_clearance=stale",
    )
    calls: list[dict[str, object]] = []
    refresh_calls = 0

    def fake_request(url: str, **kwargs: object) -> object:
        nonlocal refresh_calls
        calls.append({"url": url, **kwargs})
        if len(calls) == 1:
            raise ApiError(
                403,
                "<!DOCTYPE html><title>Just a moment...</title>",
                "接口返回非 JSON：<!DOCTYPE html><title>Just a moment...</title>",
            )
        return {"success": True, "quota": 12}

    def refresh(_auth: AuthInfo) -> AuthInfo:
        nonlocal refresh_calls
        refresh_calls += 1
        return AuthInfo(
            cookie="session=old; cf_clearance=fresh",
            access_token="token-keep",
            new_api_user="7",
        )

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        site,
        AuthInfo(cookie="session=old; cf_clearance=stale", access_token="token-keep"),
        cookie_refresher=refresh,
    )

    assert client.request("GET", "/api/user/self") == {"success": True, "quota": 12}
    assert refresh_calls == 1
    assert len(calls) == 2
    assert "cf_clearance=fresh" in str(calls[1]["headers"])
    assert "Bearer token-keep" == calls[1]["headers"]["Authorization"]
    assert client.auth.access_token == "token-keep"
    assert client.auth.cookie.endswith("cf_clearance=fresh")


def test_cloudflare_refresh_is_not_repeated_after_failed_browser_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = SiteConfig(name="waf-site", base_url="https://waf.invalid")
    calls = 0
    refresh_calls = 0

    def fake_request(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise ApiError(403, "<title>Just a moment...</title>", "Just a moment")

    def refresh(_auth: AuthInfo) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        return None

    monkeypatch.setattr("providers.profiles.newapi.http_request", fake_request)
    client = NewApiClient(
        site,
        AuthInfo(access_token="token", cookie="cf_clearance=stale"),
        cookie_refresher=refresh,
    )

    with pytest.raises(ApiError):
        client.request("GET", "/api/user/self")
    assert calls == 1
    assert refresh_calls == 1


# ── Cloudflare 拦截页（error 1020 类）与挑战页必须分开处理 ──────────────────────
# 实测 Future Hub / Orbelis：GET /api/user/checkin 回 HTTP 403 + 「Sorry, you have
# been blocked」整页 HTML。旧实现把整页 HTML 当 message 一路上抛，汇总行直接被 5KB
# HTML 灌满，还看不出这是「当前出口 IP 被站点安全规则封禁」。
CF_BLOCK_HTML = """<!DOCTYPE html>
<!--[if lt IE 7]> <html class="no-js ie6 oldie" lang="en-US"> <![endif]-->
<!--[if IE 7]>    <html class="no-js ie7 oldie" lang="en-US"> <![endif]-->
<!--[if IE 8]>    <html class="no-js ie8 oldie" lang="en-US"> <![endif]-->
<!--[if gt IE 8]><!--> <html class="no-js" lang="en-US"> <!--<![endif]-->
<head>
<title>Attention Required! | Cloudflare</title>
<link rel="stylesheet" id="cf_styles-css" href="/cdn-cgi/styles/cf.errors.css" />
</head>
<body>
  <div id="cf-error-details" class="cf-error-details-wrapper">
    <h1 data-translate="block_headline">Sorry, you have been blocked</h1>
    <h2 class="cf-subheadline"><span>You are unable to access</span> futureppo.top</h2>
  </div>
  <div class="cf-error-footer">
    <span class="cf-footer-item">Cloudflare Ray ID: <strong class="font-semibold">a31ae42cdc7984cc</strong></span>
    <span id="cf-footer-item-ip">Your IP: <span class="hidden" id="cf-footer-ip">2602:2b5:13::19d</span></span>
  </div>
  <script>window.__CF$cv$params={r:'a31ae42cdc7984cc'};var a=document.createElement('script');
  a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>
</body>
</html>"""


def test_cloudflare_block_page_becomes_one_actionable_line() -> None:
    from providers.base import parse_json, waf_page_kind

    assert waf_page_kind(CF_BLOCK_HTML) == "block"
    with pytest.raises(ApiError) as excinfo:
        parse_json(CF_BLOCK_HTML)

    error = excinfo.value
    assert error.waf_kind == "block"
    assert "<html" not in error.message and "<!DOCTYPE" not in error.message
    assert len(error.message) < 200
    # Ray ID 与站点回显的出口 IP 是排查这类封禁的唯一线索，必须留在消息里。
    assert "a31ae42cdc7984cc" in error.message
    assert "2602:2b5:13::19d" in error.message
    assert "出口 IP" in error.message


def test_cloudflare_block_is_need_verification_without_browser_fallback() -> None:
    """拦截页浏览器同样过不去：归类保持 need_verification，但不许再白开一次浏览器。"""
    from providers.base import extract_message, waf_page_kind
    from providers.profiles.newapi import _browser_can_bypass, _is_antibot_block

    error = ApiError(
        403,
        CF_BLOCK_HTML,
        extract_message(CF_BLOCK_HTML),
        waf_kind=waf_page_kind(CF_BLOCK_HTML),
    )

    assert _client().classify(error) == "need_verification"
    assert _is_antibot_block(error) is True
    assert _browser_can_bypass(error) is False


def test_cloudflare_challenge_page_still_gets_browser_fallback() -> None:
    """挑战页恰恰相反：浏览器执行一次 JS 就能过，必须继续走浏览器兜底。"""
    from providers.base import parse_json
    from providers.profiles.newapi import _browser_can_bypass

    challenge = (
        '<!DOCTYPE html><html><head><title>Just a moment...</title></head>'
        '<body><div class="cf-challenge-running"></div></body></html>'
    )
    with pytest.raises(ApiError) as excinfo:
        parse_json(challenge)

    assert excinfo.value.waf_kind == "challenge"
    assert _browser_can_bypass(excinfo.value) is True
    assert _client().classify(excinfo.value) == "need_verification"


def test_aliyun_waf_js_is_still_a_solvable_challenge() -> None:
    from providers.base import parse_json
    from providers.profiles.newapi import _browser_can_bypass

    with pytest.raises(ApiError) as excinfo:
        parse_json("var arg1='7F3A';var _0x=function(){};acw_sc__v2")

    assert excinfo.value.waf_kind == "challenge"
    assert _browser_can_bypass(excinfo.value) is True


def test_plain_html_error_page_is_summarized_not_dumped() -> None:
    """非 WAF 的 HTML 页面（登录页/502 页）也不该把整页塞进 message。"""
    from providers.base import extract_message

    html = (
        "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
        "<body><h1>nginx</h1>" + "填充" * 2000 + "</body></html>"
    )
    message = extract_message(html)

    assert "502 Bad Gateway" in message and "nginx" in message
    assert len(message) < 200
    assert "填充填充" not in message
