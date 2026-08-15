from __future__ import annotations

import ast
import asyncio
import base64
import gzip
import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from browser import session, state
from browser.oauth_flow import is_oauth_callback_url
from browser import runtime_loop
from browser.runtime_loop import BrowserResources

REPO_ROOT = Path(__file__).resolve().parents[1]


def _valid_state() -> dict:
    return {
        "cookies": [
            {
                "name": "session",
                "value": "secret",
                "domain": ".example.invalid",
                "path": "/",
            }
        ],
        "origins": [
            {
                "origin": "https://example.invalid",
                "localStorage": [{"name": "token", "value": "value"}],
            }
        ],
    }


def test_firefox_driver_page_error_patch_is_correct_and_idempotent(tmp_path) -> None:
    """驱动读 pageError.location.url 会让 Node 进程崩溃，必须改写为安全形式。

    崩溃发生在驱动进程（Python 端只看到 "Connection closed while reading from
    the driver"），且由 Firefox 的 _onUncaughtError 主动触发，与是否注册
    pageerror 监听无关，因此只能补驱动。
    """
    from browser.driver_patch import patch_firefox_page_error

    bundle = tmp_path / "coreBundle.js"
    bundle.write_text(
        "function a(pageError, page){ return {\n"
        "  url: pageError.location.url,\n"
        "  line: pageError.location.lineNumber,\n"
        "  column: pageError.location.columnNumber\n"
        "}; }\n",
        encoding="utf-8",
    )

    assert patch_firefox_page_error(bundle) == "patched"
    patched = bundle.read_text(encoding="utf-8")
    assert "pageError.location.url" not in patched
    assert "(pageError.location||{}).url" in patched

    # 补丁后的表达式必须是合法 JS，且对缺失 location 返回安全默认值。
    node = shutil.which("node")
    if node:
        probe = tmp_path / "probe.js"
        probe.write_text(
            patched + "\nconst r=a({}, null);\n"
            "if(r.url!==''||r.line!==0||r.column!==0) throw new Error('bad defaults');\n"
            "const r2=a({location:{url:'u',lineNumber:7,columnNumber:9}}, null);\n"
            "if(r2.url!=='u'||r2.line!==7||r2.column!==9) throw new Error('bad passthrough');\n"
            "console.log('ok');\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [node, str(probe)], capture_output=True, text=True, timeout=60
        )
        assert completed.returncode == 0, completed.stderr
        assert "ok" in completed.stdout

    # 幂等：重复调用不得再改动文件。
    assert patch_firefox_page_error(bundle) == "already"
    assert bundle.read_text(encoding="utf-8") == patched

    # 找不到文件时安静返回，不能影响浏览器启动。
    assert patch_firefox_page_error(tmp_path / "missing.js") == "unavailable"


def test_resource_close_timeouts_do_not_block_business_result(monkeypatch) -> None:
    class HangingPage:
        async def close(self) -> None:
            await asyncio.sleep(60)

    class HangingBrowser:
        async def close(self) -> None:
            await asyncio.sleep(60)

    monkeypatch.setattr(runtime_loop, "PAGE_CLOSE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(runtime_loop, "BROWSER_CLOSE_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> None:
        resources = BrowserResources(browser=HangingBrowser(), page=HangingPage())
        await resources.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=1))


def test_oauth_capture_auto_finishes_only_with_authenticated_cookie(monkeypatch) -> None:
    class FakePage:
        url = "https://linux.do"

        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def title(self) -> str:
            return "Linux.do"

        async def close(self) -> None:
            return None

    class FakeContext:
        def __init__(self) -> None:
            self.page = FakePage()

        async def new_page(self):
            return self.page

        async def cookies(self):
            return [{"name": "_t", "value": "token", "domain": ".linux.do", "path": "/"}]

        async def storage_state(self):
            return {"cookies": await self.cookies(), "origins": []}

    class FakeBrowser:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    browser = FakeBrowser()
    context = FakeContext()

    async def launch(**_kwargs):
        return browser, context

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(session.bypass, "launch_camoufox", launch)

    result = asyncio.run(session.capture_oauth_state("linuxdo", wait_for_close=wait_forever))

    assert result["ok"] is True
    assert state.decode_state(result["state"])["cookies"][0]["name"] == "_t"
    assert browser.closed is True


