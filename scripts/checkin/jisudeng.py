#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""极速蹬（jisudeng.com）每日签到 + 每日答题 browser_script。

该站点是 Sub2API 系（Vue SPA + Cloudflare Turnstile 登录），与百倍
（100xlabs）同构，因此签到的共享逻辑集中在 scripts/checkin/_sub2api_common.py，
本文件声明站点差异（签到端点、按钮文案、截图前缀等）并串起主流程。

站点特征：
- 可签到按钮：立即签到
- 已签到状态：今日已签到
- 签到接口：POST /api/v1/play/checkin（补签 /makeup 需排除）
- 每日答题：GET/POST /api/v1/play/quiz/{today,submit}（本文件下半部分）

登录态优先复用 browser_state，过期时用 localStorage 的 refresh_token 刷新；
refresh_token 也失效时，可用 script_args 或环境变量中的邮箱密码在真实登录页
完成登录（只消费 Cloudflare 正常签发的 Turnstile 令牌，不伪造、不绕过）。
凭据不会写入账号配置、脚本结果或日志。

答题（题库、判定、两种传输）**全部收在本文件**：它只属于这个站点，散到仓库根或
providers 里会让「一个站点的特殊玩法」污染通用层，也让人找不到题库在哪。
通用层只提供两个钩子：``run``（浏览器）与 ``run_http_extras``（纯 HTTP）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# browser_script 运行器用 spec_from_file_location 加载本文件，父目录不在
# sys.path 上，因此显式加入后再导入同目录的共享模块。
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import _sub2api_common as common  # noqa: E402

SPEC = common.SiteSpec(
    site_label="极速蹬",
    checkin_path="/api/v1/play/checkin",
    status_path="/api/v1/play/checkin/status",
    login_reset_sentinel="__jsd_login_reset",
    screenshot_prefix="jisudeng",
    default_start_path="/check-in",
    email_env="JISUDENG_EMAIL",
    password_env="JISUDENG_PASSWORD",
    checkin_texts=("立即签到",),
    already_texts=("今日已签到", "已签到"),
    success_texts=("已到账", "签到成功"),
    response_match=("/play/checkin",),
    # /play/checkin/makeup 是补签接口，监听签到响应时必须排除。
    response_exclude=("/play/checkin/makeup",),
    # 极速蹬的已签到文案都足够具体，无需弱文案特例。
    weak_already_texts=(),
    success_message="极速蹬签到成功",
    # 极速蹬历史上统一用 already_state 表达「已签到」信号，保持不变。
    signal_already_control="already_state",
    signal_already_text="already_state",
    signal_post_click_text="already_state",
)

# ══════════════════════════ 每日答题（Quiz Quest）══════════════════════════
#
# 接口（实测）：
#     GET  /api/v1/play/quiz/today
#          → data{enabled, coupon_pool_ready, questions:[{id, prompt, options[]}],
#                 already_submitted, reward_per_correct, server_date,
#                 previous_score, previous_total, previous_reward, previous_reward_type}
#     POST /api/v1/play/quiz/submit  body {"answers":[{"question_id":N,"choice_index":I}]}
#          → data{score, total, reward_amount, reward_type, coupon?}
#
# 每天一次，重复提交回业务码 PLAY_QUIZ_ALREADY_DONE。答对一题得 reward_per_correct
# （实测 0.1），也可能改发优惠券（此时 reward_amount 为 0）。

API_PREFIX = "/api/v1"
# 相对 API 前缀的路由（纯 HTTP 客户端自带前缀）与完整路径（页内 fetch 直接用）。
QUIZ_TODAY_ROUTE = "/play/quiz/today"
QUIZ_SUBMIT_ROUTE = "/play/quiz/submit"
QUIZ_TODAY_PATH = API_PREFIX + QUIZ_TODAY_ROUTE
QUIZ_SUBMIT_PATH = API_PREFIX + QUIZ_SUBMIT_ROUTE

UNKNOWN_LOG = _HERE.parents[1] / ".cache-checkin" / "play_quiz_unknown.json"

