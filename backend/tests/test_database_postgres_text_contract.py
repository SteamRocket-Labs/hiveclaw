from __future__ import annotations

import json

import pytest


def test_postgres_nul_repair_preserves_nested_text_as_a_visible_escape() -> None:
    from app.database import POSTGRES_NUL_ESCAPE, repair_postgres_nul

    repaired, replacement_count = repair_postgres_nul(
        {
            "plain": "before\x00after",
            "nested": ["\x00", {"key\x00": "value\x00"}],
            "untouched": "literal \\u0000 text",
        }
    )

    assert POSTGRES_NUL_ESCAPE == r"\u0000"
    assert repaired == {
        "plain": r"before\u0000after",
        "nested": [r"\u0000", {r"key\u0000": r"value\u0000"}],
        "untouched": r"literal \u0000 text",
    }
    assert replacement_count == 4


def test_postgres_nul_repair_rejects_json_key_collisions_instead_of_dropping_evidence() -> None:
    from app.database import PostgresTextContractError, repair_postgres_nul

    with pytest.raises(PostgresTextContractError, match="JSON object key collision"):
        repair_postgres_nul({"key\x00": "first", r"key\u0000": "second"})


def test_postgres_json_serializer_repairs_nul_before_json_encoding() -> None:
    from app.database import _postgres_json_serializer

    encoded = _postgres_json_serializer({"content": "before\x00after", "parts": ["\x00"]})

    assert json.loads(encoded) == {
        "content": r"before\u0000after",
        "parts": [r"\u0000"],
    }
    assert r'"before\\u0000after"' in encoded


def test_postgres_text_contract_snapshot_is_observable_without_content_leakage() -> None:
    from app.database import snapshot_postgres_text_contract

    snapshot = snapshot_postgres_text_contract()

    assert snapshot["encoding"] == "literal_unicode_escape"
    assert snapshot["replacement"] == r"\u0000"
    assert isinstance(snapshot["repair_events"], int)
    assert isinstance(snapshot["repaired_codepoints"], int)
    assert "last_value" not in snapshot
