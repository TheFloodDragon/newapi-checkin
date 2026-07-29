from __future__ import annotations

from pathlib import Path

import accounts_store
from providers.auth import load_auth, load_cookie_file
from providers.base import SiteConfig


def test_load_cookie_file_writes_back_cleaned_cookie_by_default(tmp_path: Path) -> None:
    path = tmp_path / "cookie.txt"
    path.write_text("a=1; session=old; session=new\n42\naccess-token\n", encoding="utf-8")

    auth = load_cookie_file(path)

    assert auth.cookie == "a=1; session=new"
    assert path.read_text(encoding="utf-8") == "a=1; session=new\n42\naccess-token\n"


def test_load_auth_cleans_in_memory_without_writing_when_disabled(
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "cookie.txt"
    original = "session=old; theme=dark; session=new\n42\naccess-token\n"
    path.write_text(original, encoding="utf-8")
    site = SiteConfig(
        name="site",
        base_url="https://site.invalid",
        cookie_file=str(path),
        auto_refresh_cookie=False,
    )

    auth = load_auth(site)

    assert auth.cookie == "session=new; theme=dark"
    assert path.read_text(encoding="utf-8") == original
    assert "已清理" not in capsys.readouterr().err


def test_load_cookie_file_preserves_third_line_access_token(tmp_path: Path) -> None:
    path = tmp_path / "cookie.txt"
    path.write_text("session=old; session=new\n42\nthird-line-token\n", encoding="utf-8")

    auth = load_cookie_file(path)

    assert auth.access_token == "third-line-token"
    assert path.read_text(encoding="utf-8").splitlines() == [
        "session=new",
        "42",
        "third-line-token",
    ]


def test_load_cookie_file_write_failure_keeps_cleaned_cookie_in_memory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "cookie.txt"
    original = "session=old; session=new\n42\nthird-line-token\n"
    path.write_text(original, encoding="utf-8")

    def fail_write(_path: Path, _text: str) -> None:
        raise OSError("write failed")

    monkeypatch.setattr(accounts_store, "atomic_write_text", fail_write)

    auth = load_cookie_file(path)

    assert auth.cookie == "session=new"
    assert auth.access_token == "third-line-token"
    assert path.read_text(encoding="utf-8") == original