# 题库：题面 → 正确选项原文。
#
# 为什么必须离线维护：取题接口**不返回**正确答案，提交结果也只给总分、不给逐题对错
# —— 既无法从题面推导，也无法靠提交反馈逐题学习（一天只有一次机会）。
#
# 两个刻意的设计选择：
# 1. 用题面而不是题目 id 作键：id 只是题库主键，站点改题库时可能复用；题面稳定且
#    自解释，人工补录时也不必先查 id。
# 2. 值存正确选项的原文而不是下标：选项顺序由服务端给出、不保证跨天一致，存下标
#    一旦顺序变化就会答错，且错得毫无征兆。
#
# 键值都会经 _norm 归一化后比较，所以照抄站点原文即可。
ANSWERS: dict[str, str] = {
    "Which HTTP method is typically used to send a chat completion request?": "POST",
    "Which field usually carries the user message in an OpenAI-style chat request?": "messages",
    "What is a common purpose of an API gateway?":
        "Route, authenticate, and meter upstream API traffic",
    "Why do providers cache prompt prefixes?":
        "To reduce latency and cost for repeated context",
    "What does RPM commonly limit?": "Requests per minute",
}


def _norm(text: Any) -> str:
    """归一化文本：小写、压缩空白、去掉首尾标点差异。"""
    return re.sub(r"\s+", " ", str(text or "").strip().lower()).strip(" .?:!")


_ANSWER_INDEX = {_norm(k): _norm(v) for k, v in ANSWERS.items()}


def summary(outcome: str, message: str, **extra: Any) -> dict[str, Any]:
    """统一摘要结构。

    字段名用 outcome 而不是 state：结果会经 mask_utils.sanitize_data 输出，
    而 "state" 命中脱敏词表（browser_state 同名前缀），会被整值替换成 <redacted>。
    """
    return {"outcome": outcome, "message": message, **extra}


def choose_index(question: Any) -> tuple[int, bool]:
    """给一道题选一个选项下标，返回 (下标, 是否命中题库)。

    未命中题库时退化为「选最长的选项」——干扰项通常明显更短，这只是比选第一个
    更好的猜法，不是答案。猜错只损失这一题的额度。
    """
    options = question.get("options") if isinstance(question, dict) else None
    if not isinstance(options, list) or not options:
        return 0, False
    wanted = _ANSWER_INDEX.get(_norm(question.get("prompt")))
    if wanted:
        for index, option in enumerate(options):
            if _norm(option) == wanted:
                return index, True
    longest = max(range(len(options)), key=lambda i: len(str(options[i] or "")))
    return longest, False


def record_unknown(questions: list[dict[str, Any]], path: Path | None = None) -> None:
    """把未收录的题面追加到缓存文件，供人工补录题库。写入失败不影响主流程。"""
    if not questions:
        return
    target = path or UNKNOWN_LOG
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = []
        if not isinstance(existing, list):
            existing = []
        seen = {_norm(item.get("prompt")) for item in existing if isinstance(item, dict)}
        for item in questions:
            key = _norm(item.get("prompt"))
            if key and key not in seen:
                seen.add(key)
                existing.append({"prompt": item.get("prompt"), "options": item.get("options")})
        target.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def describe_unknown(unknown: list[dict[str, Any]]) -> list[str]:
    """未收录题目的日志行：题面 + 全部选项，并标出这次猜了哪个。

    必须直接打出来。只写进缓存文件的话，用户看日志根本不知道今天有题在猜，
    也就不会去补题库。
    """
    if not unknown:
        return []
    lines = [f"答题有 {len(unknown)} 道题不在题库（已按最长选项猜，并记入 {UNKNOWN_LOG.name}）："]
    for question in unknown:
        options = question.get("options") if isinstance(question.get("options"), list) else []
        guess, _known = choose_index(question)
        lines.append(f"  题目：{question.get('prompt')}")
        for index, option in enumerate(options):
            lines.append(f"    [{index}]{'*' if index == guess else ' '} {option}")
    return lines


