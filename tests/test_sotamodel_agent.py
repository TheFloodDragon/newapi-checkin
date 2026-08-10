# -*- coding: utf-8 -*-
"""SOTA Model agent 签到脚本回归。

站点把签到搬到 /api/user/sota-agent-checkin，标准 /api/user/checkin 固定回
「签到功能未启用」。这里锁住：只调实际启用的端点、金额换算、不确定 POST 的恢复、
Turnstile 归类，以及日志不泄露令牌。
"""

from __future__ import annotations

from typing import Any

import pytest

from browser import script_loader
from providers.base import ApiError, QUOTA_UNIT, UserInfo

SCRIPT_PATH = "scripts/checkin/sotamodel_agent.py"
sota = script_loader.load_site_script(SCRIPT_PATH)


class FakeClient:
    """只实现脚本会用到的 newapi 客户端表面。"""

    quota_is_usd = False

    def __init__(self, replies: dict[str, Any], *, user_quota: Any = 534931547) -> None:
        self.replies = replies
        self.user_quota = user_quota
        self.calls: list[tuple[str, str]] = []
        self.user_calls = 0

    def request(self, method: str, path: str, body: Any = None, **_kwargs: Any) -> Any:
        self.calls.append((method, path))
        reply = self.replies[method.upper()]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def fetch_user(self) -> UserInfo:
        self.user_calls += 1
        return UserInfo(quota_raw=self.user_quota)


def _status(*, checked: bool, credits: int = 300) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "checked_in_today": checked,
            "reward_credits": credits,
            "reward_quota": credits * QUOTA_UNIT,
        },
    }


def _checkin(*, credits: int = 300, current: int = 685_000_000) -> dict[str, Any]:
    return {
        "success": True,
        "data": {
            "reward_credits": credits,
            "quota_awarded": credits * QUOTA_UNIT,
            "current_quota": current,
        },
    }


def test_script_declares_own_http_flow() -> None:
    hooks = script_loader.load_script_hooks(SCRIPT_PATH)

    assert hooks.owns_http_flow is True
    assert hooks.do_checkin is not None


def test_already_checked_in_reports_today_reward_without_post() -> None:
    client = FakeClient({"GET": _status(checked=True)})

    reward = sota.do_checkin(client)

    assert reward.already_done is True
    assert reward.extra["result_message"] == "今日已签到，今日奖励 $300.00"
    # 已签到时的金额是「今日这一档奖励」，不是本次到账，不得写进 quota_awarded。
    assert reward.quota_awarded is None
    assert reward.current_quota == 534931547
    assert reward.extra["today_reward_credits"] == pytest.approx(300)
    assert all(method == "GET" for method, _path in client.calls)


def test_first_checkin_awards_internal_quota_and_amount_message() -> None:
    client = FakeClient({"GET": _status(checked=False), "POST": _checkin()})

    reward = sota.do_checkin(client)

    assert reward.already_done is False
    # 汇总层按 quota_is_usd=False 换算，因此额度必须是站点内部 quota（$300 = 1.5e8）。
    assert reward.quota_awarded == pytest.approx(150_000_000)
    assert reward.current_quota == 685_000_000
    assert reward.extra["result_message"] == "签到成功，获得 $300.00"
    assert reward.extra["completion_signal"] == "sota_agent_checkin_response"
    assert reward.extra["reward_credits"] == pytest.approx(300)
    assert sum(method == "POST" for method, _path in client.calls) == 1
    assert {path for _method, path in client.calls} == {sota.CHECKIN_ROUTE}


def test_reward_amount_follows_server_weekday_value() -> None:
    """奖励按星期配置，脚本只如实上报服务端返回值，不硬编码 $300。"""
    client = FakeClient(
        {"GET": _status(checked=False, credits=120), "POST": _checkin(credits=120)}
    )

    reward = sota.do_checkin(client)

    assert reward.quota_awarded == pytest.approx(60_000_000)
    assert reward.extra["result_message"] == "签到成功，获得 $120.00"


