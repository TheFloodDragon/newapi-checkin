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
import concurrent.futures
import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import accounts_store
from mask_utils import mask_secrets

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

SCRIPT_DIR = Path(__file__).resolve().parent
# 统一账号配置：ACCOUNTS.json；sites.json 仅作旧配置补全来源。
SITES_CONFIG_PATH = SCRIPT_DIR / "sites.json"
CHECKIN_SCRIPT = SCRIPT_DIR / "checkin.py"
RESULTS_DIR = SCRIPT_DIR / "results"
RESULT_JSON_PATH = RESULTS_DIR / "checkin_result.json"
OLD_NEWAPI_SCRIPTS = {"elysiver_checkin.py", "chybenzun_checkin.py"}
OK_STATUSES = {"success", "already_done"}
QUOTA_UNIT = 500_000  # New API 内部 quota 与 USD 的换算系数：quota / 500000 = $
# 新三维字段：站点适配器 / 登录方式 / 签到方式
DEFAULT_PROFILE = "newapi"
KNOWN_PROFILES = {"newapi", "sub2api"}
KNOWN_AUTH_METHODS = {"access_token", "cookie", "browser", "oauth"}
KNOWN_ACTIONS = {"api", "relogin", "visit", "browser_script"}
FLOW_LABELS = {
    "api": "接口签到",
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


@dataclass
class TaskResult:
    name: str
    returncode: int
    output: str
    started_at: datetime | None = None
    ended_at: datetime | None = None
    duration: float = 0.0


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        value = "https://" + value
    return value


def load_config_sites(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.exists():
        return []
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    raw_sites = raw.get("sites", []) if isinstance(raw, dict) else raw
    if not isinstance(raw_sites, list):
        raise ValueError("sites.json 必须是数组，或包含 sites 数组的对象。")
    return [site for site in raw_sites if isinstance(site, dict)]


def normalize_profile(value: Any) -> str:
    text = str(value or DEFAULT_PROFILE).strip().lower()
    return text if text in KNOWN_PROFILES else DEFAULT_PROFILE


def normalize_auth_method(value: Any) -> str:
    text = str(value or "cookie").strip().lower()
    return text if text in KNOWN_AUTH_METHODS else "cookie"


def normalize_action(value: Any) -> str:
    text = str(value or "api").strip().lower()
    return text if text in KNOWN_ACTIONS else "api"


def build_site_tasks() -> list[CheckinTask]:
    """从 ACCOUNTS.json 统一配置拆出每个站点为独立任务。"""
    if not CHECKIN_SCRIPT.exists():
        return []

    try:
        sites = accounts_store.load_unified_accounts(sites_path=SITES_CONFIG_PATH)
    except Exception as exc:
        print(f"[WARN] 读取统一配置失败：{exc}")
        return []

    tasks: list[CheckinTask] = []
    for site in sites:
        base_url = normalize_base_url(str(site.get("base_url") or site.get("url") or ""))
        if not base_url:
            continue

        name = str(site.get("name") or base_url)
        site_profile = normalize_profile(site.get("site_profile") or site.get("type") or site.get("provider"))
        auth_method = normalize_auth_method(site.get("auth_method"))
        checkin_action = normalize_action(site.get("checkin_action"))
        enabled = accounts_store.parse_enabled(site.get("enabled"), True)
        if not enabled:
            continue
        oauth_provider = accounts_store.normalize_oauth_provider(site.get("oauth_provider")) or "linuxdo"
        oauth_account = accounts_store.normalize_oauth_account(site.get("oauth_account") or site.get("oauth_account_id"))

        command = [
            sys.executable, str(CHECKIN_SCRIPT),
            "--base-url", base_url,
            "--name", name,
            "--site-profile", site_profile,
            "--auth-method", auth_method,
            "--checkin-action", checkin_action,
        ]
        api_variant = str(site.get("api_variant") or "auto").strip().lower()
        if api_variant:
            command.extend(["--api-variant", api_variant])
        if checkin_action == "browser_script":
            script = str(site.get("script") or "").strip()
            if script:
                command.extend(["--script", script])
            script_args = accounts_store.normalize_script_args(site.get("script_args"))
            if script_args:
                command.extend(["--script-args", json.dumps(script_args, ensure_ascii=False, separators=(",", ":"))])
            command.extend(["--script-timeout", str(accounts_store.parse_script_timeout(site.get("script_timeout"), 120))])
        cookie_file = str(site.get("cookie_file") or site.get("token_file") or "").strip()
        cookie = str(site.get("cookie") or "").strip()
        access_token = str(site.get("access_token") or site.get("authorization") or "").strip()
        user_id = str(site.get("user_id") or site.get("new_api_user") or "").strip()

        if cookie_file:
            command.extend(["--token-file", cookie_file])
        if cookie:
            command.extend(["--cookie", cookie])
        if access_token:
            command.extend(["--access-token", access_token])
        if user_id:
            command.extend(["--user-id", user_id])
        if auth_method == "oauth" or checkin_action == "relogin":
            command.extend(["--oauth-provider", oauth_provider, "--oauth-account", oauth_account])
        # 站点未配 proxy 时，回退到全局 CHECKIN_PROXY（CI 可从 Secret 注入住宅代理，
        # 用于绕过阿里云 WAF 对数据中心/CI 出口 IP 的持续风控）。
        proxy = str(site.get("proxy") or "").strip() or os.environ.get("CHECKIN_PROXY", "").strip()
        if proxy:
            command.extend(["--proxy", proxy])

        env: dict[str, str] | None = None
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
                browser_state = str(site.get("browser_state") or "").strip()
            if browser_state:
                env = {"CHECKIN_BROWSER_STATE": browser_state}

        # Per-task timeout. Browser-driven flows (browser/oauth login, relogin,
        # custom browser scripts) can spend minutes on WAF solving + navigation,
        # so they get a generous cap; plain HTTP flows finish fast. browser_script
        # honors its own script_timeout plus startup/teardown headroom.
        if checkin_action == "browser_script":
            script_timeout = accounts_store.parse_script_timeout(site.get("script_timeout"), 120)
            task_timeout = float(script_timeout) + 120.0
        elif auth_method in {"browser", "oauth"} or checkin_action == "relogin":
            task_timeout = 420.0
        else:
            task_timeout = 120.0

        flow_label = f"{FLOW_LABELS.get(site_profile, site_profile)} / {FLOW_LABELS.get(auth_method, auth_method)} / {FLOW_LABELS.get(checkin_action, checkin_action)}"
        tasks.append(CheckinTask(f"{flow_label}: {name}", command, env=env, site_key=base_url, timeout=task_timeout))
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


def run_task(task: CheckinTask) -> TaskResult:
    run_env = None
    if task.env:
        run_env = {**os.environ, **task.env}
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
        output = partial
        if partial_err:
            output = output + ("\n" if output else "") + partial_err
        timeout_line = json.dumps(
            {
                "site": task.name,
                "base_url": task.site_key,
                "status": "error",
                "message": f"task timed out after {task.timeout:.0f}s and was killed",
            },
            ensure_ascii=False,
        )
        output = (output + ("\n" if output else "")) + timeout_line
        return TaskResult(
            task.name,
            124,  # conventional timeout exit code
            output.rstrip(),
            started_at=started_at,
            ended_at=ended_at,
            duration=time.perf_counter() - start_perf,
        )
    ended_at = datetime.now()
    output = completed.stdout
    if completed.stderr:
        output = output + ("\n" if output else "") + completed.stderr
    return TaskResult(task.name, completed.returncode, output.rstrip(), started_at=started_at, ended_at=ended_at, duration=time.perf_counter() - start_perf)


def extract_json_payload(output: str) -> Any | None:
    """从子任务输出中提取 JSON；允许 JSON 前面带有 [WARN] 等日志。"""
    if not output.strip():
        return None

    decoder = json.JSONDecoder()
    for index, char in enumerate(output):
        if char not in "[{":
            continue
        try:
            candidate, _end = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, (dict, list)):
            return candidate
    return None


def first_result_item(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    return {}


def is_blank(value: Any) -> bool:
    return value is None or value == ""


def find_first_value(data: Any, keys: list[str]) -> Any:
    """在嵌套 dict/list 中按键名查找第一个非空值。"""
    wanted = {key.lower() for key in keys}
    queue: list[Any] = [data]
    seen: set[int] = set()

    while queue:
        item = queue.pop(0)
        if item is None:
            continue
        marker = id(item)
        if marker in seen:
            continue
        seen.add(marker)

        if isinstance(item, dict):
            for key, value in item.items():
                if str(key).lower() in wanted and not is_blank(value):
                    return value
            queue.extend(item.values())
        elif isinstance(item, list):
            queue.extend(item)
    return None


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


def quota_to_usd(value: Any, *, already_usd: bool = False) -> str:
    """把额度数值格式化为 $x USD 字符串；非数字原样返回。

    - already_usd=False：值是 New API 内部 quota，需 /500000 换算（如 newapi）
    - already_usd=True ：值本身已是 USD（如 sub2api 的 reward_amount），不换算
    """
    if is_blank(value):
        return ""
    try:
        usd = float(value) if already_usd else float(value) / QUOTA_UNIT
        return f"${usd:.4g}"
    except (TypeError, ValueError):
        return value_to_text(value)


def detail_is_usd(detail: Any) -> bool:
    """provider 在 detail 里标记 quota_is_usd=true 时，额度无需 /500000 换算。"""
    return bool(find_first_value(detail, ["quota_is_usd"]))


def format_quota(value: Any, *, already_usd: bool = False) -> str:
    return quota_to_usd(value, already_usd=already_usd)


def extract_quota_awarded(detail: Any) -> Any:
    return find_first_value(
        detail,
        [
            "quota_awarded",
            "awarded_quota",
            "award_quota",
            "reward_quota",
            "checkin_quota",
            "quota_reward",
        ],
    )


def extract_current_quota(detail: Any) -> Any:
    return find_first_value(
        detail,
        [
            "current_quota",   # checkin.py 注入的标准字段
            "remaining_quota",
            "available_quota",
            "quota_remaining",
            "user_quota",
            "quota",
            "balance",
        ],
    )


def append_part(parts: list[str], label: str, value: Any, *, skip_value: Any = None) -> None:
    if is_blank(value):
        return
    if skip_value is not None and value == skip_value:
        return
    parts.append(f"{label}：{value_to_text(value)}")


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
        parts.append("今日已领取，无需重复签到")
    if status == "success" and not is_blank(quota_awarded):
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
    if status == "success":
        return "成功"
    if status == "already_done":
        return "已领取"
    if status == "need_login":
        return "登录失效"
    if status == "need_verification":
        return "需验证"
    if status == "need_config":
        return "需配置"
    if status == "unknown" and returncode == 0:
        return "成功"
    if status == "unknown":
        return "失败"
    if status == "error":
        return "失败"
    return status if status != "unknown" else "失败"


def status_icon(status: str, returncode: int) -> str:
    if status == "success" or (status == "unknown" and returncode == 0):
        return "✅"
    if status == "already_done":
        return "🎁"
    if status == "need_login":
        return "🔐"
    if status == "need_verification":
        return "⚠️"
    if status == "need_config":
        return "🛠️"
    return "❌"


def task_result_to_summary(result: TaskResult) -> dict[str, Any]:
    payload = extract_json_payload(result.output)
    item = first_result_item(payload)
    output_tail = result.output.strip().splitlines()[-1][:200] if result.output.strip() else "无输出"

    status = str(item.get("status") or "unknown")
    message = str(item.get("message") or ("执行成功" if result.returncode == 0 else output_tail))
    site = str(item.get("site") or result.name)
    base_url = str(item.get("base_url") or "")
    detail = item.get("detail")
    already_usd = detail_is_usd(detail)
    quota_awarded = extract_quota_awarded(detail)
    current_quota = extract_current_quota(detail)
    label = compact_status(status, result.returncode)
    icon = status_icon(status, result.returncode)
    note = build_detail_note(status, message, detail)
    ok = status in OK_STATUSES or (status == "unknown" and result.returncode == 0)

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
    if result.output and (verbose or not summary["ok"]):
        print("  原始输出：", flush=True)
        print(textwrap.indent(mask_secrets(result.output), "    "), flush=True)
    print(flush=True)


def run_tasks(tasks: list[CheckinTask], workers: int = 0, verbose: bool = False) -> list[TaskResult]:
    if not tasks:
        return []

    max_workers = workers if workers > 0 else min(8, len(tasks))
    site_locks = {task.site_key: threading.Lock() for task in tasks if task.site_key}

    def run_task_guarded(task: CheckinTask) -> TaskResult:
        if not task.site_key:
            return run_task(task)
        with site_locks[task.site_key]:
            return run_task(task)

    results: list[TaskResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(run_task_guarded, task): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            task = future_map[future]
            try:
                result = future.result()
            except Exception as exc:
                result = TaskResult(task.name, 1, f"任务异常：{exc}")
            results.append(result)
            print_result(result, verbose=verbose)
    return results


def write_result_file(summaries: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    failed_count = sum(1 for item in summaries if not item["ok"])
    success_count = sum(1 for item in summaries if item["status"] == "success")
    already_done_count = sum(1 for item in summaries if item["status"] == "already_done")
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(summaries),
        "success_count": success_count,
        "already_done_count": already_done_count,
        "failed_count": failed_count,
        "results": summaries,
    }
    RESULT_JSON_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="执行所有签到任务")
    parser.add_argument("--workers", type=int, default=0, help="同时执行的最大任务数，默认最多 8 个")
    parser.add_argument("--verbose", action="store_true", help="打印每个任务的完整原始输出（已脱敏）；默认仅失败任务打印")
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

    results = run_tasks(tasks, args.workers, verbose=args.verbose)
    summaries = [task_result_to_summary(result) for result in results]
    write_result_file(summaries)

    max_name = max((len(item["site"]) for item in summaries), default=0)
    max_status = max((len(item["label"]) for item in summaries), default=0)
    max_name = max(max_name, 4)
    max_status = max(max_status, 4)

    print("\n总结：")
    print(f"  {'站点':<{max_name}} | 图标 | {'状态':<{max_status}} | 备注")
    print(f"  {'-' * max_name}-+-{'-' * 2}-+-{'-' * max_status}-+-{'-' * 24}")
    for item in summaries:
        detail = item["note"] or item["message"]
        print(f"  {item['site']:<{max_name}} | {item['icon']} | {item['label']:<{max_status}} | {detail}")

    failed_count = sum(1 for item in summaries if not item["ok"])
    print(f"\n结果文件：{RESULT_JSON_PATH}")
    return 0 if failed_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
