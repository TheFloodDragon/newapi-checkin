#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""执行当前目录下的签到任务。

规则：
- ACCOUNTS.json 里的每个启用站点（newapi / sub2api 等）都会拆成一个独立任务；
- 其他独立的 *checkin.py 脚本也会作为独立任务；
- 每个任务独立计算是否需要签到，并独立打印结果。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import accounts_store
import time_utils
from checkin_core.batch import run_serial_groups
from checkin_core.enums import OK_STATUSES, VALID_RESULT_STATUSES, status_meta
from checkin_core.events import WorkerEvent
from config import Timeouts, OutputConfig
from mask_utils import mask_secrets, sanitize_data
from providers import base as providers_base

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
# 统一账号配置：ACCOUNTS.json；sites.json 仅作旧配置补全来源。
SITES_CONFIG_PATH = SCRIPT_DIR / "sites.json"
CHECKIN_SCRIPT = SCRIPT_DIR / "checkin.py"
RESULTS_DIR = accounts_store.RESULTS_DIR
RESULT_JSON_PATH = RESULTS_DIR / "checkin_result.json"
OLD_NEWAPI_SCRIPTS = {"elysiver_checkin.py", "chybenzun_checkin.py"}
# 结果状态集合由 checkin_core.enums 统一维护。
# 子任务因超时被强制终止时使用的约定退出码（与 GNU timeout 一致）。
TIMEOUT_EXIT_CODE = 124

# 「按任务解析」的凭据类环境变量：只能来自本站点配置，绝不能从父进程继承。
#
# build_site_tasks 只在站点值非空时写入 task.env，而 run_task 过去用
# {**os.environ, **task.env} 构造子进程环境。于是父进程里任何一个
# CHECKIN_ACCESS_TOKEN / CHECKIN_COOKIE 都会漏给「没配这项凭据」的站点，
# 把 A 账号的 token 发到 B 站点去（已实测复现）。显式空值同样无法覆盖继承值。
#
# CHECKIN_PROXY 刻意不在此列：它是设计上的全局回退（CI 从 Secret 注入住宅代理），
# build_site_tasks 已在站点无 proxy 时显式读取并写入 task.env。
TASK_SCOPED_ENV = frozenset(
    {
        "CHECKIN_ACCESS_TOKEN",
        "CHECKIN_REFRESH_TOKEN",
        "CHECKIN_COOKIE",
        "CHECKIN_USER_ID",
        "CHECKIN_BROWSER_STATE",
        "CHECKIN_SCRIPT_ARGS",
        "CHECKIN_CACHE_POLICY",
    }
)
# 新三维字段：站点适配器 / 登录方式 / 签到方式
FLOW_LABELS = {
    "api": "接口签到",
    # 走了脚本钩子的接口签到（如 scripts/newapi_captcha.py 的图形验证码）。
    # 与纯 api 分开命名：这两条路径的失败原因完全不同，汇总里必须能一眼区分。
    "api+captcha": "接口签到 + 图形验证码",
    "visit": "访问保活",
    "relogin": "浏览器重登",
    "browser_script": "浏览器脚本",
    "newapi": "NewAPI",
    "sub2api": "Sub2API",
    "access_token": "Token",
    "cookie": "Cookie",
    "browser": "浏览器",
    "oauth": "OAuth",
}


@dataclass
class CheckinTask:
    name: str
    command: list[str]
    env: dict[str, str] | None = None
    site_key: str = ""
    # Hard wall-clock cap for the child process. Browser tasks can hang inside
    # launch_camoufox / page.goto with no internal timeout, which would block
    # the whole ThreadPoolExecutor shutdown until the CI job-level timeout. A
    # per-task timeout guarantees the batch always makes progress.
    timeout: float = 180.0
    worker_protocol: bool = False


@dataclass
class TaskResult:
    name: str
    returncode: int
    output: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration: float = 0.0
    diagnostics: str = ""
    worker_protocol: bool = False


@dataclass
class RetryPlanItem:
    """当前任务及其同名历史结果；同名重复项按出现顺序一一匹配。"""

    task: CheckinTask
    previous_summary: dict[str, Any] | None = None
    carried_forward: bool = False


