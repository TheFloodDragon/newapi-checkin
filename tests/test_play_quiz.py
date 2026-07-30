# -*- coding: utf-8 -*-
"""极速蹬「每日答题」链路回归。

答题的题库、判定与两种传输全部收在 scripts/checkin/jisudeng.py，本文件覆盖接线与
判定，不覆盖答案对错：题库本身是人工维护的知识，测试只保证「按选项原文匹配」
「未收录时降级并记录」「各种服务端回执被正确归类」这几条契约。

站点真实回执（www.jisudeng.com 实测）：
  GET  /api/v1/play/quiz/today  → data{enabled, questions:[{id,prompt,options}],
                                       already_submitted, reward_per_correct}
  POST /api/v1/play/quiz/submit → data{score, total, reward_amount, reward_type, coupon?}
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKIN_DIR = REPO_ROOT / "scripts" / "checkin"
if str(CHECKIN_DIR) not in sys.path:
    sys.path.insert(0, str(CHECKIN_DIR))

from browser import script_loader  # noqa: E402

# 与运行期同一条加载路径（校验相对路径 + 每次重新执行），避免测试走了另一套导入。
quiz = script_loader.load_site_script("scripts/checkin/jisudeng.py")


class FakeHelpers:
    def __init__(self, origin: str = "https://site.invalid") -> None:
        self.origin = origin
        self.logs: list[str] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    def resolve_url(self, path: str) -> str:
        return self.origin + path


class FakePage:
    """按 path 回放 evaluate 结果，并记录提交的 body。"""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, Any]] = []

    async def evaluate(self, _js: str, args: Any) -> Any:
        _origin, path, body = args
        self.calls.append((path, body))
        reply = self.replies.get(path)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeClient:
    """纯 HTTP 传输的替身：按相对路由回放 Sub2ApiClient.request 的返回。"""

    def __init__(self, replies: dict[str, Any]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, body: Any = None, **_kw: Any) -> Any:
        self.calls.append((method, path, body))
        reply = self.replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply


def _ok(data: Any, code: str = "0") -> dict[str, Any]:
    return {"ok": True, "status": 200, "code": code, "message": "success", "data": data}


QUESTIONS = [
    {"id": 1, "prompt": "Which HTTP method is typically used to send a chat completion request?",
     "options": ["GET", "POST", "DELETE", "OPTIONS"]},
    {"id": 7, "prompt": "What does RPM commonly limit?",
     "options": ["Random password minimum", "Requests per minute", "Rows per model"]},
]


# ── 题库与选项匹配 ───────────────────────────────────────────────────────────
def test_answer_matches_option_text_not_index() -> None:
    """选项顺序由服务端给出，题库必须按原文匹配，换序后仍要选对。"""
    shuffled = {"id": 1, "prompt": QUESTIONS[0]["prompt"],
                "options": ["OPTIONS", "DELETE", "POST", "GET"]}
    index, known = quiz.choose_index(shuffled)
    assert known is True
    assert shuffled["options"][index] == "POST"


def test_prompt_matching_ignores_case_and_punctuation() -> None:
    index, known = quiz.choose_index(
        {"id": 7, "prompt": "  what does RPM commonly limit  ", "options": ["Rows per model", "Requests per minute"]}
    )
    assert (known, index) == (True, 1)


def test_unknown_question_falls_back_to_longest_option() -> None:
    index, known = quiz.choose_index(
        {"id": 99, "prompt": "全新题目", "options": ["短", "稍微长一点的选项", "中等"]}
    )
    assert known is False
    assert index == 1


def test_unknown_questions_are_recorded(tmp_path, monkeypatch) -> None:
    log = tmp_path / "unknown.json"
    monkeypatch.setattr(quiz, "UNKNOWN_LOG", log)
    quiz.record_unknown([{"prompt": "新题 A", "options": ["a", "b"]}])
    quiz.record_unknown([{"prompt": "新题 A", "options": ["a", "b"]}, {"prompt": "新题 B", "options": ["c"]}])
    saved = json.loads(log.read_text(encoding="utf-8"))
    assert [item["prompt"] for item in saved] == ["新题 A", "新题 B"], "同一题面不应重复记录"


# ── 浏览器传输：run_quiz 状态机 ───────────────────────────────────────────────
def test_submits_chosen_answers_and_reports_score() -> None:
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": False,
                                   "questions": QUESTIONS, "reward_per_correct": 0.1}),
        quiz.QUIZ_SUBMIT_PATH: _ok({"score": 2, "total": 2, "reward_amount": 0.2, "reward_type": "balance"}),
    })
    outcome = asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))
    assert outcome["outcome"] == "submitted"
    assert (outcome["score"], outcome["total"], outcome["unknown"]) == (2, 2, 0)
    assert "$0.20" in outcome["message"]
    submitted = [body for path, body in page.calls if path == quiz.QUIZ_SUBMIT_PATH]
    assert submitted == [{"answers": [{"question_id": 1, "choice_index": 1},
                                      {"question_id": 7, "choice_index": 1}]}]


def test_coupon_reward_is_not_shown_in_message() -> None:
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": False, "questions": QUESTIONS}),
        quiz.QUIZ_SUBMIT_PATH: _ok({"score": 2, "total": 2, "reward_amount": 0,
                                    "reward_type": "coupon", "coupon": {"name": "新订阅减免0.5"}}),
    })
    outcome = asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))
    # 奖励是优惠券时既不报金额（reward_amount 为 0），也不把券名拼进消息
    assert outcome["message"] == "答题 2/2"
    assert outcome["reward_type"] == "coupon"


def test_unknown_question_is_printed_with_options(tmp_path, monkeypatch) -> None:
    """未收录的题必须打到日志：只落缓存文件的话用户不会知道今天在猜。"""
    monkeypatch.setattr(quiz, "UNKNOWN_LOG", tmp_path / "unknown.json")
    fresh = {"id": 42, "prompt": "全新题目 X", "options": ["短", "明显更长的那个选项"]}
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": False, "questions": [fresh]}),
        quiz.QUIZ_SUBMIT_PATH: _ok({"score": 1, "total": 1, "reward_amount": 0.1, "reward_type": "balance"}),
    })
    helpers = FakeHelpers()
    outcome = asyncio.run(quiz.run_quiz(page, helpers, "https://site.invalid"))
    logged = "\n".join(helpers.logs)
    assert "全新题目 X" in logged
    assert "明显更长的那个选项" in logged
    assert "[1]*" in logged, "应标出猜的是哪个选项"
    assert outcome["unknown_prompts"] == ["全新题目 X"]


def test_already_submitted_skips_submit() -> None:
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": True,
                                   "questions": QUESTIONS, "previous_score": 4, "previous_total": 5}),
    })
    outcome = asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))
    assert outcome["outcome"] == "already_done"
    assert all(path != quiz.QUIZ_SUBMIT_PATH for path, _ in page.calls)


def test_disabled_site_reports_disabled() -> None:
    page = FakePage({quiz.QUIZ_TODAY_PATH: _ok({"enabled": False})})
    assert asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))["outcome"] == "disabled"


def test_submit_already_done_code_is_not_error() -> None:
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": False, "questions": QUESTIONS}),
        quiz.QUIZ_SUBMIT_PATH: {"ok": False, "status": 400, "code": "PLAY_QUIZ_ALREADY_DONE",
                                "message": "already done", "data": None},
    })
    assert asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))["outcome"] == "already_done"


def test_token_unavailable_reports_error() -> None:
    page = FakePage({quiz.QUIZ_TODAY_PATH: {"ok": False, "status": 0, "reason": "no_token"}})
    outcome = asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))
    assert outcome["outcome"] == "error"
    assert "no_token" in outcome["message"]


def test_page_exception_does_not_propagate() -> None:
    page = FakePage({quiz.QUIZ_TODAY_PATH: RuntimeError("boom")})
    assert asyncio.run(quiz.run_quiz(page, FakeHelpers(), "https://site.invalid"))["outcome"] == "error"


# ── 与签到结果的合并（浏览器路径）─────────────────────────────────────────────
@pytest.mark.parametrize("status", ["need_login", "need_verification", "error"])
def test_quiz_skipped_when_checkin_failed(status: str) -> None:
    result = {"status": status, "message": "签到失败", "detail": {}}
    merged = asyncio.run(quiz._attach_quiz(FakePage({}), FakeHelpers(), dict(result)))
    assert merged == result, "签到未成立时不应触发答题，也不应改写结论"


def test_quiz_failure_never_changes_checkin_status(monkeypatch) -> None:
    async def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("quiz down")

    monkeypatch.setattr(quiz, "run_quiz", boom)
    merged = asyncio.run(quiz._attach_quiz(FakePage({}), FakeHelpers(), {"status": "success", "message": "签到成功"}))
    assert merged["status"] == "success"
    assert merged["message"] == "签到成功", "答题异常不得污染签到消息"
    assert merged["detail"]["quiz"]["outcome"] == "error"


def test_quiz_result_is_appended_to_message(monkeypatch) -> None:
    async def ok(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"outcome": "submitted", "message": "答题 5/5", "score": 5, "total": 5}

    monkeypatch.setattr(quiz, "run_quiz", ok)
    merged = asyncio.run(quiz._attach_quiz(FakePage({}), FakeHelpers(), {"status": "already_done", "message": "今日已签到。"}))
    # 签到消息自带句号，拼接时不能出现「。；」
    assert merged["message"] == "今日已签到；答题 5/5"
    assert merged["detail"]["quiz"]["score"] == 5


def test_merge_message_handles_empty_and_trailing_period() -> None:
    assert quiz.merge_message("今日已签到。", "答题 5/5") == "今日已签到；答题 5/5"
    assert quiz.merge_message("签到成功", "") == "签到成功"
    assert quiz.merge_message("", "答题 5/5") == "答题 5/5"


# ── 纯 HTTP 传输：run_play_quiz_http ──────────────────────────────────────────
def test_http_quiz_submits_and_summarizes() -> None:
    client = FakeClient({
        quiz.QUIZ_TODAY_ROUTE: {"code": 0, "data": {"enabled": True, "already_submitted": False,
                                                    "questions": QUESTIONS}},
        quiz.QUIZ_SUBMIT_ROUTE: {"code": 0, "data": {"score": 2, "total": 2,
                                                     "reward_amount": 0.2, "reward_type": "balance"}},
    })
    outcome = quiz.run_play_quiz_http(client)
    assert outcome["outcome"] == "submitted"
    assert "$0.20" in outcome["message"]
    assert client.calls[-1] == ("POST", quiz.QUIZ_SUBMIT_ROUTE,
                                {"answers": [{"question_id": 1, "choice_index": 1},
                                             {"question_id": 7, "choice_index": 1}]})


def test_http_quiz_returns_none_when_site_has_no_feature() -> None:
    """没有该功能的站点端点 404，应当被当作「无此项」而不是失败。"""
    from providers.base import ApiError

    client = FakeClient({quiz.QUIZ_TODAY_ROUTE: ApiError(404, None, "404 page not found")})
    assert quiz.run_play_quiz_http(client) is None
    assert quiz.run_http_extras(client) == {}


def test_http_quiz_maps_already_done_reason() -> None:
    from providers.base import ApiError

    client = FakeClient({
        quiz.QUIZ_TODAY_ROUTE: {"code": 0, "data": {"enabled": True, "already_submitted": False,
                                                    "questions": QUESTIONS}},
        quiz.QUIZ_SUBMIT_ROUTE: ApiError(
            None, {"code": 400, "reason": "PLAY_QUIZ_ALREADY_DONE", "message": "already"}, "already"
        ),
    })
    assert quiz.run_play_quiz_http(client)["outcome"] == "already_done"


def test_http_extras_wraps_summary_under_quiz_key() -> None:
    client = FakeClient({
        quiz.QUIZ_TODAY_ROUTE: {"code": 0, "data": {"enabled": True, "already_submitted": True,
                                                    "previous_score": 5, "previous_total": 5}},
    })
    extras = quiz.run_http_extras(client)
    assert set(extras) == {"quiz"}
    assert extras["quiz"]["outcome"] == "already_done"


# ── 通用层钩子：providers 只按约定调用脚本，不认识「答题」──────────────────────
def _site(**kw: Any):
    from providers.base import SiteConfig

    base = {"name": "t", "base_url": "https://t.invalid", "script": "scripts/checkin/jisudeng.py"}
    base.update(kw)
    return SiteConfig(**base)


def test_extras_hook_merges_into_result(monkeypatch) -> None:
    from providers.actions import browser_script
    from providers.base import CheckinResult

    monkeypatch.setattr(quiz, "run_http_extras",
                        lambda _client, log=None: {"quiz": {"outcome": "submitted", "message": "答题 5/5"}})
    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: quiz)

    result = CheckinResult("t", "https://t.invalid", "success", "签到成功。", detail={"a": 1})
    merged = browser_script._run_http_extras(_site(), object(), result)
    assert merged.message == "签到成功；答题 5/5"
    assert merged.detail["quiz"]["outcome"] == "submitted"
    assert merged.detail["a"] == 1, "不得覆盖签到自己的 detail"


def test_extras_hook_skipped_when_checkin_not_conclusive(monkeypatch) -> None:
    from providers.actions import browser_script
    from providers.base import CheckinResult

    def explode(_path: str) -> Any:
        raise AssertionError("签到未成立时不该加载脚本")

    monkeypatch.setattr(script_loader, "load_site_script", explode)
    result = CheckinResult("t", "https://t.invalid", "need_login", "登录失效")
    assert browser_script._run_http_extras(_site(), object(), result) is result


def test_extras_hook_ignores_scripts_without_the_hook(monkeypatch) -> None:
    from providers.actions import browser_script
    from providers.base import CheckinResult

    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: object())
    result = CheckinResult("t", "https://t.invalid", "success", "签到成功")
    merged = browser_script._run_http_extras(_site(), object(), result)
    assert merged.message == "签到成功"
    assert merged.detail is None


def test_extras_hook_failure_never_changes_checkin_status(monkeypatch) -> None:
    from providers.actions import browser_script
    from providers.base import CheckinResult

    def boom(_client: Any, log: Any = None) -> dict[str, Any]:
        raise RuntimeError("extras down")

    monkeypatch.setattr(quiz, "run_http_extras", boom)
    monkeypatch.setattr(script_loader, "load_site_script", lambda _path: quiz)
    result = CheckinResult("t", "https://t.invalid", "success", "签到成功")
    merged = browser_script._run_http_extras(_site(), object(), result)
    assert merged.status == "success"
    assert merged.message == "签到成功", "附加任务异常不得污染签到消息"
    assert merged.detail["extras"]["outcome"] == "error"


def test_extras_hook_skipped_without_script_path(monkeypatch) -> None:
    """纯 API 站点（没有 script 字段）不该尝试加载脚本。"""
    from providers.actions import browser_script
    from providers.base import CheckinResult

    def explode(_path: str) -> Any:
        raise AssertionError("没有 script 时不该加载")

    monkeypatch.setattr(script_loader, "load_site_script", explode)
    result = CheckinResult("t", "https://t.invalid", "success", "签到成功")
    assert browser_script._run_http_extras(_site(script=""), object(), result) is result
