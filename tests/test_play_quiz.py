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


@pytest.fixture(autouse=True)
def _isolated_learning_bank(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """每条测试使用独立学习库，绝不读写开发机的真实答题历史。"""
    monkeypatch.setattr(quiz, "LEARNED_BANK", tmp_path / "learned.json")


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


# ── 题面末尾的「（第N题）」序号 ───────────────────────────────────────────────
_CN_CASES = [
    ("以下哪项属于客户端应避免的行为", 7,
     ["校验参数后再发请求", "遇到 429 时退避重试", "把密钥硬编码到前端公开代码", "记录关键错误日志"],
     "把密钥硬编码到前端公开代码"),
    ("流式输出（streaming）最主要的用户价值是", 6,
     ["首字节更快、边生成边展示", "保证永不失败", "自动修复业务逻辑", "免除计费"],
     "首字节更快、边生成边展示"),
    ("幂等键（Idempotency-Key）的主要作用是", 8,
     ["提升图片清晰度", "防止重试造成重复扣费或重复创建", "绕过权限校验", "跳过日志记录"],
     "防止重试造成重复扣费或重复创建"),
    ("在生产环境中处理超时，推荐做法是", 6,
     ["无限等待", "直接忽略错误", "设置超时并做有限重试", "每次都重启服务"],
     "设置超时并做有限重试"),
    ("提示词前缀缓存（Prompt Cache）的主要收益是", 10,
     ["提高重复上下文请求的速度并降低成本", "关闭流式输出", "禁用鉴权", "强制返回 HTML"],
     "提高重复上下文请求的速度并降低成本"),
]


@pytest.mark.parametrize(("stem", "number", "options", "expected"), _CN_CASES)
def test_recorded_chinese_questions_are_answered(
    stem: str, number: int, options: list[str], expected: str
) -> None:
    """站点把题号拼进题面，补录的中文题必须能带序号命中。"""
    index, known = quiz.choose_index({"id": 1, "prompt": f"{stem}（第{number}题）", "options": options})
    assert known is True
    assert options[index] == expected


@pytest.mark.parametrize("number", [1, 3, 99])
def test_answer_survives_question_renumbering(number: int) -> None:
    """同一道题换位置就换序号；序号进了键，题库第二天就再也匹配不上。"""
    stem, _n, options, expected = _CN_CASES[0]
    shuffled = list(reversed(options))
    index, known = quiz.choose_index({"id": 1, "prompt": f"{stem}（第{number}题）", "options": shuffled})
    assert known is True
    assert shuffled[index] == expected


def test_answer_matches_prompt_without_number() -> None:
    stem, _n, options, expected = _CN_CASES[0]
    index, known = quiz.choose_index({"id": 1, "prompt": stem, "options": options})
    assert known is True
    assert options[index] == expected


def test_number_suffix_stripping_does_not_swallow_real_text() -> None:
    """只剥「末尾」的题号；题面里正常出现的括号内容不能被吃掉。"""
    assert quiz._norm_prompt("以下哪项属于客户端应避免的行为（第7题）") == quiz._norm_prompt(
        "以下哪项属于客户端应避免的行为"
    )
    assert quiz._norm_prompt("流式输出（streaming）最主要的用户价值是") != quiz._norm_prompt(
        "流式输出最主要的用户价值是"
    )


# ── 相似度匹配语义 ───────────────────────────────────────────────────────────
def test_similarity_tolerates_rewritten_prompt_and_option() -> None:
    """站点改写题面/选项措辞后仍要答对——这正是不用精确相等匹配的原因。"""
    index, known = quiz.choose_index(
        {
            "id": 3,
            "prompt": "问题 3：以下哪项属于客户端应尽量避免的行为？（第2题）",
            "options": ["记录关键错误日志", "把密钥硬编码到前端公开代码里", "遇到 429 时退避重试"],
        }
    )
    assert known is True
    assert index == 1


def test_low_similarity_prompt_is_treated_as_new_question() -> None:
    """相似度不足必须判为新题去猜并记录，不能硬套到最像的老题上。"""
    assert quiz.match_answer("如何选择向量数据库的索引类型") is None
    index, known = quiz.choose_index(
        {"id": 99, "prompt": "如何选择向量数据库的索引类型", "options": ["短", "更长一些的选项"]}
    )
    assert known is False
    assert index == 1, "退化路径仍是选最长选项"


def test_bank_entries_are_separable_by_threshold() -> None:
    """题库内不同题之间的相似度必须明显低于阈值，否则会互相串答案。"""
    prompts = [quiz._norm_prompt(p) for p in quiz.ANSWERS]
    worst = max(
        quiz.similarity(a, b)
        for i, a in enumerate(prompts)
        for j, b in enumerate(prompts)
        if i < j
    )
    assert worst < quiz.PROMPT_SIMILARITY_MIN, f"存在过于相似的题库条目：{worst:.3f}"


def test_every_bank_entry_resolves_itself() -> None:
    """每条题库条目都必须能用自己的题面命中自己的答案（防录入笔误）。"""
    for prompt, answer in quiz.ANSWERS.items():
        assert quiz.match_answer(prompt) == answer, prompt
        assert quiz.match_answer(f"{prompt}（第3题）") == answer, prompt


def test_verified_2026_07_quiz_answers_are_recorded() -> None:
    """实测未知题按带星选项提交后 5/5，固化该批服务端已验证答案。"""
    verified = {
        "奖池版本的价值是什么": "方便复盘不同配置效果",
        "提交客服反馈时最好提供什么": "错误截图、时间和请求信息",
        "签到失败提示应该包含什么": "失败原因和可操作下一步",
        "API Key 泄露后第一步应该做什么": "立即禁用或重置密钥",
        "请求接口时 Header 里的 Authorization 通常放什么": "访问令牌或 API Key",
    }
    for prompt, answer in verified.items():
        options = ["无关选项", answer, "另一个无关选项", "短"]
        index, known = quiz.choose_index({"id": prompt, "prompt": prompt, "options": options})
        assert known is True
        assert index == 1


def _attempt(question_id: int, prompt: str, options: list[str], choice_index: int) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "prompt": prompt,
        "options": options,
        "choice_index": choice_index,
        "choice": options[choice_index],
    }