def build_site_tasks() -> list[CheckinTask]:
    """从 ACCOUNTS.json 统一配置拆出每个站点为独立任务。"""
    if not CHECKIN_SCRIPT.exists():
        return []

    sites = accounts_store.load_unified_accounts(sites_path=SITES_CONFIG_PATH)

    tasks: list[CheckinTask] = []
    for site in sites:
        # 父进程统一解析配置 + 兼容缓存；子 worker 收到的是最终运行凭据，
        # 并通过 CHECKIN_CACHE_POLICY=ignore 禁止再次叠加缓存。
        site_config = accounts_store.runtime_site_from_mapping(site)
        base_url = site_config.base_url
        if not base_url:
            continue

        name = site_config.name
        site_profile = site_config.site_profile
        auth_method = site_config.auth_method
        checkin_action = site_config.checkin_action
        if not site_config.enabled:
            continue
        oauth_provider = site_config.oauth_provider
        oauth_account = site_config.oauth_account
        oauth_fallback_provider = site_config.oauth_fallback_provider
        oauth_fallback_account = site_config.oauth_fallback_account

        command = [
            sys.executable, str(CHECKIN_SCRIPT),
            "--base-url", base_url,
            "--name", name,
            "--site-profile", site_profile,
            "--auth-method", auth_method,
            "--checkin-action", checkin_action,
            "--worker",
        ]
        api_variant = accounts_store.normalize_api_variant(site.get("api_variant"))
        command.extend(["--api-variant", api_variant])
        verification_mode = accounts_store.normalize_verification_mode(
            site.get("verification_mode")
        )
        command.extend(["--verification-mode", verification_mode])
        # api 也可挂自定义站点脚本；此前只给
        # browser_script 透传，导致本地 GUI 测试可用、批量运行与 CI 却静默丢失脚本。
        script = str(site.get("script") or "").strip()
        if checkin_action in {"api", "browser_script"} and script:
            command.extend(["--script", script])
        if checkin_action == "browser_script":
            command.extend(["--script-timeout", str(accounts_store.parse_script_timeout(site.get("script_timeout")))])
        cookie_file = site_config.cookie_file.strip()
        cookie = site_config.cookie.strip()
        access_token = site_config.access_token.strip()
        user_id = site_config.user_id.strip()

        if cookie_file:
            command.extend(["--token-file", cookie_file])
        env_values: dict[str, str] = {}
        # script_args 可能含站点账号密码（scripts/checkin/*.py 的登录兜底会读 email/password），
        # 必须走环境变量：命令行参数会出现在同机可见的进程列表里。
        if checkin_action == "browser_script":
            script_args = accounts_store.normalize_script_args(site.get("script_args"))
            if script_args:
                env_values["CHECKIN_SCRIPT_ARGS"] = json.dumps(
                    script_args, ensure_ascii=False, separators=(",", ":")
                )
        if cookie:
            env_values["CHECKIN_COOKIE"] = cookie
        if access_token:
            env_values["CHECKIN_ACCESS_TOKEN"] = access_token
        # refresh_token 让 sub2api 站点在 access_token 过期时纯 HTTP 续期，
        # 不必为此启动浏览器；与 token 同级敏感，同样只走环境变量。
        refresh_token = site_config.refresh_token.strip()
        if refresh_token:
            env_values["CHECKIN_REFRESH_TOKEN"] = refresh_token
        if user_id:
            env_values["CHECKIN_USER_ID"] = user_id
        if auth_method == "oauth" or checkin_action == "relogin":
            command.extend(["--oauth-provider", oauth_provider, "--oauth-account", oauth_account])
        if oauth_fallback_provider:
            command.extend([
                "--oauth-fallback-provider", oauth_fallback_provider,
                "--oauth-fallback-account", oauth_fallback_account,
            ])
        # 站点未配 proxy 时，回退到全局 CHECKIN_PROXY（CI 可从 Secret 注入住宅代理，
        # 用于绕过阿里云 WAF 对数据中心/CI 出口 IP 的持续风控）。
        proxy = str(site.get("proxy") or "").strip() or os.environ.get("CHECKIN_PROXY", "").strip()
        if proxy:
            env_values["CHECKIN_PROXY"] = proxy

        # 这三项以前只在 checkin.py 读配置文件时生效；worker 模式（--base-url）不透传会
        # 静默回落默认值，导致同一份 ACCOUNTS.json 在 GUI 能用、批量/CI 却失败。
        if not site_config.verify_ssl:
            command.append("--no-verify-ssl")
        referer_path = str(site.get("referer_path") or "").strip()
        if referer_path and referer_path != "/profile":
            command.extend(["--referer-path", referer_path])
        if not site_config.auto_refresh_cookie:
            command.append("--no-auto-refresh-cookie")

        if auth_method in {"browser", "oauth"}:
            browser_profile = str(site.get("browser_profile") or "").strip()
            login_selector = str(site.get("login_selector") or "").strip()
            if browser_profile:
                command.extend(["--browser-profile", browser_profile])
            if login_selector:
                command.extend(["--login-selector", login_selector])
            # browser_state 可达数十 KB，超命令行长度上限，改用环境变量传给子进程。
            if auth_method == "oauth":
                browser_state = accounts_store.oauth_state_text(oauth_provider, oauth_account).strip()
            else:
                browser_state = site_config.browser_state.strip()
            if browser_state:
                env_values["CHECKIN_BROWSER_STATE"] = browser_state

        # 凭据已在父进程按配置 basis 解析完，子 worker 必须禁止再次读 token_cache，
        # 否则同一任务会经过两套优先级判断，显式空值也可能被旧缓存回填。
        env_values["CHECKIN_CACHE_POLICY"] = "ignore"
        env = env_values or None
        # Browser-driven flows (browser/oauth login, relogin,
        # custom browser scripts) can spend minutes on WAF solving + navigation,
        # so they get a generous cap; plain HTTP flows finish fast. browser_script
        # honors its own script_timeout plus startup/teardown headroom.
        if checkin_action == "browser_script":
            script_timeout = accounts_store.parse_script_timeout(site.get("script_timeout"), Timeouts.BROWSER_SCRIPT_DEFAULT)
            task_timeout = float(script_timeout) + Timeouts.BROWSER_STARTUP_OVERHEAD
        elif (
            auth_method in {"browser", "oauth"}
            or checkin_action == "relogin"
            or oauth_fallback_provider
            # New API 接口签到默认带内置验证路由；auto/turnstile 可能按需启动浏览器。
            # 这里只放宽任务硬上限，不会让无需验证的站点实际启动浏览器或变慢。
            or (site_profile == "newapi" and checkin_action == "api")
        ):
            task_timeout = Timeouts.BROWSER_TASK
        else:
            task_timeout = Timeouts.HTTP_TASK

        flow_label = f"{FLOW_LABELS.get(site_profile, site_profile)} / {FLOW_LABELS.get(auth_method, auth_method)} / {FLOW_LABELS.get(checkin_action, checkin_action)}"
        tasks.append(
            CheckinTask(
                f"{flow_label}: {name}",
                command,
                env=env,
                site_key=base_url,
                timeout=task_timeout,
                worker_protocol=True,
            )
        )
    return tasks


