from __future__ import annotations

from types import SimpleNamespace

from providers.actions import browser_script
from providers.base import SiteConfig


class FakeRunner:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = list(statuses)
        self.calls: list[dict[str, object]] = []

    def run_sync(self, **kwargs):
        self.calls.append(kwargs)
        status = self.statuses.pop(0)
        return SimpleNamespace(status=status, message=status, detail={})


def _site() -> SiteConfig:
    return SiteConfig(
        name="script",
        base_url="https://script.invalid",
        auth_method="browser",
        checkin_action="browser_script",
        browser_state="site-state",
        script="scripts/checkin/100xlabs.py",
        oauth_fallback_provider="linuxdo",
        oauth_fallback_account="default",
    )


def _install(monkeypatch, runner: FakeRunner, oauth_state: str = "oauth-state") -> None:
    monkeypatch.setattr(browser_script, "_load_runner", lambda: runner)
    monkeypatch.setattr(
        browser_script.accounts_store,
        "oauth_state_text",
        lambda provider, account: oauth_state,
    )


def test_browser_script_prefers_site_state_without_eager_oauth(monkeypatch) -> None:
    runner = FakeRunner(["success"])
    _install(monkeypatch, runner)

    result = browser_script.run_action(_site(), SimpleNamespace())

    assert result.status == "success"
    assert len(runner.calls) == 1
    assert runner.calls[0]["browser_state_text"] == "site-state"
    assert runner.calls[0]["oauth_provider"] == ""
    assert not result.detail.get("oauth_fallback_used")


def test_script_handles_oauth_keeps_shared_state_without_pretrigger(monkeypatch) -> None:
    runner = FakeRunner(["success"])
    _install(monkeypatch, runner)
    site = _site()
    site.auth_method = "oauth"
    site.browser_state = ""
    site.oauth_fallback_provider = ""
    site.oauth_fallback_account = ""
    site.oauth_provider = "linuxdo"
    site.oauth_account = "default"
    site.script_args = {"script_handles_oauth": True}

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "success"
    assert len(runner.calls) == 1
    assert runner.calls[0]["browser_state_text"] == "oauth-state"
    assert runner.calls[0]["oauth_provider"] == ""
    assert result.detail["script_handles_oauth"] is True


def test_browser_script_retries_once_with_oauth_on_need_login(monkeypatch) -> None:
    runner = FakeRunner(["need_login", "success"])
    _install(monkeypatch, runner)

    result = browser_script.run_action(_site(), SimpleNamespace())

    assert result.status == "success"
    assert len(runner.calls) == 2
    assert runner.calls[1]["browser_state_text"] == "oauth-state"
    assert runner.calls[1]["oauth_provider"] == "linuxdo"
    assert result.detail["oauth_fallback_used"] is True


def test_browser_script_uses_oauth_when_site_state_is_missing(monkeypatch) -> None:
    runner = FakeRunner(["success"])
    _install(monkeypatch, runner)
    site = _site()
    site.browser_state = ""

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "success"
    assert len(runner.calls) == 1
    assert runner.calls[0]["oauth_provider"] == "linuxdo"
    assert result.detail["oauth_fallback_used"] is True


def test_browser_script_reports_missing_fallback_state(monkeypatch) -> None:
    runner = FakeRunner(["success"])
    _install(monkeypatch, runner, oauth_state="")
    site = _site()
    site.browser_state = ""

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "error"
    assert "linuxdo:default" in result.message
    assert "签到失败" in result.message
    assert runner.calls == []


def test_browser_script_without_oauth_reports_failed_checkin_on_expired_state(monkeypatch) -> None:
    runner = FakeRunner(["need_login"])
    _install(monkeypatch, runner)
    site = _site()
    site.oauth_fallback_provider = ""
    site.oauth_fallback_account = ""

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "error"
    assert "登录态缓存已失效" in result.message
    assert "未配置 OAuth 兜底" in result.message
    assert "签到失败" in result.message
    assert len(runner.calls) == 1
    assert runner.calls[0]["browser_state_text"] == "site-state"
    assert runner.calls[0]["oauth_provider"] == ""


def test_api_first_uses_plain_http_client_not_browser_refresher(monkeypatch) -> None:
    """有 access_token 的站点必须先走纯 API，且不得触发会启动浏览器的刷新器。

    用户要求的顺序是「先 API（纯 HTTP）→ 再登录态 → 再账密」。
    build_lazy_refresh_client 注入的 token_refresher 会拉起 Camoufox，
    放在第 1 级会让「纯 API」名不副实（实测日志里出现过 Camoufox 启动），
    因此第 1 级只能用 build_client 的纯 HTTP 客户端。
    """
    runner = FakeRunner([])
    _install(monkeypatch, runner)

    class FakeClient:
        base_url = "https://script.invalid"
        quota_is_usd = True

        def fetch_status(self):
            return SimpleNamespace(checked_in_today=False, quota_usd=3.0)

        def do_checkin(self, _turnstile=""):
            return SimpleNamespace(
                already_done=False,
                checkin_unconfirmed=False,
                quota_awarded=1,
                raw={"reward_amount": 1},
            )

    build_calls: list[object] = []

    class FakeProfile:
        def build_lazy_refresh_client(self, site):  # pragma: no cover - 不应被调用
            raise AssertionError("第 1 级不得使用会启动浏览器的 lazy 刷新客户端")

        def build_client(self, site, auth):
            build_calls.append(auth)
            return FakeClient()

        def classify(self, error):
            return "error"

    site = _site()
    site.access_token = "cached-jwt"

    result = browser_script.run_action(site, FakeProfile())

    assert result.status == "success"
    assert result.detail["api_first"] is True
    assert result.detail["api_stage"] == "token"
    assert len(build_calls) == 1
    # 纯 API 已拿到结论，绝不应启动浏览器脚本。
    assert runner.calls == []

def test_missing_state_with_script_credentials_still_runs_script(monkeypatch) -> None:
    """无 browser_state 但 script_args 有账密时，应让脚本自行登录而非直接失败。"""
    runner = FakeRunner(["success"])
    _install(monkeypatch, runner, oauth_state="")
    site = _site()
    site.browser_state = ""
    site.oauth_fallback_provider = ""
    site.oauth_fallback_account = ""
    site.script_args = {"email": "user@example.test", "password": "pw"}

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "success"
    assert len(runner.calls) == 1
    assert runner.calls[0]["browser_state_text"] == ""
    assert result.detail["self_login"] is True


def test_missing_state_without_any_credentials_reports_clear_error(monkeypatch) -> None:
    """既无登录态、又无 OAuth 兜底、也无脚本凭据时，错误信息需覆盖三种缺失。"""
    runner = FakeRunner([])
    _install(monkeypatch, runner, oauth_state="")
    site = _site()
    site.browser_state = ""
    site.oauth_fallback_provider = ""
    site.oauth_fallback_account = ""
    site.script_args = {}

    result = browser_script.run_action(site, SimpleNamespace())

    assert result.status == "error"
    assert "脚本账密凭据" in result.message
    assert runner.calls == []
