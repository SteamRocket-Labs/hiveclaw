"""Unknown native execution outcomes must not advertise a safe effect retry."""

import json

from app.services.runtime_tool_timeout_sources import _timeout_content_hash_matches, _timeout_text
from app.services.session_model_inputs import _hash


def test_new_timeout_text_preserves_unknown_execution_recovery():
    content = _timeout_text("write_file", 30)
    error = json.loads(content.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert error["error_class"] == "timeout"
    assert error["retryable"] is False
    assert _timeout_content_hash_matches("write_file", 30, _hash(content))


def test_historical_timeout_bytes_remain_verifiable():
    content = (
        "❌ write_file exceeded the 30 second time limit.\n\n"
        "Hint: Try a simpler request, smaller input, or a more targeted operation.\n\n"
        '<tool_error>{"ok": false, "tool_name": "write_file", "error_class": "timeout", '
        '"message": "write_file exceeded the 30 second time limit.", "provider": "runtime", '
        '"http_status": null, "retryable": true, "actionable_hint": '
        '"Try a simpler request, smaller input, or a more targeted operation."}</tool_error>'
    )
    assert _timeout_text("write_file", 30, legacy_format=True) == content
    assert _timeout_content_hash_matches("write_file", 30, _hash(content))
    assert _timeout_text("write_file", 30) != content


def test_timeout_receipt_rejects_changed_tool_duration_or_content():
    content = _timeout_text("write_file", 30, legacy_format=True)
    content_hash = _hash(content)
    assert not _timeout_content_hash_matches("read_file", 30, content_hash)
    assert not _timeout_content_hash_matches("write_file", 31, content_hash)
    assert not _timeout_content_hash_matches("write_file", 30, _hash(content + "tampered"))


def test_historical_unknown_outcome_timeout_bytes_remain_verifiable():
    content = (
        "❌ write_file exceeded the 30 second time limit.\n\n"
        "Hint: The execution outcome is unknown. Inspect the original tool's state or read back its output "
        "before requesting another effect.\n\n"
        '<tool_error>{"ok": false, "tool_name": "write_file", "error_class": "timeout", '
        '"message": "write_file exceeded the 30 second time limit.", "provider": "runtime", '
        '"http_status": null, "retryable": false, "actionable_hint": '
        '"The execution outcome is unknown. Inspect the original tool\'s state or read back its output '
        'before requesting another effect."}</tool_error>'
    )
    assert _timeout_content_hash_matches("write_file", 30, _hash(content))
    assert not _timeout_content_hash_matches("read_file", 30, _hash(content))