def test_oauth_capture_rejects_anonymous_provider_cookie(monkeypatch) -> None:
    class FakePage:
        url = "https://linux.do"

        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def close(self) -> None:
            return None

    class FakeContext:
        async def new_page(self):
            return FakePage()

        async def cookies(self):
            return [{"name": "_forum_session", "value": "anonymous", "domain": ".linux.do"}]

        async def storage_state(self):
            raise AssertionError("匿名状态不应被导出")

    class FakeBrowser:
        async def close(self) -> None:
            return None

    async def launch(**_kwargs):
        return FakeBrowser(), FakeContext()

    async def finish_now() -> None:
        return None

    original_sleep = asyncio.sleep

    async def fast_sleep(_delay: float) -> None:
        await original_sleep(0)

    monkeypatch.setattr(session.bypass, "launch_camoufox", launch)
    monkeypatch.setattr(session.asyncio, "sleep", fast_sleep)

    result = asyncio.run(session.capture_oauth_state("linuxdo", wait_for_close=finish_now))

    assert result["ok"] is False
    assert result["state"] == ""
    assert "有效认证 Cookie" in result["message"]


def test_state_roundtrip_and_schema_validation() -> None:
    encoded = state.encode_state(_valid_state())
    assert state.decode_state(encoded) == _valid_state()

    with pytest.raises(state.BrowserStateError):
        state.encode_state({"cookies": [{"name": 1, "value": "x"}]})
    with pytest.raises(state.BrowserStateError):
        state.decode_state("not base64!")
    with pytest.raises(state.BrowserStateError):
        state.decode_state(base64.b64encode(b"not-gzip").decode("ascii"))

    oversized = _valid_state()
    oversized["origins"][0]["localStorage"][0]["value"] = "x" * (4 * 1024 * 1024)
    packed = gzip.compress(json.dumps(oversized).encode("utf-8"), compresslevel=9)
    with pytest.raises(state.BrowserStateError, match="解压后数据过大"):
        state.decode_state(base64.b64encode(packed).decode("ascii"))


def test_restore_storage_state_isolates_local_storage_by_origin() -> None:
    class FakeContext:
        def __init__(self) -> None:
            self.cookie_calls: list[list[dict]] = []
            self.init_scripts: list[str] = []

        async def add_cookies(self, cookies: list[dict]) -> None:
            self.cookie_calls.append(cookies)

        async def add_init_script(self, script: str) -> None:
            self.init_scripts.append(script)

    cookies = _valid_state()["cookies"]
    storage_state = {
        "cookies": cookies,
        "origins": [
            {
                "origin": "https://one.invalid",
                "localStorage": [
                    {"name": "shared", "value": "one"},
                    {"name": "only_one", "value": "1"},
                ],
            },
            {
                "origin": "https://two.invalid",
                "localStorage": [
                    {"name": "shared", "value": "two"},
                    {"name": "only_two", "value": "2"},
                ],
            },
            {
                "origin": "https://one.invalid",
                "localStorage": [{"name": "shared", "value": "one-new"}],
            },
            {"origin": "", "localStorage": [{"name": "ignored", "value": "x"}]},
            {"origin": "https://empty.invalid", "localStorage": [{"name": "", "value": "x"}]},
        ],
    }
    context = FakeContext()

    asyncio.run(state.restore_storage_state(context, storage_state))

    assert context.cookie_calls == [cookies]
    assert len(context.init_scripts) == 1
    script = context.init_scripts[0]
    encoded_states = script.split("const states = ", 1)[1].split(";", 1)[0]
    states = json.loads(encoded_states)
    assert states == {
        "https://one.invalid": {"shared": "one-new", "only_one": "1"},
        "https://two.invalid": {"shared": "two", "only_two": "2"},
    }
    assert "const pairs = states[location.origin] || {};" in script
    assert "Object.entries(states)" not in script
    assert "only_two" not in states["https://one.invalid"]
    assert "only_one" not in states["https://two.invalid"]


