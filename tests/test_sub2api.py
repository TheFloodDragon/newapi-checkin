from __future__ import annotations

import pytest

from providers.actions import api
from providers.base import ApiError, AuthInfo, SiteConfig
from providers.profiles import sub2api


def _client() -> sub2api.Sub2ApiClient:
    site = SiteConfig(
        name="sub2api-test",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="access_token",
        access_token="token",
    )
    return sub2api.Sub2ApiClient(site, AuthInfo(access_token="token"))


def test_expired_cached_token_refreshes_once_and_retries(monkeypatch) -> None:
    site = SiteConfig(
        name="sub2api-oauth",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="oauth",
        access_token="old-token",
        browser_state="saved-state",
    )
    refresh_calls = 0
    auth_headers: list[str] = []

    def refresher() -> str:
        nonlocal refresh_calls
        refresh_calls += 1
        return "fresh-token"

    client = sub2api.Sub2ApiClient(
        site,
        AuthInfo(access_token="old-token"),
        token_refresher=refresher,
    )

    def fake_http_request(url, *, method, headers, body, proxy, retry_non_idempotent, verify_ssl, **_kwargs):
        auth_headers.append(headers.get("Authorization", ""))
        if len(auth_headers) == 1:
            raise ApiError(401, {"code": "TOKEN_EXPIRED"}, "Token has expired")
        return {"code": 0, "data": {"balance": 9}}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    payload = client.request("GET", "/user/profile")

    assert payload == {"code": 0, "data": {"balance": 9}}
    assert refresh_calls == 1
    assert auth_headers == ["Bearer old-token", "Bearer fresh-token"]