def test_perfect_score_learns_answers_and_reuses_after_option_shuffle() -> None:
    attempts = [
        _attempt(1, "学习题 A", ["错误", "已验证答案 A", "短"], 1),
        _attempt(2, "学习题 B", ["已验证答案 B", "错误", "短"], 0),
    ]

    learned = quiz.record_submit_learning({"score": 2, "total": 2}, attempts)

    assert learned == {"correct": 2, "incorrect": 0, "unresolved": 0, "saved": True}
    bank = quiz.load_learning_bank()
    assert bank["questions"][quiz._norm_prompt("学习题 A")]["correct_answer"] == "已验证答案 A"
    shuffled = {"id": 1, "prompt": "学习题 A（第8题）", "options": ["短", "错误", "已验证答案 A"]}
    assert quiz.choose_index(shuffled) == (2, True)


def test_zero_score_records_wrong_choice_and_excludes_it_next_time() -> None:
    attempt = _attempt(1, "排除错误选项题", ["明显最长但错误的选项", "候选答案", "短"], 0)

    learned = quiz.record_submit_learning({"score": 0, "total": 1}, [attempt])
    index, known = quiz.choose_index(
        {"id": 1, "prompt": "排除错误选项题", "options": ["明显最长但错误的选项", "候选答案", "短"]}
    )

    assert learned["incorrect"] == 1
    assert index == 1, "已确认错误的最长项必须被排除"
    assert known is False, "仍有两个候选时不能伪装成已确定答案"


def test_partial_score_without_details_only_records_unresolved() -> None:
    attempts = [
        _attempt(1, "部分得分题 A", ["A0", "A1"], 0),
        _attempt(2, "部分得分题 B", ["B0", "B1"], 1),
    ]

    learned = quiz.record_submit_learning({"score": 1, "total": 2}, attempts)
    bank = quiz.load_learning_bank()

    assert learned == {"correct": 0, "incorrect": 0, "unresolved": 2, "saved": True}
    for entry in bank["questions"].values():
        assert not entry["correct_answer"]
        assert not entry["wrong_answers"]
        assert entry["history"][-1]["result"] == "unresolved"


def test_per_question_results_learn_mixed_outcomes_and_server_answer() -> None:
    attempts = [
        _attempt(11, "混合反馈题 A", ["猜错", "干扰", "服务端正确答案"], 0),
        _attempt(12, "混合反馈题 B", ["干扰", "本次答对"], 1),
    ]
    data = {
        "score": 1,
        "total": 2,
        "question_results": [
            {"question_id": 11, "is_correct": False, "correct_index": 2},
            {"question_id": 12, "correct": True},
        ],
    }

    learned = quiz.record_submit_learning(data, attempts)
    bank = quiz.load_learning_bank()["questions"]

    assert learned == {"correct": 1, "incorrect": 1, "unresolved": 0, "saved": True}
    first = bank[quiz._norm_prompt("混合反馈题 A")]
    second = bank[quiz._norm_prompt("混合反馈题 B")]
    assert first["correct_answer"] == "服务端正确答案"
    assert first["wrong_answers"] == ["猜错"]
    assert second["correct_answer"] == "本次答对"