def test_restore_storage_state_accepts_empty_state() -> None:
    class FakeContext:
        async def add_cookies(self, _cookies) -> None:
            raise AssertionError("空 cookies 不应调用 add_cookies")

        async def add_init_script(self, _script) -> None:
            raise AssertionError("空 origins 不应调用 add_init_script")

    asyncio.run(state.restore_storage_state(FakeContext(), {"cookies": [], "origins": []}))
    asyncio.run(state.restore_storage_state(FakeContext(), None))


def test_browser_session_error_carries_structured_status() -> None:
    error = session.BrowserSessionError(
        "文案可以任意修改",
        status="need_config",
        detail={"source": "state"},
    )

    assert str(error) == "文案可以任意修改"
    assert error.status == "need_config"
    assert error.detail == {"source": "state"}


def _module_attribute_uses(alias_to_module: dict[str, str]) -> dict[str, set[str]]:
    """静态收集仓库里 `<alias>.<attr>` 形式的跨模块属性引用。"""
    uses: dict[str, set[str]] = {module: set() for module in alias_to_module.values()}
    for path in sorted(REPO_ROOT.glob("**/*.py")):
        if ".worktrees" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        local_aliases = {
            alias.asname or alias.name.rsplit(".", 1)[-1]: module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            for module in [alias_to_module.get(alias.name)]
            if module is not None
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in local_aliases
            ):
                uses[local_aliases[node.value.id]].add(node.attr)
    return uses


def test_split_browser_modules_keep_every_referenced_symbol() -> None:
    """拆分后的兼容门面必须覆盖全仓引用，包括 `_safe_close_page` 这类私有清理入口。

    这条测试防的是一整类回归：模块拆分时漏了 re-export，调用点直到真正跑浏览器
    才在 finally 里抛 AttributeError，把成功结果覆盖成异常并泄漏浏览器进程。
    """
    alias_to_module = {
        "session": "browser.session",
        "runtime_loop": "browser.runtime_loop",
        "waf": "browser.waf",
        "oauth_flow": "browser.oauth_flow",
        "site_messages": "browser.site_messages",
        "storage_scope": "browser.storage_scope",
        "script_loader": "browser.script_loader",
    }
    uses = _module_attribute_uses(alias_to_module)
    assert uses["browser.session"], "静态扫描必须至少发现既有的 session.* 调用"

    missing: list[str] = []
    for module_name, attributes in uses.items():
        module = importlib.import_module(module_name)
        missing.extend(
            f"{module_name}.{attribute}" for attribute in sorted(attributes) if not hasattr(module, attribute)
        )
    assert not missing, f"以下符号在拆分后已不可访问：{missing}"


def test_browser_resources_close_is_complete_and_idempotent() -> None:
    calls: list[str] = []

    class FailingPage:
        async def close(self) -> None:
            calls.append("page")
            raise RuntimeError("page already closed")

    class FakeBrowser:
        async def close(self) -> None:
            calls.append("browser")

    resources = BrowserResources(browser=FakeBrowser(), page=FailingPage())

    asyncio.run(resources.close())
    asyncio.run(resources.close())

    assert calls == ["page", "browser"]
    assert resources.page is None
    assert resources.browser is None


def test_run_sync_inside_running_event_loop() -> None:
    async def outer() -> str:
        return session.run_sync(asyncio.sleep(0, result="ok"))

    assert asyncio.run(outer()) == "ok"


def test_run_sync_propagates_nested_exception() -> None:
    async def fail() -> None:
        raise RuntimeError("boom")

    async def outer() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            session.run_sync(fail())

    asyncio.run(outer())


def test_oauth_callback_url_requires_exact_origin() -> None:
    base_url = "https://site.invalid"

    assert is_oauth_callback_url("https://site.invalid/api/oauth/linuxdo?code=ok", base_url)
    assert is_oauth_callback_url("https://site.invalid:443/console", base_url)
    assert not is_oauth_callback_url("https://evil-site.invalid/api/oauth/linuxdo?code=ok", base_url)
    assert not is_oauth_callback_url("https://sub.site.invalid/api/oauth/linuxdo?code=ok", base_url)
    assert not is_oauth_callback_url("http://site.invalid/api/oauth/linuxdo?code=ok", base_url)
    assert not is_oauth_callback_url("https://site.invalid:8443/api/oauth/linuxdo?code=ok", base_url)
    assert not is_oauth_callback_url("https://site.invalid/login?next=code", base_url)


