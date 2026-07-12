from __future__ import annotations

from ci import patch_playwright


def test_patch_applies_page_error_and_missing_response_guards() -> None:
    source = f"""
url: pageError.location.url,
line: pageError.location.lineNumber,
column: pageError.location.columnNumber,
{patch_playwright.NETWORK_RESPONSE_UNSAFE_MARKER}
response2.setEncodedBodySize(event.encodedBodySize);
"""

    patched, count = patch_playwright.apply_replacements(source)

    assert count == 4
    assert patch_playwright.PAGE_ERROR_UNSAFE_MARKER not in patched
    assert patch_playwright.NETWORK_RESPONSE_UNSAFE_MARKER not in patched
    assert "if (!response2)" in patched
    assert "this._requests.delete(request2._id);" in patched


def test_patch_applies_network_guard_when_page_error_was_already_fixed() -> None:
    source = patch_playwright.NETWORK_RESPONSE_UNSAFE_MARKER

    patched, count = patch_playwright.apply_replacements(source)

    assert count == 1
    assert "if (!response2)" in patched


def test_patch_is_idempotent() -> None:
    patched, first_count = patch_playwright.apply_replacements(
        patch_playwright.NETWORK_RESPONSE_UNSAFE_MARKER
    )
    second, second_count = patch_playwright.apply_replacements(patched)

    assert first_count == 1
    assert second_count == 0
    assert second == patched