def plan(data: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """把 /quiz/today 的 data 变成 (提前结论, 待提交答案, 未收录题目)。

    提前结论非空表示不该提交（站点没开、今日已答、没有题目）。
    """
    if not isinstance(data, dict):
        return summary("error", "答题接口返回结构无法识别"), [], []
    if not data.get("enabled"):
        return summary("disabled", "站点未开启答题"), [], []
    if data.get("already_submitted"):
        score, total = data.get("previous_score"), data.get("previous_total")
        return summary("already_done", f"今日答题已完成（{score}/{total}）",
                       score=score, total=total, reward=data.get("previous_reward")), [], []
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        return summary("unavailable", "答题接口未返回题目"), [], []

    answers: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        index, known = choose_index(question)
        answers.append({"question_id": question.get("id"), "choice_index": index})
        if not known:
            unknown.append(question)
    return None, answers, unknown


def summarize_submit(response: dict[str, Any], unknown: list[dict[str, Any]]) -> dict[str, Any]:
    """把提交回执归一成摘要。response 形如 {ok, status, code, message, data}。"""
    if not response.get("ok"):
        code = str(response.get("code") or "")
        message = str(response.get("message") or response.get("reason") or f"HTTP {response.get('status')}")
        if "ALREADY" in code.upper() or "ALREADY" in message.upper():
            return summary("already_done", "今日答题已完成")
        return summary("error", f"提交答题失败：{message}", unknown=len(unknown))

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    score, total = data.get("score"), data.get("total")
    reward = data.get("reward_amount")
    text = f"答题 {score}/{total}"
    # 奖励可能是优惠券，此时 reward_amount 为 0；不写金额也不写券名。
    if isinstance(reward, (int, float)) and not isinstance(reward, bool) and reward > 0:
        text += f"，获得 ${float(reward):.2f}"
    return summary(
        "submitted", text,
        score=score, total=total, reward=reward,
        reward_type=data.get("reward_type"), unknown=len(unknown),
        unknown_prompts=[str(item.get("prompt") or "") for item in unknown],
    )


def merge_message(base: str, extra: str) -> str:
    """把答题结论拼到签到消息后面。

    签到消息通常自带句号（「今日已签到。」），直接拼会得到「今日已签到。；答题…」，
    所以先去掉尾部句号再用分号连接。
    """
    head = str(base or "").rstrip().rstrip("。.")
    tail = str(extra or "").strip()
    if not tail:
        return str(base or "")
    return f"{head}；{tail}" if head else tail


# ── 传输一：浏览器页内 fetch ─────────────────────────────────────────────────
# 复用 _sub2api_common 的页内鉴权状态机（读 localStorage 的 auth_token、401 时刷新
# 一次再重试），因此答题与签到走同一套登录态，不必另外导出 token。

_FETCH_JS = "async ([baseUrl, path, body]) => {\n" + common._PAGE_AUTH_REQUEST_HELPERS_JS + """
    try {
        const headers = { Accept: 'application/json' };
        const init = { credentials: 'include', headers };
        if (body !== null) {
            init.method = 'POST';
            headers['Content-Type'] = 'application/json';
            init.body = JSON.stringify(body);
        }
        const response = await requestWithAuth((accessToken) => fetch(baseUrl + path, {
            ...init,
            headers: { ...headers, Authorization: `Bearer ${accessToken}` },
        }));
        if (!response) return { ok: false, status: 0, reason: 'no_token' };
        const raw = await parseBody(response);
        const payload = raw && typeof raw.data === 'object' && raw.data ? raw.data : raw;
        return {
            ok: response.ok,
            status: response.status,
            code: raw && raw.code !== undefined ? String(raw.code) : '',
            message: raw && raw.message ? String(raw.message) : '',
            data: payload,
        };
    } catch (err) {
        return { ok: false, status: 0, reason: String(err) };
    }
}"""


async def _quiz_call(page: Any, origin: str, path: str, body: Any = None) -> dict[str, Any]:
    try:
        result = await page.evaluate(_FETCH_JS, [origin, path, body])
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "reason": f"{type(exc).__name__}: {exc}"}
    return result if isinstance(result, dict) else {"ok": False, "status": 0, "reason": "bad_result"}