def build_script_tasks() -> list[CheckinTask]:
    tasks: list[CheckinTask] = []
    has_sites_config = SITES_CONFIG_PATH.exists()

    for script in sorted(SCRIPT_DIR.glob("*checkin.py"), key=lambda path: path.name.lower()):
        name = script.name
        if name == Path(__file__).name:
            continue
        if name == CHECKIN_SCRIPT.name:
            continue
        if has_sites_config and name in OLD_NEWAPI_SCRIPTS:
            continue
        tasks.append(CheckinTask(name, [sys.executable, str(script)]))

    return tasks


def discover_tasks() -> list[CheckinTask]:
    return build_site_tasks() + build_script_tasks()


def is_completed_summary(summary: dict[str, Any] | None) -> bool:
    """只有协议明确成功且 ``ok`` 严格为真时才可沿用。"""
    return bool(
        summary
        and summary.get("ok") is True
        and str(summary.get("status") or "") in OK_STATUSES
    )


def load_retry_history(
    path: Path | None = None,
    *,
    business_day: str | None = None,
) -> list[dict[str, Any]] | None:
    """加锁读取当天可复用的完整结果；缺失、跨日或损坏均返回 ``None``。

    历史状态不限制在当前枚举内：未知状态和协议错误仍是有效的“待重试”记录。
    但匹配所需的 task/status/ok 必须类型正确，否则整份历史按无效处理并全量执行。
    """
    result_path = path or RESULT_JSON_PATH
    try:
        with accounts_store.file_lock(result_path):
            if not result_path.exists():
                return None
            payload = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeError):
        return None

    expected_day = business_day or time_utils.business_date()
    if not isinstance(payload, dict) or payload.get("business_date") != expected_day:
        return None
    rows = payload.get("results")
    if not isinstance(rows, list):
        return None

    history: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        task_name = row.get("task")
        if not isinstance(task_name, str) or not task_name.strip():
            return None
        if not isinstance(row.get("status"), str) or not isinstance(row.get("ok"), bool):
            return None
        history.append(row)
    return history


def build_retry_plan(
    tasks: list[CheckinTask],
    history: list[dict[str, Any]] | None = None,
) -> list[RetryPlanItem]:
    """按当前任务顺序规划执行；历史同名项用队列匹配，避免重复名称覆盖。"""
    history_by_task: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    for summary in history or []:
        history_by_task[str(summary["task"])].append(summary)

    plan: list[RetryPlanItem] = []
    for task in tasks:
        queue = history_by_task.get(task.name)
        previous = queue.popleft() if queue else None
        plan.append(
            RetryPlanItem(
                task=task,
                previous_summary=previous,
                carried_forward=is_completed_summary(previous),
            )
        )
    return plan


