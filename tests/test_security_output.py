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


# ── 已复现的脱敏缺口回归 ──────────────────────────────────────────────────────
def test_non_http_proxy_credentials_are_masked() -> None:
    """CLI 明确支持 socks5 代理，旧正则只匹配 http(s)，凭据原样进日志。"""
    for scheme in ("socks5", "socks5h", "socks4", "http", "https"):
        text = mask_secrets(f"{scheme}://fakeuser:fakepass123@proxy.invalid:1080")
        assert "fakepass123" not in text
        assert "fakeuser" not in text
        assert scheme in text


def test_opaque_bearer_token_is_fully_masked() -> None:
    """base64 token 含 + / =，旧字符集只掩码到第一个 +，其后原样泄露。"""
    raw = "abc+fakesecret/xyz=="
    text = mask_secrets(f"Authorization: Bearer {raw}")
    assert raw not in text
    assert "fakesecret" not in text
    assert "•" in text


def test_suffix_based_sensitive_keys_are_redacted() -> None:
    safe = sanitize_data(
        {
            "api_key": "FAKE_API_KEY_VALUE",
            "api-key": "FAKE_API_KEY_DASHED",
            "client_secret": "FAKE_CLIENT_SECRET",
            "proxy_password": "FAKE_PROXY_PW",
            "private_key": "FAKE_PRIVATE_KEY",
            "site_cookie": "session=FAKE_SESSION",
            "storage_state": "FAKE_STORAGE_STATE",
            "browser_state_text": "FAKE_BROWSER_STATE_TEXT",
            "plain_field": "keep-me",
        }
    )
    text = json.dumps(safe, ensure_ascii=False)
    for secret in (
        "FAKE_API_KEY_VALUE",
        "FAKE_API_KEY_DASHED",
        "FAKE_CLIENT_SECRET",
        "FAKE_PROXY_PW",
        "FAKE_PRIVATE_KEY",
        "FAKE_SESSION",
        "FAKE_STORAGE_STATE",
        "FAKE_BROWSER_STATE_TEXT",
    ):
        assert secret not in text
    assert safe["plain_field"] == "keep-me"


def test_free_form_api_key_field_is_masked() -> None:
    text = mask_secrets('{"api_key": "FAKE_FREEFORM_KEY", "client_secret": "FAKE_FREEFORM_SECRET"}')
    assert "FAKE_FREEFORM_KEY" not in text
    assert "FAKE_FREEFORM_SECRET" not in text


def test_markdown_report_shows_retry_execution_and_carried_counts() -> None:
    markdown = report.build_report(
        {
            "results": [
                {
                    "site": "retried-now",
                    "ok": True,
                    "icon": "✅",
                    "label": "成功",
                    "note": "recovered",
                    "executed_this_run": True,
                    "carried_forward": False,
                    "retried": True,
                    "retry_succeeded": True,
                },
                {
                    "site": "carried",
                    "ok": True,
                    "icon": "✅",
                    "label": "已领取",
                    "note": "cached",
                    "executed_this_run": False,
                    "carried_forward": True,
                    "retried": False,
                    "retry_succeeded": False,
                },
                {
                    "site": "retried-before",
                    "ok": True,
                    "icon": "✅",
                    "label": "成功",
                    "note": "cached retry success",
                    "executed_this_run": False,
                    "carried_forward": True,
                    "retried": False,
                    "retry_succeeded": True,
                },
            ]
        }
    )

    assert "- 本轮实际执行: 1" in markdown
    assert "- 沿用上次完成: 2" in markdown
    assert "- 本轮重试成功: 1" in markdown
    assert markdown.count("🔁 重试成功") == 2
    assert markdown.count("本轮跳过") == 2