def test_oauth_callback_accepts_business_page_landing() -> None:
    """同源业务页就是有效回跳终点，不能强求 /console、/oauth 或 code=。

    有的站点 callback 成功后直接 302 到业务页并去掉 code：实测 ABR 福利站落在
    `/checkin`，旧判据把这次成功回跳报成「OAuth 未跳回站点，停在 <站点地址>」，
    进而误判为登录失败。
    """
    base_url = "https://checkin.new-api.abrdns.com"

    assert is_oauth_callback_url("https://checkin.new-api.abrdns.com/checkin", base_url)
    assert is_oauth_callback_url("https://checkin.new-api.abrdns.com/", base_url)
    # 仍停在本站登录入口说明授权没走完，不算回跳
    assert not is_oauth_callback_url(
        "https://checkin.new-api.abrdns.com/auth/linuxdo/login", base_url
    )
    # 但带 code 的 /auth/... 正是标准 callback
    assert is_oauth_callback_url(
        "https://checkin.new-api.abrdns.com/auth/linuxdo/callback?code=abc", base_url
    )
    # 跨源（仍在 provider 授权页）永远不算回跳
    assert not is_oauth_callback_url(
        "https://connect.linux.do/oauth2/authorize?client_id=x", base_url
    )


def test_site_success_message_extracts_agentrouter_toast() -> None:
    assert session._site_success_message(["dom: 登录成功，今日奖励已发放"]) == "登录成功，今日奖励已发放"
    assert session._site_success_message(["dom: 今日已签到", "dom: 登录失败，请重试"]) == ""


def test_attach_site_errors_separates_success_toast_from_errors() -> None:
    target: dict = {}

    session._attach_site_errors(
        target,
        ["dom: 登录成功，今日奖励已发放", "response: HTTP 429 https://example.invalid/api/user"],
    )

    assert target["site_success_message"] == "登录成功，今日奖励已发放"
    assert target["site_errors"] == ["response: HTTP 429 https://example.invalid/api/user"]
    assert "登录成功" not in target["site_error"]


def test_wait_for_site_success_message_captures_delayed_toast() -> None:
    class FakePage:
        def __init__(self) -> None:
            self.calls = 0

        async def evaluate(self, _script: str) -> list[str]:
            self.calls += 1
            return [] if self.calls == 1 else ["登录成功，获得每日额度"]

    page = FakePage()
    target: dict = {}
    message = asyncio.run(
        session._wait_for_site_success_message(
            page,
            {"items": [], "tasks": []},
            target,
            timeout_ms=500,
        )
    )

    assert message == "登录成功，获得每日额度"
    assert target["site_success_message"] == message
    assert page.calls == 2


def test_oauth_result_uses_success_toast_when_quota_is_unchanged() -> None:
    result = session._oauth_checkin_result(
        1_000_000,
        1_000_000,
        {"landed_back": True, "site_success_message": "登录成功，今日奖励已发放"},
    )

    assert result["status"] == "success"
    assert result["message"] == "签到成功（站点弹窗：登录成功，今日奖励已发放）。"


def test_oauth_result_without_success_toast_remains_already_done() -> None:
    result = session._oauth_checkin_result(1_000_000, 1_000_000, {"landed_back": True})

    assert result["status"] == "already_done"


def test_cloudflare_midway_does_not_veto_a_landed_oauth() -> None:
    """实测 AgentRouter：授权页先弹 CF 挑战、ClickSolver 报「未能通过」，
    但随后授权成功、跳回站点并弹出「签到成功，新增额度已到账」。
    cloudflare 只说明「过程中弹过验证」，人都跳回来了不能再否决结论。
    """
    link = {
        "landed_back": True,
        "cloudflare": True,
        "site_success_message": "签到成功，新增额度已到账",
    }
    result = session._oauth_checkin_result(None, 199_805_975, link)

    assert result["status"] == "success"
    assert "重新捕获登录态" not in result["message"]