def retry_plan_tasks(plan: list[RetryPlanItem]) -> list[CheckinTask]:
    return [item.task for item in plan if not item.carried_forward]


def build_task_env(task: CheckinTask) -> dict[str, str]:
    """构造子进程环境：先剔除所有任务级凭据变量，再写入本站点自己的值。

    必须无条件剔除（而不是「有 task.env 时才处理」）：没有任何凭据的站点同样
    不该继承父进程里别的账号的 token/cookie。
    """
    env = {key: value for key, value in os.environ.items() if key not in TASK_SCOPED_ENV}
    env.update(task.env or {})
    return env


def run_task(task: CheckinTask) -> TaskResult:
    run_env = build_task_env(task)
    started_at = datetime.now()
    start_perf = time.perf_counter()
    try:
        completed = subprocess.run(
            task.command,
            cwd=SCRIPT_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
            timeout=task.timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # A hung child (stuck launch_camoufox / page.goto) must not block the
        # thread pool forever. subprocess.run already kills the child on timeout;
        # surface partial output and a synthetic error status the classifier can
        # read (status=error keeps the batch exit code non-zero).
        ended_at = datetime.now()
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        partial_err = exc.stderr or ""
        if isinstance(partial_err, bytes):
            partial_err = partial_err.decode("utf-8", "replace")
        diagnostics = partial
        if partial_err:
            diagnostics = diagnostics + ("\n" if diagnostics else "") + partial_err
        timeout_line = json.dumps(
            {
                "site": task.name,
                "base_url": task.site_key,
                "status": "error",
                "message": f"task timed out after {task.timeout:.0f}s and was killed",
            },
            ensure_ascii=False,
        )
        return TaskResult(
            task.name,
            TIMEOUT_EXIT_CODE,  # conventional timeout exit code (128+SIGKILL)
            timeout_line,
            started_at=started_at,
            ended_at=ended_at,
            duration=time.perf_counter() - start_perf,
            diagnostics=diagnostics.rstrip(),
            worker_protocol=task.worker_protocol,
        )
    ended_at = datetime.now()
    return TaskResult(
        task.name,
        completed.returncode,
        completed.stdout.rstrip(),
        started_at=started_at,
        ended_at=ended_at,
        duration=time.perf_counter() - start_perf,
        diagnostics=completed.stderr.rstrip(),
        worker_protocol=task.worker_protocol,
    )


def extract_json_payload(output: str) -> Any | None:
    """返回 stdout 中最后一个可完整解码的 JSON 对象/数组。

    只扫描末尾 ``OutputConfig.MAX_OUTPUT_SCAN`` 字节：legacy 脚本通常把结果 JSON
    打印在最后一行（前面可能有大量诊断输出）。限制扫描长度可避免对超长输出逐字符
    raw_decode 造成的 O(n²) 级开销，同时保留「取最后一个有效 JSON」的语义。
    """
    if not output.strip():
        return None

    scan_text = output[-OutputConfig.MAX_OUTPUT_SCAN :]
    decoder = json.JSONDecoder()
    last: Any | None = None
    for index, char in enumerate(scan_text):
        if char not in "[{":
            continue
        try:
            candidate, _end = decoder.raw_decode(scan_text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, (dict, list)):
            last = candidate
    return last


def first_result_item(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list):
        if len(payload) != 1 or not isinstance(payload[0], dict):
            return {}
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return {}


def validate_result_item(item: dict[str, Any]) -> str:
    """校验 worker 结果最小 schema，返回错误原因；空串表示有效。"""
    if not item:
        return "stdout 中没有有效结果对象"
    missing = [key for key in ("site", "base_url", "status", "message") if key not in item]
    if missing:
        return f"结果缺少字段：{', '.join(missing)}"
    status = str(item.get("status") or "")
    if status not in VALID_RESULT_STATUSES:
        return f"结果 status 无效：{status!r}"
    if not isinstance(item.get("site"), str) or not isinstance(item.get("base_url"), str):
        return "结果 site/base_url 必须是字符串"
    if not isinstance(item.get("message"), str):
        return "结果 message 必须是字符串"
    return ""


def is_blank(value: Any) -> bool:
    return value is None or value == ""


def value_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:g}"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(value)
    return text.replace("\r", " ").replace("\n", " ").strip()