def test_fetch_status_reads_checkin_extension(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr(
        client,
        "request",
        lambda method, path, body=None, *, retry_non_idempotent=False: {
            "checked_in_today": True,
            "balance": 22.5,
            "current_streak": 4,
        },
    )

    status = client.fetch_status()

    assert status.checked_in_today is True
    assert status.quota_usd == 22.5
    assert status.raw["source"] == "/check-in/status"


def test_usage_zero_balance_does_not_fall_back(monkeypatch) -> None:
    client = _client()
    usage_payload = {
        "items": [
            {
                "user": {"balance": 0},
                "api_key": {"quota": 99},
            }
        ]
    }

    def fake_standard_balance(data):
        return 99 if data is usage_payload else None

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        assert method == "GET"
        if path in {"/user/profile", "/auth/me"}:
            return {}
        if path.startswith("/usage?"):
            return usage_payload
        raise AssertionError(f"不应访问 {path}")

    monkeypatch.setattr(sub2api, "_extract_standard_balance", fake_standard_balance)
    monkeypatch.setattr(client, "request", fake_request)

    assert client.fetch_user().quota_raw == 0


def test_status_then_unsupported_checkin_reuses_user_probe(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        # 标准 Sub2API（无任何签到扩展）：所有 fork 的签到端点都返回 404，
        # fetch_status 降级到用户资料，do_checkin 落到「保活成功」。
        if path in {"/check-in/status", "/play/checkin/status", "/check-in", "/play/checkin"}:
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "GET" and path == "/user/profile":
            return {"username": "alice", "balance": 7}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    status = client.fetch_status()
    reward = client.do_checkin()

    assert status.quota_usd == 7
    assert reward.current_quota == 7
    assert reward.extra["unsupported_checkin"] is True
    # 状态探测遍历两个 fork 端点后回落用户资料；签到同样遍历两个端点。
    assert calls == [
        ("GET", "/check-in/status"),
        ("GET", "/play/checkin/status"),
        ("GET", "/user/profile"),
        ("POST", "/check-in"),
        ("POST", "/play/checkin"),
    ]


def test_query_action_fetch_user_and_status_share_one_probe(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        # 标准 Sub2API：无签到扩展接口，两个 fork 的状态端点都返回 404 触发降级。
        if path in {"/check-in/status", "/play/checkin/status"}:
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "GET" and path == "/user/profile":
            return {"username": "alice", "balance": 12.5}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _site, _auth: client)

    result = api.query_action(client.site, profile)

    assert result.ok is True
    assert result.quota_usd == 12.5
    # fetch_user 探测 /user/profile 并缓存；fetch_status 遍历两个状态端点（均 404）后复用缓存。
    assert calls == [
        ("GET", "/user/profile"),
        ("GET", "/check-in/status"),
        ("GET", "/play/checkin/status"),
    ]


def test_successful_real_checkin_invalidates_user_cache(monkeypatch) -> None:
    client = _client()
    profile_requests = 0

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        nonlocal profile_requests
        if method == "GET" and path == "/user/profile":
            profile_requests += 1
            return {"balance": 10 if profile_requests == 1 else 20}
        if method == "POST" and path == "/check-in":
            return {"reward_amount": 1}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    assert client.fetch_user().quota_raw == 10
    client.do_checkin()
    assert client.fetch_user().quota_raw == 20
    assert profile_requests == 2


def test_failed_first_user_query_does_not_poison_cache(monkeypatch) -> None:
    client = _client()
    attempts = 0

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        nonlocal attempts
        assert (method, path) == ("GET", "/user/profile")
        attempts += 1
        if attempts == 1:
            raise ApiError(503, None, "temporary", transient=True)
        return {"balance": 3}

    monkeypatch.setattr(client, "request", fake_request)

    with pytest.raises(ApiError, match="temporary"):
        client.fetch_user()
    assert client.fetch_user().quota_raw == 3
    assert client.fetch_user().quota_raw == 3
    assert attempts == 2


def test_authenticated_unknown_balance_is_cached(monkeypatch) -> None:
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        return {}

    def fake_usage():
        calls.append(("GET", "/v1/usage"))
        return {}

    monkeypatch.setattr(client, "request", fake_request)
    monkeypatch.setattr(client, "request_usage", fake_usage)

    first = client.fetch_user()
    first_call_count = len(calls)
    second = client.fetch_user()

    assert first.quota_raw is None
    assert second is first
    assert len(calls) == first_call_count


# ── fork 端点探测（极速蹬 /play/checkin vs 100xLabs /check-in）──────────────────

def test_jisudeng_style_play_checkin_endpoint_is_probed(monkeypatch) -> None:
    """极速蹬把签到挂在 /play/checkin；/check-in 返回 404 后应自动切到它。"""
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        if path.startswith("/check-in"):
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "GET" and path == "/play/checkin/status":
            return {"today_checked": False, "balance": 4.0}
        if method == "POST" and path == "/play/checkin":
            return {"reward_amount": 0.25, "balance": 4.25}
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    status = client.fetch_status()
    reward = client.do_checkin()

    # today_checked 被识别为「今日未签」，而不是 None
    assert status.checked_in_today is False
    assert status.quota_usd == 4.0
    assert reward.quota_awarded == 0.25
    assert reward.raw["checkin_endpoint"] == "/play/checkin"
    # 状态探测命中后缓存端点对，do_checkin 不再重试 /check-in
    assert calls == [
        ("GET", "/check-in/status"),
        ("GET", "/play/checkin/status"),
        ("POST", "/play/checkin"),
    ]


def test_already_checked_in_error_caches_endpoint(monkeypatch) -> None:
    """签到端点返回「今日已签到」也说明端点正确，应缓存而非继续试错。"""
    client = _client()
    calls: list[tuple[str, str]] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append((method, path))
        if path == "/check-in/status" or path == "/play/checkin/status":
            raise ApiError(404, {"message": "not found"}, "not found")
        if method == "POST" and path == "/check-in":
            raise ApiError(409, {"message": "今日已签到"}, "今日已签到")
        raise AssertionError(f"不应访问 {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    with pytest.raises(ApiError, match="今日已签到"):
        client.do_checkin()

    assert client._checkin_endpoint == ("/check-in", "/check-in/status")
    # 已签到即停止，不再尝试 /play/checkin
    assert ("POST", "/play/checkin") not in calls


def test_unsupported_on_all_endpoints_falls_back_to_balance_query(monkeypatch) -> None:
    """所有 fork 端点都不存在时，回落为「登录态验证 + 余额查询」保活。"""
    client = _client()

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        if method == "GET" and path == "/user/profile":
            return {"balance": 6}
        raise ApiError(404, {"message": "not found"}, "not found")

    monkeypatch.setattr(client, "request", fake_request)

    reward = client.do_checkin()

    assert reward.extra["unsupported_checkin"] is True
    assert reward.current_quota == 6
    assert reward.raw["probed_endpoints"] == ["/check-in", "/play/checkin"]


# ── 纯 HTTP refresh_token 续期（不启动浏览器）─────────────────────────────────

def test_http_refresh_token_renews_expired_access_token(monkeypatch) -> None:
    """access_token 过期时用 refresh_token 走纯 HTTP 续期，不拉起浏览器。"""
    site = SiteConfig(
        name="sub2api-refresh",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="access_token",
        access_token="stale-token",
        refresh_token="valid-refresh-token",
    )
    browser_calls = 0

    def browser_refresher() -> str:
        nonlocal browser_calls
        browser_calls += 1
        return "browser-token"

    client = sub2api.Sub2ApiClient(
        site,
        AuthInfo(access_token="stale-token"),
        token_refresher=browser_refresher,
    )

    seen_auth: list[str] = []
    refresh_bodies: list[bytes] = []
    persisted: list[tuple[str, str]] = []

    def fake_http_request(url, *, method, headers, body=None, proxy="", verify_ssl=True, **_kwargs):
        if url.endswith("/auth/refresh"):
            refresh_bodies.append(body)
            return {"data": {"access_token": "fresh-token", "refresh_token": "rotated-refresh"}}
        seen_auth.append(headers.get("Authorization", ""))
        if len(seen_auth) == 1:
            raise ApiError(401, {"code": "TOKEN_EXPIRED"}, "Token has expired")
        return {"code": 0, "data": {"balance": 3.5}}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)
    monkeypatch.setattr(
        sub2api,
        "_persist_refreshed_token",
        lambda s, token, refresh_token="": persisted.append((token, refresh_token)),
    )

    payload = client.request("GET", "/user/profile")

    assert payload == {"code": 0, "data": {"balance": 3.5}}
    # 401 后用 refresh_token 换到新 token 并重试成功
    assert seen_auth == ["Bearer stale-token", "Bearer fresh-token"]
    assert b"valid-refresh-token" in refresh_bodies[0]
    # 浏览器兜底完全没被调用
    assert browser_calls == 0
    # 新 token 与轮换后的 refresh_token 都被持久化
    assert persisted == [("fresh-token", "rotated-refresh")]
    assert client._refresh_token == "rotated-refresh"


def test_http_refresh_failure_falls_back_to_browser_refresher(monkeypatch) -> None:
    """refresh_token 也失效时，才回退到浏览器刷新。"""
    site = SiteConfig(
        name="sub2api-refresh-fail",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="oauth",
        access_token="stale-token",
        refresh_token="expired-refresh-token",
    )
    browser_calls = 0

    def browser_refresher() -> str:
        nonlocal browser_calls
        browser_calls += 1
        return "browser-token"

    client = sub2api.Sub2ApiClient(
        site,
        AuthInfo(access_token="stale-token"),
        token_refresher=browser_refresher,
    )
    seen_auth: list[str] = []

    def fake_http_request(url, *, method, headers, body=None, proxy="", verify_ssl=True, **_kwargs):
        if url.endswith("/auth/refresh"):
            raise ApiError(401, {"message": "refresh token expired"}, "refresh token expired")
        seen_auth.append(headers.get("Authorization", ""))
        if len(seen_auth) == 1:
            raise ApiError(401, {"code": "TOKEN_EXPIRED"}, "Token has expired")
        return {"code": 0, "data": {"balance": 1.0}}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    payload = client.request("GET", "/user/profile")

    assert payload == {"code": 0, "data": {"balance": 1.0}}
    assert browser_calls == 1
    assert seen_auth == ["Bearer stale-token", "Bearer browser-token"]


def test_missing_refresh_token_skips_http_refresh(monkeypatch) -> None:
    """未配置 refresh_token 时不应发出 /auth/refresh 请求。"""
    site = SiteConfig(
        name="sub2api-no-refresh",
        base_url="https://sub2api.invalid",
        site_profile="sub2api",
        auth_method="access_token",
        access_token="stale-token",
    )
    client = sub2api.Sub2ApiClient(site, AuthInfo(access_token="stale-token"))
    urls: list[str] = []

    def fake_http_request(url, *, method, headers, body=None, proxy="", verify_ssl=True, **_kwargs):
        urls.append(url)
        raise ApiError(401, {"code": "TOKEN_EXPIRED"}, "Token has expired")

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    with pytest.raises(ApiError):
        client.request("GET", "/user/profile")

    assert not any(url.endswith("/auth/refresh") for url in urls)


# ── 误报「签到成功但额度未到账」的防护 ────────────────────────────────────────
def test_empty_checkin_response_is_marked_unconfirmed() -> None:
    """签到接口回 200 但 body 无任何奖励证据时，必须标记未确认。

    这是「显示签到成功、实际额度没到账」的根因：旧实现把空响应当成空成功。
    """
    assert sub2api.Sub2ApiClient._reward_from({}).checkin_unconfirmed is True
    assert sub2api.Sub2ApiClient._reward_from({"data": None}).checkin_unconfirmed is True
    # 非 dict（HTML/纯文本）同样不构成证据
    assert sub2api.Sub2ApiClient._reward_from("ok").checkin_unconfirmed is True


def test_checkin_response_with_evidence_is_confirmed() -> None:
    """任一可信信号（奖励额度/余额/连续天数/已签标记）都算签到成立。"""
    for payload in (
        {"reward_amount": 0.25},
        {"balance": 12.5},
        {"current_streak": 3},
        {"already_checked_in": True},
        {"checked_in_today": True},
        {"success": True},
        {"checkin_date": "2026-07-28"},
    ):
        reward = sub2api.Sub2ApiClient._reward_from(payload)
        assert reward.checkin_unconfirmed is False, payload


def test_success_false_response_raises_instead_of_silent_success(monkeypatch) -> None:
    """{"success": false} 必须抛错，不能当成成功（此前只校验 code 字段）。"""
    client = _client()

    def fake_http_request(url, *, method, headers, body=None, **_kwargs):
        return {"success": False, "message": "签到功能已关闭"}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    with pytest.raises(ApiError, match="签到功能已关闭"):
        client.request("POST", "/check-in", {})


def test_unconfirmed_checkin_without_quota_growth_is_not_success(monkeypatch) -> None:
    """未确认签到 + 余额无增长 + 状态未标记已签 → 不得报成功。"""
    client = _client()
    balance = 10.0

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        if method == "GET" and path.endswith("/status"):
            # 状态接口可用但未标记已签到
            return {"checked_in_today": False, "balance": balance}
        if method == "GET" and path == "/user/profile":
            return {"balance": balance}
        if method == "POST":
            return {}  # 空响应：无任何签到证据
        raise ApiError(404, None, "not found")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _site, _auth: client)

    result = api.run_action(client.site, profile)

    assert result.status == "error"
    assert "未发放额度" in result.message
    assert result.detail["checkin_unconfirmed"] is True
    assert result.detail["checkin_unconfirmed"] is True


def test_unconfirmed_checkin_confirmed_by_quota_growth_is_success(monkeypatch) -> None:
    """未确认签到，但签到后余额真的增长 → 判定成功并回填增量。"""
    client = _client()
    state = {"balance": 10.0, "posted": False}

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        if method == "POST":
            state["posted"] = True
            state["balance"] = 10.25
            return {}  # 空响应，但余额确实变了
        if method == "GET" and path.endswith("/status"):
            return {"checked_in_today": False, "balance": state["balance"]}
        if method == "GET" and path == "/user/profile":
            return {"balance": state["balance"]}
        raise ApiError(404, None, "not found")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _site, _auth: client)

    result = api.run_action(client.site, profile)

    assert result.status == "success"
    assert "获得额度" in result.message
    assert state["posted"] is True


