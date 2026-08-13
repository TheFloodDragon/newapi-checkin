#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VC API（sub.vcnovb.cn）幸运轮盘每日抽取。

轮盘抽取本身就是本站签到：优先通过 Sub2ApiClient 纯 HTTP 调用；Token 不可用时由
browser_script action 依次尝试 refresh_token、纯 HTTP 账密登录，最后才启动浏览器。
浏览器兜底也只调用站点真实轮盘 API，不依赖动画按钮。
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _sub2api_common as common  # noqa: E402
from providers.base import ApiError, CheckinReward  # noqa: E402

POOL_KEY = "normal"
TIMEZONE_QUERY = "timezone=Asia%2FShanghai"
SUMMARY_ROUTE = f"/lottery?{TIMEZONE_QUERY}"
HISTORY_ROUTE = f"/lottery/history?page=1&page_size=20&{TIMEZONE_QUERY}"
DRAW_ROUTE = f"/lottery/pools/{POOL_KEY}/draw?{TIMEZONE_QUERY}"
NO_CHANCE_REASON = "LOTTERY_NO_CHANCE"

SPEC = common.SiteSpec(
    site_label="VC API 幸运轮盘",
    checkin_path=f"/api/v1/lottery/pools/{POOL_KEY}/draw",
    login_reset_sentinel="__vcnovb_lottery_login_reset",
    screenshot_prefix="vcnovb-lottery",
    default_start_path="/lottery",
    email_env="VCNOVB_EMAIL",
    password_env="VCNOVB_PASSWORD",
    response_match=("/lottery/pools/", "/draw"),
    success_message="抽奖完成",
)

# 声明本脚本完全接管 HTTP 流程（状态查询、签到与结果确认），跳过 Sub2API 标准端点探测。
# 本站使用自定义 lottery 接口而非标准签到接口，探测 /api/v1/check-in/status 等端点只会产生无用的 404 日志。
OWNS_HTTP_FLOW = True


def _log_fn(log: Any = None):
    return log if callable(log) else (lambda _message: None)


def _unwrap(payload: Any) -> Any:
    """解开 Sub2API 的 {code, data} 响应信封。"""
    if isinstance(payload, dict) and "data" in payload:
        data = payload.get("data")
        if data is not None:
            return data
    return payload


def _pool_state(payload: Any) -> dict[str, Any] | None:
    data = _unwrap(payload)
    pools = data.get("pools") if isinstance(data, dict) else None
    if not isinstance(pools, list):
        return None
    for item in pools:
        if not isinstance(item, dict):
            continue
        pool = item.get("pool")
        if isinstance(pool, dict) and str(pool.get("key") or "") == POOL_KEY:
            return item
    return None


def _remaining(state: dict[str, Any]) -> int:
    total = 0
    for key in ("base_remaining", "extra_remaining"):
        value = state.get(key)
        if isinstance(value, bool):
            continue
        try:
            total += max(0, int(value or 0))
        except (TypeError, ValueError):
            continue
    return total


def _history_items(payload: Any) -> list[dict[str, Any]]:
    data = _unwrap(payload)
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


def _period_date(period_key: Any) -> str:
    text = str(period_key or "")
    prefix, sep, value = text.partition(":")
    return value if sep and prefix == "d" else ""