def format_quota(value: Any, *, already_usd: bool = False) -> str:
    """把额度数值格式化为 $x USD 字符串（唯一实现在 providers.base.format_usd）。

    - already_usd=False：值是 New API 内部 quota，需 /500000 换算（如 newapi）
    - already_usd=True ：值本身已是 USD（如 sub2api 的 reward_amount），不换算
    """
    if is_blank(value):
        return ""
    return providers_base.format_usd(value, is_usd=already_usd, fallback=value_to_text(value))


# detail 提取与 quota_is_usd 判定同样收敛到 providers.base；保留原名兼容既有调用。
find_first_value = providers_base.find_first_value
detail_is_usd = providers_base.detail_is_usd
extract_quota_awarded = providers_base.detail_quota_awarded
extract_current_quota = providers_base.detail_current_quota


def append_part(parts: list[str], label: str, value: Any, *, skip_value: Any = None) -> None:
    if is_blank(value):
        return
    if skip_value is not None and value == skip_value:
        return
    parts.append(f"{label}：{value_to_text(value)}")


def captcha_note(detail: Any) -> str:
    """图形验证码流程的摘要（走了几次、命中哪套方言、读数是否可信）。

    这些字段由 scripts/newapi_captcha.py 通过 CheckinReward.extra 回传。
    只打日志不够：日志会滚走，而「今天到底走没走验证码、试了几次」是判断
    识别器是否在退化的唯一长期记录（实测 sheapi 站点此前完全看不到这段）。
    """
    if not isinstance(detail, dict):
        return ""
    attempts = detail.get("captcha_attempts")
    dialect = detail.get("captcha_dialect")
    if attempts in (None, "") and not dialect:
        return ""
    bits: list[str] = []
    if dialect:
        bits.append(str(dialect))
    if isinstance(attempts, int) and attempts > 0:
        bits.append(f"第 {attempts} 次通过" if not detail.get("captcha_failed") else f"{attempts} 次均失败")
    if detail.get("captcha_answer_exact") is False:
        bits.append("读数不可信")
    return "，".join(bits)


def quiz_note(detail: Any) -> str:
    """每日答题的摘要（来自 detail["quiz"]，由站点脚本写入）。

    答题是签到之外的独立收益，成功与否此前只体现在 message 尾部，一旦站点
    改了文案就完全看不出答题有没有跑。这里从结构化字段单独渲染一列。
    """
    if not isinstance(detail, dict):
        return ""
    quiz = detail.get("quiz")
    if not isinstance(quiz, dict):
        return ""
    text = str(quiz.get("message") or quiz.get("outcome") or "").strip()
    unknown = quiz.get("unknown")
    if isinstance(unknown, int) and unknown > 0:
        text = f"{text}（{unknown} 题未收录，已猜）" if text else f"{unknown} 题未收录，已猜"
    return text


def build_detail_note(status: str, message: str, detail: Any) -> str:
    parts: list[str] = []

    already_usd = detail_is_usd(detail)
    quota_awarded = extract_quota_awarded(detail)
    current_quota = extract_current_quota(detail)
    source = find_first_value(detail, ["checkin_source", "source", "mode"])
    consecutive_days = find_first_value(detail, ["consecutive_days", "continuous_days", "consecutive_checkins"])
    total_checkins = find_first_value(detail, ["total_checkins", "checkin_count", "total_days", "checked_days"])
    checked_in_today = find_first_value(detail, ["checked_in_today", "today_checked", "is_checked_in"])

    if status == "already_done":
        # relogin / visit 类站点靠 OAuth 登录发放额度，无独立签到状态接口，
        # 「额度无变化」不代表今日一定已领取（可能到账延迟）。这类场景保留 action
        # 给出的更准确 message，不要覆盖成确定性的「今日已领取」。
        if str(source) in {"relogin", "visit"} and message:
            parts.append(message)
        else:
            parts.append("今日已领取，无需重复签到")
    # 0 视为「站点没回具体金额」而非「获得 $0」：否则汇总行会出现
    # 「获得额度：$0.0000」，看着像签到失败。非零才展示金额。
    if status == "success" and providers_base.has_awarded_amount(quota_awarded, is_usd=already_usd):
        parts.append(f"获得额度：{format_quota(quota_awarded, already_usd=already_usd)}")
    elif "获得额度" in message:
        parts.append(message)

    append_part(
        parts,
        "当前额度",
        format_quota(current_quota, already_usd=already_usd),
        skip_value=format_quota(quota_awarded, already_usd=already_usd),
    )
    append_part(parts, "连续天数", consecutive_days)
    append_part(parts, "累计签到", total_checkins)
    if checked_in_today is True and status != "already_done":
        parts.append("今日状态：已完成")
    if source:
        source_text = FLOW_LABELS.get(str(source), str(source))
        append_part(parts, "流程", source_text)
    append_part(parts, "验证码", captcha_note(detail))
    append_part(parts, "答题", quiz_note(detail))

    if not parts and message:
        parts.append(message)
    elif status not in OK_STATUSES and message and message not in parts:
        parts.insert(0, message)

    seen: set[str] = set()
    unique_parts: list[str] = []
    for part in parts:
        part = value_to_text(part)
        if part and part not in seen:
            seen.add(part)
            unique_parts.append(part)
    return "；".join(unique_parts)


