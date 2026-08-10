# -*- coding: utf-8 -*-
"""VC API 幸运轮盘脚本回归。"""

from __future__ import annotations

from typing import Any

import pytest

from browser import script_loader
from providers.base import ApiError, SiteConfig

lottery = script_loader.load_site_script("scripts/checkin/vcnovb_lottery.py")


class FakeClient:
    quota_is_usd = True

    def __init__(self, replies: dict[tuple[str, str], Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Any, dict[str, Any]]] = []
        self.base_url = "https://sub.vcnovb.cn"
        self.site = SiteConfig(
            name="VC API 幸运轮盘",
            base_url=self.base_url,
            site_profile="sub2api",
            auth_method="browser",
            checkin_action="browser_script",
            script="scripts/checkin/vcnovb_lottery.py",
            script_args={"email": "user@example.test", "password": "SecretPassword"},
        )

    def request(self, method: str, path: str, body: Any = None, **kwargs: Any) -> Any:
        self.calls.append((method, path, body, kwargs))
        reply = self.replies[(method, path)]
        if isinstance(reply, list):
            reply = reply.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _summary(*, remaining: int = 1, active: bool = True, period: str = "d:2026-08-10") -> dict[str, Any]:
    return {
        "code": 0,
        "data": {
            "pools": [
                {
                    "pool": {"id": 1, "key": "normal", "name": "普通抽奖", "enabled": True},
                    "base_remaining": remaining,
                    "extra_remaining": 0,
                    "period_key": period,
                    "active": active,
                }
            ]
        },
    }


def _record(
    *,
    outcome: str = "win",
    prize_type: str = "subscription",
    name: str = "尝鲜套餐",
    balance: float | None = None,
    days: int | None = 30,
) -> dict[str, Any]:
    return {
        "id": 10601,
        "pool_id": 1,
        "pool_key": "normal",
        "outcome": outcome,
        "chance_source": "base",
        "prize_id": 3 if outcome != "none" else None,
        "prize": {
            "id": 3,
            "name": name,
            "prize_type": prize_type,
            "balance_amount": balance,
            "validity_days": days,
        } if outcome != "none" else None,
        "base_remaining": 0,
        "extra_remaining": 0,
        "created_at": "2026-08-10T14:12:16.188432+08:00",
    }


def _history(record: dict[str, Any]) -> dict[str, Any]:
    return {"code": 0, "data": {"items": [record], "total": 1}}


def test_subscription_draw_uses_idempotency_header_and_returns_result() -> None:
    record = _record()
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(),
        ("POST", lottery.DRAW_ROUTE): {"code": 0, "data": record},
    })
    logs: list[str] = []

    reward = lottery.do_checkin(client, log=logs.append)

    assert reward.already_done is False
    assert reward.quota_awarded is None
    assert reward.extra["result_message"] == "抽奖成功：尝鲜套餐（30 天）"
    assert reward.extra["lottery_prize_name"] == "尝鲜套餐"
    post = next(call for call in client.calls if call[0] == "POST")
    key = post[3]["extra_headers"]["Idempotency-Key"]
    assert key.startswith("vcnovb-lottery-")
    assert key == lottery._idempotency_key(client, lottery._pool_state(_summary()))
    assert "SecretPassword" not in "\n".join(logs)
    assert "user@example.test" not in "\n".join(logs)


def test_balance_draw_reports_usd_award() -> None:
    record = _record(prize_type="balance", name="1元余额", balance=1, days=None)
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(),
        ("POST", lottery.DRAW_ROUTE): {"code": 0, "data": record},
    })

    reward = lottery.do_checkin(client)

    assert reward.quota_awarded == 1
    assert reward.extra["result_message"] == "抽奖成功：1元余额（+$1.00）"
    assert reward.extra["balance_amount"] == 1


def test_no_remaining_chance_returns_todays_history_without_post() -> None:
    record = _record()
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(remaining=0),
        ("GET", lottery.HISTORY_ROUTE): _history(record),
    })

    reward = lottery.do_checkin(client)

    assert reward.already_done is True
    assert reward.quota_awarded is None
    assert reward.extra["result_message"] == "今日已抽取：尝鲜套餐（30 天）"
    assert reward.extra["completion_signal"] == "lottery_history"
    assert not any(method == "POST" for method, _path, _body, _kwargs in client.calls)


def test_no_chance_race_recovers_from_history() -> None:
    record = _record(prize_type="balance", name="5元余额", balance=5, days=None)
    no_chance = ApiError(
        400,
        {"code": 400, "message": "no lottery chance available", "reason": "LOTTERY_NO_CHANCE"},
        "no lottery chance available",
    )
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(),
        ("POST", lottery.DRAW_ROUTE): no_chance,
        ("GET", lottery.HISTORY_ROUTE): _history(record),
    })

    reward = lottery.do_checkin(client)

    assert reward.already_done is True
    assert reward.extra["result_message"] == "今日已抽取：5元余额（+$5.00）"


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record(outcome="none", prize_type="", name="", balance=None, days=None), "抽奖完成：未中奖"),
        (
            _record(outcome="blessing", prize_type="blessing", name="祝你今天顺利", days=None),
            "抽奖完成：祝你今天顺利",
        ),
    ],
)
def test_non_winning_outcomes_have_readable_messages(record: dict[str, Any], expected: str) -> None:
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(),
        ("POST", lottery.DRAW_ROUTE): {"code": 0, "data": record},
    })

    assert lottery.do_checkin(client).extra["result_message"] == expected


def test_history_must_match_current_period() -> None:
    old = _record()
    old["created_at"] = "2026-08-09T12:00:00+08:00"
    client = FakeClient({
        ("GET", lottery.SUMMARY_ROUTE): _summary(remaining=0),
        ("GET", lottery.HISTORY_ROUTE): _history(old),
    })

    reward = lottery.do_checkin(client)

    assert reward.already_done is True
    assert "draw_id" not in reward.extra
    assert reward.extra["result_message"] == "今日已抽取（历史结果不可用）"


def test_inactive_pool_is_not_reported_as_checked_in() -> None:
    client = FakeClient({("GET", lottery.SUMMARY_ROUTE): _summary(active=False)})

    with pytest.raises(ApiError, match="当前未开放"):
        lottery.do_checkin(client)