def test_jisudeng_endpoint_probed_and_cached(monkeypatch) -> None:
    """极速蹬只走 /play/checkin，不再先尝试不存在的 /check-in/status。"""
    client = sub2api.Sub2ApiClient(
        SiteConfig(
            name="极速蹬",
            base_url="https://www.jisudeng.com",
            site_profile="sub2api",
            auth_method="access_token",
            access_token="token",
        ),
        AuthInfo(access_token="token"),
    )
    calls: list[str] = []

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        calls.append(f"{method} {path}")
        if path.startswith("/check-in"):
            raise ApiError(404, "404 page not found", "404 page not found")
        if path == "/play/checkin/status":
            return {"checked_in_today": False, "balance": 5.0}
        if path == "/play/checkin":
            return {"reward_amount": 0.25, "balance": 5.25}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)

    status = client.fetch_status()
    assert status.checked_in_today is False
    assert calls == ["GET /play/checkin/status"]

    calls.clear()
    reward = client.do_checkin()
    assert reward.quota_awarded == 0.25
    assert reward.checkin_unconfirmed is False
    assert client._checkin_endpoint == ("/play/checkin", "/play/checkin/status")
    assert calls == ["POST /play/checkin"]

    # 端点已缓存：第二次不再探测错误的通用路径。
    calls.clear()
    client.do_checkin()
    assert calls == ["POST /play/checkin"]