def compact_status(status: str, returncode: int) -> str:
    if status == "unknown":
        return "协议错误"
    return status_meta(status).label


def status_icon(status: str, returncode: int) -> str:
    if status == "unknown":
        return "❌"
    return status_meta(status).icon


def task_result_to_summary(result: TaskResult) -> dict[str, Any]:
    payload: Any | None
    if result.worker_protocol:
        try:
            payload = json.loads(result.output.strip())
        except (json.JSONDecodeError, TypeError):
            payload = None
    else:
        payload = extract_json_payload(result.output)
    item = first_result_item(payload)
    protocol_error = validate_result_item(item)
    diagnostic_source = result.diagnostics.strip() or result.output.strip()
    output_tail = diagnostic_source.splitlines()[-1][:200] if diagnostic_source else "无输出"
    if protocol_error:
        item = {
            "site": result.name,
            "base_url": "",
            "status": "error",
            "message": f"子任务结果协议错误：{protocol_error}；诊断：{output_tail}",
            "detail": {"protocol_error": protocol_error},
        }
    elif result.returncode != 0 and str(item.get("status")) in OK_STATUSES:
        item = {
            **item,
            "status": "error",
            "message": f"子任务退出码为 {result.returncode}，与成功结果不一致",
        }

    item = sanitize_data(item)
    status = str(item.get("status") or "error")
    message = str(item.get("message") or output_tail)
    site = str(item.get("site") or result.name)
    base_url = str(item.get("base_url") or "")
    detail = item.get("detail")
    already_usd = detail_is_usd(detail)
    quota_awarded = extract_quota_awarded(detail)
    current_quota = extract_current_quota(detail)
    label = compact_status(status, result.returncode)
    icon = status_icon(status, result.returncode)
    note = build_detail_note(status, message, detail)
    ok = status in OK_STATUSES and result.returncode == 0

    return {
        "site": site,
        "task": result.name,
        "base_url": base_url,
        "status": status,
        "label": label,
        "icon": icon,
        "ok": ok,
        "returncode": result.returncode,
        "message": value_to_text(message),
        "note": note,
        "quota_awarded": format_quota(quota_awarded, already_usd=already_usd),
        "current_quota": format_quota(current_quota, already_usd=already_usd),
        "duration_seconds": round(result.duration, 3),
        "started_at": result.started_at.isoformat(timespec="seconds") if result.started_at else "",
        "ended_at": result.ended_at.isoformat(timespec="seconds") if result.ended_at else "",
    }


def merge_retry_results(
    plan: list[RetryPlanItem],
    executed_results: list[TaskResult],
) -> list[dict[str, Any]]:
    """把本轮执行结果放回计划位置，与沿用项合并成当前配置的完整结果。"""
    result_iter = iter(executed_results)
    merged: list[dict[str, Any]] = []

    for item in plan:
        if item.carried_forward:
            if item.previous_summary is None:  # 防御不可达的不一致计划
                raise ValueError(f"任务 {item.task.name!r} 缺少可沿用结果")
            summary = dict(item.previous_summary)
            summary["task"] = item.task.name
            summary["carried_forward"] = True
            summary["executed_this_run"] = False
            summary["retried"] = False
            # 重试成功是结果的历史属性：第三次运行沿用时仍保留行级标记。
            summary["retry_succeeded"] = summary.get("retry_succeeded") is True
            merged.append(summary)
            continue

        try:
            result = next(result_iter)
        except StopIteration as exc:
            raise ValueError("本轮执行结果少于重试计划") from exc

        summary = task_result_to_summary(result)
        summary["task"] = item.task.name
        summary["carried_forward"] = False
        summary["executed_this_run"] = True
        previous = item.previous_summary
        retried = previous is not None
        summary["retried"] = retried
        if retried:
            summary["previous_status"] = str(previous.get("status") or "")
        else:
            summary.pop("previous_status", None)
        summary["retry_succeeded"] = bool(
            retried and not is_completed_summary(previous) and is_completed_summary(summary)
        )
        merged.append(summary)

    try:
        next(result_iter)
    except StopIteration:
        return merged
    raise ValueError("本轮执行结果多于重试计划")