def _matching_history(payload: Any, period_key: Any) -> dict[str, Any] | None:
    wanted_date = _period_date(period_key)
    for item in _history_items(payload):
        if str(item.get("pool_key") or "") != POOL_KEY:
            continue
        created_at = str(item.get("created_at") or "")
        if not wanted_date or created_at.startswith(wanted_date):
            return item
    return None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prize(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("prize")
    return value if isinstance(value, dict) else {}


def _result_text(record: dict[str, Any]) -> str:
    outcome = str(record.get("outcome") or "none").casefold()
    prize = _prize(record)
    name = str(prize.get("name") or "").strip()
    prize_type = str(prize.get("prize_type") or "").casefold()

    if outcome == "none":
        return "未中奖"
    if outcome == "blessing":
        return name or "获得祝福"
    if prize_type == "subscription":
        days = _number(prize.get("validity_days"))
        suffix = f"（{int(days)} 天）" if days is not None else ""
        return f"{name or '订阅套餐'}{suffix}"
    if prize_type == "balance":
        amount = _number(prize.get("balance_amount"))
        suffix = f"（+${amount:.2f}）" if amount is not None else ""
        return f"{name or '余额奖励'}{suffix}"
    return name or "中奖"


def _result_message(record: dict[str, Any] | None, *, already: bool) -> str:
    if record is None:
        return "今日已抽取（历史结果不可用）" if already else "抽奖完成（结果不可用）"
    prefix = "今日已抽取" if already else (
        "抽奖成功" if str(record.get("outcome") or "").casefold() == "win" else "抽奖完成"
    )
    return f"{prefix}：{_result_text(record)}"


def _detail(
    state: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    already: bool,
) -> dict[str, Any]:
    prize = _prize(record or {})
    detail: dict[str, Any] = {
        "result_message": _result_message(record, already=already),
        "checked_in_today": True,
        "lottery_pool": POOL_KEY,
        "period_key": state.get("period_key"),
        "base_remaining": state.get("base_remaining"),
        "extra_remaining": state.get("extra_remaining"),
        "completion_signal": "lottery_history" if already else "lottery_draw_response",
    }
    if record is not None:
        detail.update(
            {
                "draw_id": record.get("id"),
                "lottery_outcome": record.get("outcome"),
                "chance_source": record.get("chance_source"),
                "prize_id": record.get("prize_id"),
                "lottery_prize_name": prize.get("name"),
                "prize_type": prize.get("prize_type"),
                "balance_amount": prize.get("balance_amount"),
                "validity_days": prize.get("validity_days"),
            }
        )
    return detail


def _reward(
    state: dict[str, Any],
    record: dict[str, Any] | None,
    *,
    already: bool,
    raw: Any = None,
) -> CheckinReward:
    prize = _prize(record or {})
    awarded: float | None = None
    if not already and str((record or {}).get("outcome") or "").casefold() == "win":
        if str(prize.get("prize_type") or "").casefold() == "balance":
            awarded = _number(prize.get("balance_amount"))
    return CheckinReward(
        already_done=already,
        quota_awarded=awarded,
        raw=raw if raw is not None else record,
        extra=_detail(state, record, already=already),
    )


def _is_no_chance(exc: ApiError) -> bool:
    payload = exc.payload if isinstance(exc.payload, dict) else {}
    reason = str(payload.get("reason") or "").upper()
    message = str(payload.get("message") or exc.message or "").casefold()
    return reason == NO_CHANCE_REASON or "no lottery chance" in message


def _idempotency_key(client: Any, state: dict[str, Any]) -> str:
    """每个账号/周期固定一个键；只传摘要，不泄露账号标识。"""
    site = getattr(client, "site", None)
    args = getattr(site, "script_args", {}) if site is not None else {}
    email = str(args.get("email") or "") if isinstance(args, dict) else ""
    seed = "|".join(
        (
            str(getattr(site, "base_url", "") or getattr(client, "base_url", "")),
            str(getattr(site, "name", "")),
            email,
            POOL_KEY,
            str(state.get("period_key") or ""),
        )
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return f"vcnovb-lottery-{digest[:48]}"


def _already_reward(client: Any, state: dict[str, Any], log: Any = None) -> CheckinReward:
    _log = _log_fn(log)
    history = client.request("GET", HISTORY_ROUTE)
    record = _matching_history(history, state.get("period_key"))
    message = _result_message(record, already=True)
    _log(message)
    return _reward(state, record, already=True, raw=history)


def do_checkin(client: Any, log: Any = None) -> CheckinReward:
    """纯 HTTP 每日抽取；由 browser_script 的 API-first 阶段调用。"""
    _log = _log_fn(log)
    _log("读取幸运轮盘今日状态")
    summary = client.request("GET", SUMMARY_ROUTE)
    state = _pool_state(summary)
    if state is None:
        raise ApiError(None, summary, "轮盘状态响应缺少普通奖池")

    pool = state.get("pool") if isinstance(state.get("pool"), dict) else {}
    if not bool(state.get("active")) or not bool(pool.get("enabled", True)):
        raise ApiError(None, summary, "普通抽奖当前未开放")

    if _remaining(state) <= 0:
        return _already_reward(client, state, log=log)

    _log("今日轮盘仍有次数，调用抽取接口")
    try:
        draw = client.request(
            "POST",
            DRAW_ROUTE,
            {},
            extra_headers={"Idempotency-Key": _idempotency_key(client, state)},
        )
    except ApiError as exc:
        # 两个并发任务都在 GET 时看到剩余 1 次，后到的 POST 会得到 NO_CHANCE；
        # 此时读取历史即可恢复为同一个幂等成功结果。
        if _is_no_chance(exc):
            _log("抽取机会已被本周期其它请求使用，读取历史结果")
            return _already_reward(client, state, log=log)
        raise

    record = _unwrap(draw)
    if not isinstance(record, dict):
        raise ApiError(None, draw, "抽取接口未返回可识别结果")
    message = _result_message(record, already=False)
    _log(message)
    # 成功响应带有最新剩余次数时优先采用，便于 detail 与页面保持一致。
    for key in ("base_remaining", "extra_remaining"):
        if record.get(key) is not None:
            state[key] = record.get(key)
    return _reward(state, record, already=False, raw=draw)


_BROWSER_LOTTERY_JS = common._page_auth_script(  # noqa: SLF001 - 复用同源站点鉴权状态机
    f"""
    const unwrap = (raw) => raw && typeof raw === 'object' && raw.data !== undefined
        ? raw.data : raw;
    const poolKey = {POOL_KEY!r};
    const summaryPath = '/api/v1/lottery?{TIMEZONE_QUERY}';
    const historyPath = '/api/v1/lottery/history?page=1&page_size=20&{TIMEZONE_QUERY}';
    const drawPath = '/api/v1/lottery/pools/' + poolKey + '/draw?{TIMEZONE_QUERY}';
    const readHistory = async (state) => {{
        const response = await requestWithAuth((accessToken) => fetch(baseUrl + historyPath, {{
            credentials: 'include',
            headers: {{ Authorization: `Bearer ${{accessToken}}`, Accept: 'application/json' }},
        }}));
        if (!response) return {{ ok: false, status: 401, reason: 'NO_TOKEN' }};
        const raw = await parseBody(response);
        const data = unwrap(raw);
        const items = data && Array.isArray(data.items) ? data.items : [];
        const day = String(state.period_key || '').startsWith('d:')
            ? String(state.period_key).slice(2) : '';
        const record = items.find((item) => item && item.pool_key === poolKey
            && (!day || String(item.created_at || '').startsWith(day))) || null;
        return {{ ok: response.ok, status: response.status, already: true, state, record, raw }};
    }};
    try {{
        const summaryResponse = await requestWithAuth((accessToken) => fetch(baseUrl + summaryPath, {{
            credentials: 'include',
            headers: {{ Authorization: `Bearer ${{accessToken}}`, Accept: 'application/json' }},
        }}));
        if (!summaryResponse) return {{ ok: false, status: 401, reason: 'NO_TOKEN' }};
        const summaryRaw = await parseBody(summaryResponse);
        const summary = unwrap(summaryRaw);
        const pools = summary && Array.isArray(summary.pools) ? summary.pools : [];
        const state = pools.find((item) => item && item.pool && item.pool.key === poolKey);
        if (!summaryResponse.ok || !state) {{
            return {{ ok: false, status: summaryResponse.status, reason: 'INVALID_SUMMARY', raw: summaryRaw }};
        }}
        if (!state.active || state.pool.enabled === false) {{
            return {{ ok: false, status: 400, reason: 'POOL_INACTIVE', state }};
        }}
        const remaining = Number(state.base_remaining || 0) + Number(state.extra_remaining || 0);
        if (remaining <= 0) return await readHistory(state);

        let userId = 'user';
        try {{
            const user = JSON.parse(localStorage.getItem('auth_user') || '{{}}');
            if (user && user.id !== undefined) userId = String(user.id);
        }} catch (_) {{ /* ignore */ }}
        const idempotencyKey = `vcnovb-lottery-${{userId}}-${{String(state.period_key || 'period')}}-${{poolKey}}`;
        const drawResponse = await requestWithAuth((accessToken) => fetch(baseUrl + drawPath, {{
            method: 'POST',
            credentials: 'include',
            headers: {{
                Authorization: `Bearer ${{accessToken}}`,
                Accept: 'application/json',
                'Content-Type': 'application/json',
                'Idempotency-Key': idempotencyKey,
            }},
            body: '{{}}',
        }}));
        if (!drawResponse) return {{ ok: false, status: 401, reason: 'NO_TOKEN' }};
        const drawRaw = await parseBody(drawResponse);
        const reason = String(drawRaw && drawRaw.reason || '');
        if (!drawResponse.ok && reason === {NO_CHANCE_REASON!r}) return await readHistory(state);
        const record = unwrap(drawRaw);
        return {{
            ok: drawResponse.ok,
            status: drawResponse.status,
            already: false,
            state,
            record: record && typeof record === 'object' ? record : null,
            raw: drawRaw,
            reason,
        }};
    }} catch (_) {{
        return {{ ok: false, status: 0, reason: 'FETCH_ERROR' }};
    }}
"""
)


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """浏览器兜底：完成真实登录后仍直接调用轮盘 API。"""
    opts = common.parse_options(SPEC, getattr(site, "script_args", {}))
    start_target = opts.start_target or SPEC.default_start_path
    resolved_url = helpers.resolve_url(start_target)
    origin = helpers.resolve_url("/").rstrip("/")
    login_detail: dict[str, Any] = {}

    async def do_login() -> dict[str, Any] | None:
        return await common.login_with_password(
            page,
            context,
            helpers,
            SPEC,
            opts,
            resolved_url=resolved_url,
            origin=origin,
            login_detail=login_detail,
        )

    await common.add_init_script(context, common.preflight_init_script())
    await common.navigate_and_settle(page, helpers, start_target, opts)

    login_attempted = False
    if await common.on_login_page(page):
        login_attempted = True
        failure = await do_login()
        if failure is not None:
            return failure
        await common.navigate_and_settle(page, helpers, start_target, opts)

    if not await common.authenticated(page, origin):
        if not login_attempted:
            failure = await do_login()
            if failure is not None:
                return failure
        if not await common.authenticated(page, origin):
            return helpers.need_login(
                "VC API 幸运轮盘登录态无效，请检查账号凭据",
                {"target_url": resolved_url, **login_detail},
            )

    login_detail["auth_verified"] = True
    await common.persist_state(context, site)
    helpers.log("浏览器登录态已验证，直接调用幸运轮盘 API")
    try:
        result = await page.evaluate(_BROWSER_LOTTERY_JS, origin)
    except Exception as exc:
        return helpers.error(
            f"浏览器内轮盘 API 调用异常：{type(exc).__name__}",
            {"target_url": resolved_url, **login_detail},
        )
    if not isinstance(result, dict):
        return helpers.error("浏览器内轮盘 API 未返回有效结果", {"target_url": resolved_url, **login_detail})

    status = int(result.get("status") or 0)
    if not bool(result.get("ok")):
        reason = str(result.get("reason") or "UNKNOWN")[:80]
        if status in {401, 403}:
            return helpers.need_login(
                "VC API 幸运轮盘登录态已失效",
                {"response_status": status, "reason": reason, **login_detail},
            )
        return helpers.error(
            f"幸运轮盘接口调用失败（HTTP {status or 0}，{reason}）",
            {"response_status": status, "reason": reason, **login_detail},
        )

    state = result.get("state") if isinstance(result.get("state"), dict) else {}
    record = result.get("record") if isinstance(result.get("record"), dict) else None
    already = bool(result.get("already"))
    reward = _reward(state, record, already=already, raw=result.get("raw"))
    detail = {**reward.extra, "response_status": status, "checkin_source": "browser_api", **login_detail}
    message = str(reward.extra.get("result_message") or "抽奖完成")
    if already:
        return helpers.already_done(message, detail, quota_is_usd=True)
    return helpers.success(
        message,
        detail,
        awarded=reward.quota_awarded,
        quota_is_usd=True,
    )