def test_unconfirmed_but_quota_grew_is_success(monkeypatch) -> None:
    """空响应 + 余额确实增长 → 判成功并回填增量（站点静默发放）。"""
    client = _client()
    state = {"balance": 10.0, "checked_in": False}

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        if method == "POST" and path == "/play/checkin":
            state["balance"] = 10.25
            state["checked_in"] = True
            return {}  # 空响应：无任何签到证据
        if path.startswith("/check-in"):
            raise ApiError(404, None, "not found")
        if path == "/play/checkin/status":
            return {"checked_in_today": state["checked_in"], "balance": state["balance"]}
        if path == "/user/profile":
            return {"balance": state["balance"]}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _s, _a: client)

    result = api.run_action(client.site, profile)
    assert result.status == "success"
    assert "0.25" in result.message


def test_site_refusal_surfaces_original_reason(monkeypatch) -> None:
    """success:false 是站点明确拒绝，必须带出站点原因，不得伪装成「无签到接口」。"""
    client = _client()

    def fake_request(method: str, path: str, body=None, *, retry_non_idempotent: bool = False):
        if path.startswith("/check-in"):
            raise ApiError(404, None, "not found")
        if path == "/play/checkin/status":
            return {"checked_in_today": False, "balance": 10.0}
        if method == "POST" and path == "/play/checkin":
            raise ApiError(None, {"success": False, "message": "签到功能维护中"}, "签到功能维护中")
        if path == "/user/profile":
            return {"balance": 10.0}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(client, "request", fake_request)
    profile = sub2api.Sub2ApiProfile()
    monkeypatch.setattr(profile, "build_client", lambda _s, _a: client)

    result = api.run_action(client.site, profile)
    assert result.status == "error"
    assert "签到功能维护中" in result.message


def test_success_false_without_code_is_rejected(monkeypatch) -> None:
    """request() 必须校验 success:false（旧实现只查 code，会误判成功）。"""
    client = _client()

    def fake_http_request(url, *, method, headers, body=None, proxy="", **_kwargs):
        return {"success": False, "message": "签到功能维护中"}

    monkeypatch.setattr(sub2api, "http_request", fake_http_request)

    with pytest.raises(ApiError, match="签到功能维护中"):
        client.request("POST", "/play/checkin", {})