# 各阶段调用日志的前缀（子进程写 stderr，形如「[api_first:站点名] ...」）。
# 这类行是排查「卡在哪一级凭据」的主要依据，因此始终打印；真正可能回显
# Cookie/token 的完整原始输出仍只在 --verbose 或任务失败时才输出。
#
# 白名单漏项会让整条链路「静默」：实测 sheapi.top 的图形验证码签到全程一行日志
# 都看不到，根因就是站点脚本的日志走 providers/actions/api.py 的 [api:站点名]，
# 而这里当时只放行了 api_first:（前缀不同，startswith 匹配不上）。凡是新增日志
# 前缀都必须同步登记到这里，否则等于没写。
STAGE_LOG_PREFIXES = (
    "api:",           # api action + 站点脚本（含图形验证码流程）
    "api_first:",     # browser_script 的纯 HTTP 前置链
    "http:",          # 站点原始返回值（providers.base.log_http_exchange）
    "sub2api:",
    "newapi:",
    "relogin:",
    "browser_script:",
)


def stage_logs(result: TaskResult) -> list[str]:
    """从子进程 stderr 里挑出各阶段调用日志（[api_first:站点] ... 这类行）。

    这些行是「签到到底走了哪条路、卡在哪一级」的唯一线索，此前只在 --verbose
    或任务失败时随「原始输出」整块打印，批量签到成功时完全看不到，用户无法确认
    是走了纯 API 还是退化到开浏览器。按前缀白名单挑选，避免把可能回显凭据的
    完整输出无条件打出来。
    """
    text = result.diagnostics or ""
    if not text:
        return []
    picked: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        event = WorkerEvent.from_line(stripped)
        if event is not None:
            marker = f"{event.stage}:{event.site}" if event.site else event.stage
            picked.append(f"[{marker}] {event.message}")
            continue
        # 兼容尚未迁移的旧前缀日志。
        if not stripped.startswith("["):
            continue
        marker = stripped[1:].split("]", 1)[0]
        if any(marker.startswith(prefix) for prefix in STAGE_LOG_PREFIXES):
            picked.append(stripped)
    return picked


def print_result(result: TaskResult, verbose: bool = False) -> None:
    summary = task_result_to_summary(result)
    headline = f"[{summary['site']}] {summary['icon']} {summary['label']}"
    if summary["note"]:
        headline += f" - {mask_secrets(summary['note'])}"
    print(headline, flush=True)
    if summary["base_url"]:
        print(f"  站点地址：{summary['base_url']}", flush=True)
    if summary["quota_awarded"]:
        print(f"  获得额度：{summary['quota_awarded']}", flush=True)
    if summary["current_quota"]:
        print(f"  当前额度：{summary['current_quota']}", flush=True)
    if summary["message"] and summary["message"] not in summary["note"]:
        print(f"  消息：{mask_secrets(summary['message'])}", flush=True)
    if summary.get("duration_seconds"):
        print(f"  耗时：{summary['duration_seconds']:.1f}s", flush=True)
    # 默认不打印完整原始输出（可能含 Cookie/token 回显）；仅在 verbose 或任务失败时打印，且经脱敏。
    raw_output = "\n".join(part for part in (result.output, result.diagnostics) if part)
    show_raw = bool(raw_output) and (verbose or not summary["ok"])
    # 阶段日志始终打印：批量签到时这是判断「走了纯 API 还是退化到开浏览器、卡在
    # 哪一级凭据」的唯一线索，此前只随「原始输出」整块出现，成功任务完全看不到。
    # 已经要打印原始输出时就不重复（那里本就包含这些行）。
    stages = stage_logs(result)
    if stages and not show_raw:
        print("  调用日志：", flush=True)
        for line in stages:
            print(f"    {mask_secrets(line)}", flush=True)
    if show_raw:
        print("  原始输出：", flush=True)
        print(textwrap.indent(mask_secrets(raw_output), "    "), flush=True)
    print(flush=True)


def run_tasks(tasks: list[CheckinTask], workers: int = 0, verbose: bool = False) -> list[TaskResult]:
    """复用统一分组器：同站任务串行、独立脚本和不同站点并发。"""
    return run_serial_groups(
        tasks,
        key=lambda task: task.site_key,
        execute=run_task,
        on_error=lambda task, exc: TaskResult(
            task.name,
            1,
            "",
            diagnostics=f"任务异常：{exc}",
            worker_protocol=task.worker_protocol,
        ),
        workers=workers,
        on_result=lambda result: print_result(result, verbose=verbose),
    )