async def run_quiz(page: Any, helpers: Any, origin: str) -> dict[str, Any]:
    """浏览器路径：完成一次每日答题，返回可直接塞进 detail["quiz"] 的摘要。

    任何失败都只是「答题这一项没拿到」，不影响签到结论，所以这里永不抛异常，
    统一用 outcome 表达：submitted / already_done / disabled / unavailable / error。
    """
    today = await _quiz_call(page, origin, QUIZ_TODAY_PATH)
    if not today.get("ok"):
        reason = today.get("reason") or today.get("message") or f"HTTP {today.get('status')}"
        return summary("error", f"读取答题失败：{reason}")

    early, answers, unknown = plan(today.get("data"))
    if early is not None:
        return early
    if unknown:
        record_unknown(unknown)
        for line in describe_unknown(unknown):
            common.log(helpers, line)

    submitted = await _quiz_call(page, origin, QUIZ_SUBMIT_PATH, {"answers": answers})
    return summarize_submit(submitted, unknown)


# ── 传输二：纯 HTTP（不启动浏览器）────────────────────────────────────────────
# providers/actions/browser_script.py 会先尝试纯 HTTP 签到，成功时根本不会启动浏览器。
# 若答题只写在浏览器路径里，一旦某天 token 仍有效、纯 API 直接签到成功，答题就被
# 整天跳过。因此这里再给一条纯 HTTP 通路，由通用层通过 run_http_extras 钩子调用。


def _is_missing_endpoint(exc: Any) -> bool:
    """站点没有该功能（端点 404/405）——应当当作「无此项」而不是失败。"""
    status = getattr(exc, "status", None)
    if status in {404, 405}:
        return True
    text = f"{getattr(exc, 'message', '')} {getattr(exc, 'payload', '')}".lower()
    return "404" in text or "not found" in text


def run_play_quiz_http(client: Any, log: Any = None) -> dict[str, Any] | None:
    """纯 HTTP 完成一次每日答题；站点无该功能返回 None。

    client 是 providers.profiles.sub2api.Sub2ApiClient，其 request() 自带 /api/v1
    前缀、cookie jar 与 refresh_token 续期，所以这里只给相对路由。
    """
    def _log(message: str) -> None:
        if log:
            log(message)

    def _unwrap(payload: Any) -> Any:
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            return payload["data"]
        return payload

    try:
        today = client.request("GET", QUIZ_TODAY_ROUTE)
    except Exception as exc:  # noqa: BLE001 - 附加任务不得把异常抛给签到流程
        if _is_missing_endpoint(exc):
            return None
        return summary("error", f"读取答题失败：{getattr(exc, 'message', exc)}")

    early, answers, unknown = plan(_unwrap(today))
    if early is not None:
        return early
    if unknown:
        record_unknown(unknown)
        for line in describe_unknown(unknown):
            _log(line)

    try:
        submitted = client.request("POST", QUIZ_SUBMIT_ROUTE, {"answers": answers})
    except Exception as exc:  # noqa: BLE001
        payload = getattr(exc, "payload", None)
        payload = payload if isinstance(payload, dict) else {}
        return summarize_submit(
            {
                "ok": False,
                "status": getattr(exc, "status", None),
                # 业务判据在 reason（如 PLAY_QUIZ_ALREADY_DONE），code 只是 HTTP 化的数字
                "code": str(payload.get("reason") or payload.get("code") or ""),
                "message": str(getattr(exc, "message", exc)),
            },
            unknown,
        )
    return summarize_submit({"ok": True, "data": _unwrap(submitted)}, unknown)


def run_http_extras(client: Any, log: Any = None) -> dict[str, dict[str, Any]]:
    """通用层钩子：纯 HTTP 路径签到成立后执行本站的附加日常任务。

    返回 {detail 键: 摘要}；摘要含 outcome/message，由通用层合并进结果。
    """
    quiz = run_play_quiz_http(client, log=log)
    return {"quiz": quiz} if quiz is not None else {}


# ══════════════════════════════ 签到主流程 ══════════════════════════════════


