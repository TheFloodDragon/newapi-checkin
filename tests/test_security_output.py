from __future__ import annotations

import json
from pathlib import Path

import accounts_store
from ci import report
from mask_utils import mask_secrets, sanitize_data


def test_masking_covers_nested_and_free_form_secrets() -> None:
    raw = {
        "access_token": "very-secret-token",
        "detail": {
            "message": "Authorization: Bearer eyJabcdefghijk.abcdefghijklmnop.qrstuvwxyz and sk-abcdefghijklmnop",
            "cookie": "session=secret-cookie",
        },
    }
    safe = sanitize_data(raw)
    text = json.dumps(safe, ensure_ascii=False)
    assert "very-secret-token" not in text
    assert "secret-cookie" not in text
    assert "eyJabcdefghijk" not in text
    assert "sk-abcdefghijklmnop" not in text
    assert "<redacted>" in text


def test_markdown_report_escapes_cells_and_masks_tokens() -> None:
    markdown = report.build_report(
        {
            "results": [
                {
                    "site": "<b>site|name</b>",
                    "ok": False,
                    "icon": "",
                    "label": "失败",
                    "message": "token=abcdefghijklmnop\nnext",
                }
            ]
        }
    )
    assert "<b>" not in markdown
    assert "site\\|name" in markdown
    assert "abcdefghijklmnop" not in markdown
    assert "\nnext" not in markdown


def test_shared_atomic_writer_replaces_complete_file(tmp_path: Path) -> None:
    path = tmp_path / "result.json"
    accounts_store.atomic_write_text(path, '{"ok":true}')
    assert path.read_text(encoding="utf-8") == '{"ok":true}'
    accounts_store.atomic_write_text(path, '{"ok":false}')
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": False}


def test_mask_secrets_preserves_non_secret_text() -> None:
    assert mask_secrets("plain message") == "plain message"