def result_run_counts(summaries: list[dict[str, Any]]) -> tuple[int, int, int]:
    executed_count = sum(1 for item in summaries if item.get("executed_this_run") is True)
    carried_count = sum(1 for item in summaries if item.get("carried_forward") is True)
    retry_succeeded_count = sum(
        1
        for item in summaries
        if item.get("executed_this_run") is True and item.get("retry_succeeded") is True
    )
    return executed_count, carried_count, retry_succeeded_count


def build_result_payload(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    failed_count = sum(1 for item in summaries if item.get("ok") is not True)
    success_count = sum(1 for item in summaries if item.get("status") == "success")
    already_done_count = sum(1 for item in summaries if item.get("status") == "already_done")
    executed_count, carried_count, retry_succeeded_count = result_run_counts(summaries)
    return sanitize_data(
        {
            "generated_at": time_utils.utc_iso(),
            "business_date": time_utils.business_date(),
            "total": len(summaries),
            "success_count": success_count,
            "already_done_count": already_done_count,
            "failed_count": failed_count,
            "executed_this_run_count": executed_count,
            "carried_forward_count": carried_count,
            "retry_succeeded_count": retry_succeeded_count,
            "results": summaries,
        }
    )


def write_result_file(summaries: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_result_payload(summaries)
    with accounts_store.file_lock(RESULT_JSON_PATH):
        accounts_store.atomic_write_text(
            RESULT_JSON_PATH,
            json.dumps(payload, ensure_ascii=False, indent=2),
        )


def summary_run_label(summary: dict[str, Any]) -> str:
    markers: list[str] = []
    if summary.get("retry_succeeded") is True:
        markers.append("🔁 重试成功")
    elif summary.get("retried") is True:
        markers.append("本轮重试")
    if summary.get("carried_forward") is True:
        markers.append("本轮跳过")
    markers.append(str(summary.get("label") or summary.get("status") or "未知"))
    return " / ".join(markers)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行所有签到任务")
    parser.add_argument("--workers", type=int, default=0, help="同时执行的最大任务数，默认最多 8 个")
    parser.add_argument("--verbose", action="store_true", help="打印每个任务的完整原始输出（已脱敏）；默认仅失败任务打印")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="沿用当天上次已完成结果，仅执行失败、新增或协议无效的任务",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    print(f"自动签到开始：{datetime.now():%Y-%m-%d %H:%M:%S}")

    try:
        tasks = discover_tasks()
    except Exception as exc:
        print(f"读取任务失败：{exc}")
        return 2

    if not tasks:
        print("未找到需要执行的签到任务。")
        write_result_file([])
        return 2

    history: list[dict[str, Any]] | None = None
    if getattr(args, "retry_failed", False):
        history = load_retry_history()
        if history is None:
            print("当天没有可复用的有效结果，本轮执行全部任务。")
        else:
            print(f"已读取当天上次结果：{len(history)} 项。")

    plan = build_retry_plan(tasks, history)
    tasks_to_execute = retry_plan_tasks(plan)
    carried_count = len(plan) - len(tasks_to_execute)
    if carried_count:
        print(f"沿用上次已完成结果：{carried_count} 项；本轮待执行：{len(tasks_to_execute)} 项。")
    if tasks_to_execute:
        results = run_tasks(tasks_to_execute, args.workers, verbose=args.verbose)
    else:
        print("当前任务均已完成，本轮无需启动子任务。")
        results = []

    summaries = merge_retry_results(plan, results)
    write_result_file(summaries)

    display_rows = [
        {
            "site": value_to_text(item.get("site") or item.get("task") or "Unknown"),
            "icon": value_to_text(item.get("icon")),
            "status": summary_run_label(item),
            "detail": value_to_text(item.get("note") or item.get("message")),
        }
        for item in summaries
    ]
    max_name = max((len(item["site"]) for item in display_rows), default=4)
    max_status = max((len(item["status"]) for item in display_rows), default=4)
    max_name = max(max_name, 4)
    max_status = max(max_status, 4)

    print("\n总结：")
    print(f"  {'站点':<{max_name}} | 图标 | {'状态':<{max_status}} | 备注")
    print(f"  {'-' * max_name}-+-{'-' * 2}-+-{'-' * max_status}-+-{'-' * 24}")
    for item in display_rows:
        detail = mask_secrets(item["detail"])
        print(f"  {item['site']:<{max_name}} | {item['icon']} | {item['status']:<{max_status}} | {detail}")

    executed_count, carried_count, retry_succeeded_count = result_run_counts(summaries)
    print(
        f"\n本轮实际执行：{executed_count}；"
        f"沿用上次完成：{carried_count}；"
        f"本轮重试成功：{retry_succeeded_count}"
    )
    failed_count = sum(1 for item in summaries if item.get("ok") is not True)
    print(f"结果文件：{RESULT_JSON_PATH}")
    return 0 if failed_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
