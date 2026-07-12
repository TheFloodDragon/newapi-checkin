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
