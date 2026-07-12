from __future__ import annotations

import pytest

import accounts_store
from browser import oauth_providers, state


def _encoded_state(domain: str) -> str:
    return state.encode_state(
        {
            "cookies": [
                {
                    "name": "session",
                    "value": "secret",
                    "domain": domain,
                    "path": "/",
                }
            ],
            "origins": [],
        }
    )


@pytest.mark.parametrize(
    ("hostname", "domain", "expected"),
    [
        ("github.com", ".github.com", True),
        ("api.github.com", "github.com", True),
        ("GITHUB.COM", "github.com", True),
        ("evilgithub.com", "github.com", False),
        ("github.com.evil.invalid", "github.com", False),
        ("", "github.com", False),
    ],
)
def test_hostname_matches_domain_boundary(hostname: str, domain: str, expected: bool) -> None:
    assert oauth_providers.hostname_matches_domain(hostname, domain) is expected


@pytest.mark.parametrize(
    ("provider", "url", "expected"),
    [
        ("linuxdo", "https://linux.do/", True),
        ("linuxdo", "https://connect.linux.do/oauth2/authorize", True),
        ("linuxdo", "https://foo.connect.linux.do/path", True),
        ("linuxdo", "https://evil-linux.do/", False),
        ("linuxdo", "https://linux.do.evil.invalid/", False),
        ("linuxdo", "http://connect.linux.do/oauth2/authorize", False),
        ("github", "https://github.com/login/oauth/authorize", True),
        ("github", "https://api.github.com/path", True),
        ("github", "https://evilgithub.com/", False),
        ("github", "https://github.com.evil.invalid/", False),
        ("github", "http://github.com/login", False),
        ("github", "not a url", False),
        ("github", "https://[invalid", False),
    ],
)
def test_oauth_provider_matches_only_https_hostname_boundaries(
    provider: str,
    url: str,
    expected: bool,
) -> None:
    assert oauth_providers.get_oauth_provider(provider).matches_url(url) is expected


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        (".connect.linux.do", "linuxdo"),
        (".linux.do", "linuxdo"),
        ("LOGIN.LINUX.DO", "linuxdo"),
        (".github.com", "github"),
        ("api.github.com", "github"),
        ("evil-linux.do", ""),
        ("github.com.evil.invalid", ""),
        ("evilgithub.com", ""),
    ],
)
def test_guess_oauth_provider_uses_cookie_domain_boundaries(domain: str, expected: str) -> None:
    assert accounts_store.guess_oauth_provider(_encoded_state(domain)) == expected


def test_state_contains_site_domain_uses_cookie_scope_direction() -> None:
    parent_cookie = _encoded_state(".example.com")
    child_cookie = _encoded_state("auth.example.com")

    assert accounts_store.state_contains_site_domain(parent_cookie, "https://example.com")
    assert accounts_store.state_contains_site_domain(parent_cookie, "https://app.example.com")
    assert accounts_store.state_contains_site_domain(child_cookie, "https://auth.example.com")
    assert not accounts_store.state_contains_site_domain(child_cookie, "https://example.com")
    assert not accounts_store.state_contains_site_domain(parent_cookie, "https://example.com.evil.invalid")
    assert not accounts_store.state_contains_site_domain(parent_cookie, "not a valid host")


@pytest.mark.parametrize(
    ("provider", "cookies", "expected"),
    [
        ("linuxdo", [{"name": "_t", "value": "token", "domain": ".linux.do"}], True),
        ("linuxdo", [{"name": "_forum_session", "value": "anon", "domain": ".linux.do"}], False),
        ("linuxdo", [{"name": "_t", "value": "token", "domain": "evil-linux.do"}], False),
        ("github", [{"name": "user_session", "value": "token", "domain": "github.com"}], True),
        ("github", [{"name": "logged_in", "value": "yes", "domain": "github.com"}], False),
        ("github", [{"name": "user_session", "value": "token", "domain": "github.com.evil.invalid"}], False),
    ],
)
def test_provider_requires_authenticated_cookie(
    provider: str,
    cookies: list[dict[str, object]],
    expected: bool,
) -> None:
    assert oauth_providers.get_oauth_provider(provider).has_authenticated_state(cookies) is expected