def test_learning_history_is_bounded_and_corrupt_file_recovers() -> None:
    quiz.LEARNED_BANK.write_text("{broken", encoding="utf-8")
    attempt = _attempt(1, "历史上限题", ["错", "对"], 1)
    for _ in range(quiz.LEARNED_HISTORY_LIMIT + 5):
        assert quiz.record_submit_learning({"score": 1, "total": 1}, [attempt])["saved"] is True

    entry = quiz.load_learning_bank()["questions"][quiz._norm_prompt("历史上限题")]
    assert len(entry["history"]) == quiz.LEARNED_HISTORY_LIMIT
    assert entry["correct_answer"] == "对"


def test_ambiguous_option_match_is_treated_as_unknown() -> None:
    """选项里定不到唯一答案时必须去猜并记录，不能随便挑一个充当已知答案。"""
    answer = quiz.ANSWERS["提示词前缀缓存（Prompt Cache）的主要收益是"]
    assert quiz._option_index([answer, answer], answer) is None
    index, known = quiz.choose_index(
        {"id": 4, "prompt": "提示词前缀缓存（Prompt Cache）的主要收益是", "options": [answer, answer]}
    )
    assert known is False
    assert index == 0


def test_similarity_uses_coverage_for_long_text_only() -> None:
    """长文本用覆盖率兜住「被完整包含但更长」；短串只认对称 ratio，避免虚高。"""
    # 长题面被完整包含：ratio 会被长度差拉低，覆盖率补上
    assert quiz.similarity("以下哪项属于客户端应避免的行为", "问题3：以下哪项属于客户端应避免的行为") == 1.0
    # 短串不吃覆盖率红利：post 与 options 只有零散字符相同
    assert quiz.similarity("post", "options") < quiz.OPTION_SIMILARITY_MIN


def test_similarity_handles_empty_input() -> None:
    assert quiz.similarity("", "abc") == 0.0
    assert quiz.match_answer("") is None
    assert quiz.match_answer(None) is None


def test_unknown_questions_are_recorded(tmp_path, monkeypatch) -> None:
    log = tmp_path / "unknown.json"
    monkeypatch.setattr(quiz, "UNKNOWN_LOG", log)
    quiz.record_unknown([{"prompt": "新题 A", "options": ["a", "b"]}])
    quiz.record_unknown([{"prompt": "新题 A", "options": ["a", "b"]}, {"prompt": "新题 B", "options": ["c"]}])
    saved = json.loads(log.read_text(encoding="utf-8"))
    assert [item["prompt"] for item in saved] == ["新题 A", "新题 B"], "同一题面不应重复记录"


def test_unknown_dedup_ignores_question_number(tmp_path, monkeypatch) -> None:
    """同一道未收录题换了序号不该再记一条，否则人工补录时满屏重复题。"""
    log = tmp_path / "unknown.json"
    monkeypatch.setattr(quiz, "UNKNOWN_LOG", log)
    quiz.record_unknown([{"prompt": "尚未收录的题（第2题）", "options": ["a", "b"]}])
    quiz.record_unknown([{"prompt": "尚未收录的题（第9题）", "options": ["a", "b"]}])
    saved = json.loads(log.read_text(encoding="utf-8"))
    assert len(saved) == 1


# ── 浏览器传输：run_quiz 状态机 ───────────────────────────────────────────────
def test_submits_chosen_answers_and_reports_score() -> None:
    page = FakePage({
        quiz.QUIZ_TODAY_PATH: _ok({"enabled": True, "already_submitted": False,
                                   "questions": QUESTIONS, "reward_per_correct": 0.1}),
        quiz.QUIZ_SUBMIT_PATH: _ok({"score": 2, "total": 2, "reward_amount": 0.2, "reward_type": "balance"}),
    })
    helpers = FakeHelpers()
    outcome = asyncio.run(quiz.run_quiz(page, helpers, "https://site.invalid"))
    assert outcome["outcome"] == "submitted"
    assert (outcome["score"], outcome["total"], outcome["unknown"]) == (2, 2, 0)
    assert outcome["learning"] == {"correct": 2, "incorrect": 0, "unresolved": 0, "saved": True}
    assert any("学习题库：确认正确 2" in line for line in helpers.logs)
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
    logs: list[str] = []
    outcome = quiz.run_play_quiz_http(client, log=logs.append)
    assert outcome["outcome"] == "submitted"
    assert outcome["learning"]["correct"] == 2
    assert any("学习题库：确认正确 2" in line for line in logs)
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