async def run(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """签到 + 每日答题。答题失败只体现在 detail，不改写签到结论。"""
    result = await _checkin(page, context, site, helpers)
    return await _attach_quiz(page, helpers, result)


async def _attach_quiz(page: Any, helpers: Any, result: dict[str, Any]) -> dict[str, Any]:
    """签到已成立时顺手完成答题，把结果合并进消息与 detail。"""
    if not isinstance(result, dict) or result.get("status") not in {"success", "already_done"}:
        return result
    try:
        outcome = await run_quiz(page, helpers, helpers.resolve_url("/").rstrip("/"))
    except Exception as exc:  # noqa: BLE001 - 答题异常绝不能影响签到结论
        outcome = summary("error", f"答题异常：{type(exc).__name__}: {exc}")
    common.log(helpers, f"答题：{outcome.get('message')}")
    detail = result.get("detail")
    if not isinstance(detail, dict):
        detail = {}
        result["detail"] = detail
    detail["quiz"] = outcome
    if outcome.get("outcome") in {"submitted", "already_done"}:
        result["message"] = merge_message(result.get("message", ""), str(outcome.get("message") or ""))
    return result


async def _checkin(page: Any, context: Any, site: Any, helpers: Any) -> dict[str, Any]:
    """恢复登录态后执行极速蹬每日签到。"""
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

    # 无限跳转根因修复（实测定位）：token 已过期但 localStorage 里 auth_user 残留时，
    # /dashboard 守卫判「未登录」踢去 /login，/login 守卫判「已登录」（auth_user 在）
    # 又踢回 /dashboard，两个路由守卫互踢形成无限跳转，且跳转期间页面执行上下文
    # 反复销毁、脚本 evaluate 全部失效。对策：导航前注入 init script，在
    # document_start 检查 token_expires_at，已过期则清掉全部 auth 键，让登录态一致
    # 地落为「已登出」，页面干净停在 /login，再交给账密登录兜底。
    await common.add_init_script(context, common.preflight_init_script())

    await common.navigate_and_settle(page, helpers, start_target, opts)

    # 登录闸门：只以页面是否真正落在 /login 为判据。不能主动用 localStorage 的
    # auth_token 探测 /auth/me——SPA 加载时会用 refresh_token 换出只存在内存的新
    # token，localStorage 里的旧 token 已被服务端失效，主动探测必得 401，会把
    # 「已正常登录」误判为未登录并触发不必要的账密兜底。
    login_attempted = False
    if await common.on_login_page(page):
        login_attempted = True
        login_result = await do_login()
        if login_result is not None:
            return login_result
        await common.navigate_and_settle(page, helpers, start_target, opts)
        if await common.on_login_page(page):
            return helpers.need_login(
                "极速蹬登录后仍停留在登录页，请检查凭据或稍后重试",
                {"target_url": resolved_url, "login_fallback": "redirect_failed", **login_detail},
            )

    # 登录闸门已通过。把当前 storage_state 写入运行期缓存，让滚动续期后的
    # refresh_token 在下次运行继续复用，而不改写用户账号配置。
    await common.persist_state(context, site)

    # SPA 的签到按钮要等前端拉完签到数据才渲染；轮询等待已签到状态或可点按钮。
    control, early_result = await common.wait_for_checkin_control(
        page, helpers, SPEC, opts, resolved_url=resolved_url, login_detail=login_detail
    )
    if early_result is not None:
        return early_result

    if control is None:
        # 登录态有效但 SPA 未渲染签到按钮（控制台常跳 /dashboard，/check-in 主区
        # 异步渲染滞后）。用已登录的 auth_token 直接调签到接口兜底，避免把
        # 「页面没渲染」误报成需要人工签到。
        return await common.api_fallback(
            page,
            helpers,
            SPEC,
            opts,
            origin=origin,
            resolved_url=resolved_url,
            login_attempted=login_attempted,
            do_login=do_login,
        )

    return await common.click_and_confirm(
        page,
        helpers,
        SPEC,
        opts,
        control,
        resolved_url=resolved_url,
        extra_detail=login_detail,
    )