def test_credits_only_response_is_converted_to_internal_quota() -> None:
    """站点只回 credits（美元）时要乘回 QUOTA_UNIT，避免汇总层再除一次成 $0.0006。"""
    client = FakeClient(
        {
            "GET": _status(checked=False),
            "POST": {"success": True, "data": {"reward_credits": 300}},
        }
    )

    reward = sota.do_checkin(client)

    assert reward.quota_awarded == pytest.approx(300 * QUOTA_UNIT)
    assert reward.extra["result_message"] == "签到成功，获得 $300.00"


def test_ambiguous_post_recovers_from_checked_status() -> None:
    client = FakeClient(
        {
            "GET": [_status(checked=False), _status(checked=True)],
            "POST": ApiError(None, None, "network timeout", transient=True),
        }
    )

    reward = sota.do_checkin(client)

    assert reward.already_done is True
    assert reward.extra["result_message"] == "今日已签到，今日奖励 $300.00"
    assert reward.extra["completion_signal"] == "sota_agent_status_after_post"


def test_duplicate_post_rejection_is_already_done() -> None:
    client = FakeClient(
        {
            "GET": [_status(checked=False), _status(checked=False)],
            "POST": ApiError(None, {"message": "今日已签到"}, "今日已签到"),
        }
    )

    reward = sota.do_checkin(client)

    assert reward.already_done is True


def test_post_failure_without_server_evidence_is_reported() -> None:
    """服务端既没记账也没给奖励时如实失败，不谎报成功。"""
    client = FakeClient(
        {
            "GET": [_status(checked=False), _status(checked=False)],
            "POST": ApiError(500, {"message": "internal error"}, "internal error"),
        }
    )

    with pytest.raises(ApiError, match="internal error"):
        sota.do_checkin(client)


def test_success_without_amount_requires_status_confirmation() -> None:
    """POST 回 200 但无金额时必须回读状态；未记账则报可重试错误。"""
    client = FakeClient(
        {
            "GET": [_status(checked=False), _status(checked=False)],
            "POST": {"success": True, "data": {}},
        }
    )

    with pytest.raises(ApiError, match="未给出奖励额度") as caught:
        sota.do_checkin(client)

    assert caught.value.transient is True


def test_turnstile_rejection_is_classified_as_need_verification() -> None:
    """站点要求人机验证时不能报签到失败：交由 newapi.classify 归为 need_verification。"""
    from providers.profiles.newapi import NewApiClient
    from providers.base import AuthInfo, SiteConfig

    client = FakeClient(
        {
            "GET": _status(checked=False),
            "POST": ApiError(400, {"message": "Turnstile token 为空"}, "Turnstile token 为空"),
        }
    )

    with pytest.raises(ApiError) as caught:
        sota.do_checkin(client)

    real = NewApiClient(
        SiteConfig(name="SOTA Model", base_url="https://www.sotamodel.net"),
        AuthInfo(access_token="t"),
    )
    assert real.classify(caught.value) == "need_verification"
    assert "--turnstile" in caught.value.message


def test_status_endpoint_failure_propagates() -> None:
    client = FakeClient({"GET": ApiError(401, {"message": "未登录"}, "未登录")})

    with pytest.raises(ApiError, match="未登录"):
        sota.do_checkin(client)


def test_logs_only_describe_route_and_outcome() -> None:
    """脚本不自持凭据（认证由 newapi 客户端负责），日志里也不得出现凭据材料。"""
    client = FakeClient({"GET": _status(checked=True)})
    client.access_token = "SuperSecretSotaToken"  # 即使客户端带令牌也不该被打出来
    logs: list[str] = []

    sota.do_checkin(client, log=logs.append)

    text = "\n".join(logs)
    assert sota.CHECKIN_ROUTE in text
    assert "今日已签到，今日奖励 $300.00" in text
    assert "SuperSecretSotaToken" not in text
    assert "Bearer" not in text