def test_need_human_and_waf_still_veto_oauth() -> None:
    """真正没完成的两种情形必须继续否决：停在第三方登录页、WAF 持续拦截。"""
    assert not session._oauth_landed({"landed_back": True, "need_human": True})
    assert not session._oauth_landed({"landed_back": True, "waf_blocked": True})
    assert not session._oauth_landed({"landed_back": False})
    assert session._oauth_landed({"landed_back": True, "cloudflare": True})


def test_site_error_noise_is_dropped() -> None:
    """公告接口失败、JSHandle@object、CF beacon 等与签到成败无关，
    留着只会把真正的失败原因挤出「站点原始错误」（实测挤掉了一次成功结论）。
    """
    collector: dict = {"items": [], "tasks": []}
    session._add_site_error(collector, "console.error", "获取公告失败: Error")
    session._add_site_error(collector, "console.error", "JSHandle@object")
    session._add_site_error(collector, "console.error",
                            "Cross-Origin Request Blocked: ... static.cloudflareinsights.com/beacon.min.js ...")
    session._add_site_error(collector, "console.warning",
                            'Storage access automatically granted for origin “https://connect.linux.do”')
    assert collector["items"] == []

    session._add_site_error(collector, "response", "HTTP 403 https://s.invalid/api/user/checkin body=账号已封禁")
    assert len(collector["items"]) == 1


def test_landed_oauth_drops_pre_login_errors() -> None:
    """OAuth 已跳回站点时，登录前必然出现的 401 不该再被报成「站点原始错误」。"""
    link: dict = {"landed_back": True, "site_error": "旧的 401", "site_errors": ["旧的 401"]}
    session._attach_oauth_completion_messages(
        link,
        [
            "response: HTTP 401 https://s.invalid/api/user/self body=未登录",
            "dom: 签到成功，新增额度已到账",
        ],
    )
    assert "site_error" not in link and "site_errors" not in link
    assert link["site_success_message"] == "签到成功，新增额度已到账"


def test_unfinished_oauth_keeps_raw_errors_for_diagnosis() -> None:
    """没跳回站点时相反：必须保留原始错误，否则失败无从排查。"""
    link: dict = {"landed_back": False}
    session._attach_oauth_completion_messages(
        link, ["response: HTTP 500 https://s.invalid/api/oauth/state body=boom"]
    )
    assert "HTTP 500" in link["site_error"]


# ── refresh_token 快照兜底 ───────────────────────────────────────────────────
# 背景（实测时序）：access_token 过期时 sub2api 前端收到 401 会清空 localStorage
# 再跳登录页，而 add_init_script 每次导航又重新注入，两者约每 2 秒来回竞争。
# 只读“活”存储会随机拿到空值，误报 refresh_token not found 并退化成开浏览器
# 重登。解码后的登录态快照是静态的，不受该竞争影响，必须作为兜底来源。

def _state_with_refresh(origin: str = "https://site.invalid", value: str = "rt_abc123") -> dict:
    return {
        "cookies": [],
        "origins": [
            {
                "origin": origin,
                "localStorage": [
                    {"name": "auth_token", "value": "jwt"},
                    {"name": "refresh_token", "value": value},
                ],
            }
        ],
    }


def test_storage_refresh_token_reads_from_snapshot() -> None:
    assert session.storage_refresh_token(_state_with_refresh()) == "rt_abc123"


def test_storage_refresh_token_survives_cleared_live_storage() -> None:
    """活存储被前端清空（origins 里没有 refresh_token）时返回空，需由调用方回落快照。"""
    cleared = {
        "cookies": [],
        "origins": [
            {
                "origin": "https://site.invalid",
                "localStorage": [{"name": "sub2api_site_usage_notice_v1", "value": "accepted"}],
            }
        ],
    }
    assert session.storage_refresh_token(cleared) == ""
    # 调用方的兜底表达式：活存储为空时取快照里的值
    snapshot = _state_with_refresh()
    assert (session.storage_refresh_token(cleared)
            or session.storage_refresh_token(snapshot)) == "rt_abc123"


def test_storage_refresh_token_handles_malformed_input() -> None:
    for bad in (None, {}, {"origins": None}, {"origins": [None]},
                {"origins": [{"localStorage": [None]}]},
                {"origins": [{"localStorage": [{"name": "refresh_token", "value": ""}]}]}):
        assert session.storage_refresh_token(bad) == ""
