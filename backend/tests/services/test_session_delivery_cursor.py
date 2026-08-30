from __future__ import annotations

import pytest

from app.services.session_delivery_cursor import (
    LEGACY_RANKED_SEQUENCE_PROJECTION,
    SessionDeliveryCursorError,
    project_session_event_for_delivery,
    resolve_session_delivery_cursor,
)


def test_all_unsafe_legacy_storage_uses_dense_delivery_cursor_without_rewriting_evidence() -> None:
    first_storage_sequence = 1_777_000_000_000_000_000
    cursor = resolve_session_delivery_cursor(
        event_count=3,
        storage_first_sequence=first_storage_sequence,
        storage_last_sequence=first_storage_sequence + 90_000_000_000,
    )

    original = {
        "schema": "hive.session_event_compatibility",
        "schema_version": 1,
        "event_id": "event-2",
        "sequence": first_storage_sequence + 60_000_000_000,
    }
    projected = project_session_event_for_delivery(
        original,
        cursor=cursor,
        storage_sequence=original["sequence"],
        delivery_sequence=2,
    )

    assert cursor.mode == LEGACY_RANKED_SEQUENCE_PROJECTION
    assert cursor.last_committed_delivery_sequence == 3
    assert projected["sequence"] == 2
    assert projected["storage_sequence"] == str(original["sequence"])
    assert projected["sequence_projection"] == LEGACY_RANKED_SEQUENCE_PROJECTION
    assert original["sequence"] == first_storage_sequence + 60_000_000_000
    assert "storage_sequence" not in original


def test_mixed_safe_and_unsafe_storage_namespaces_are_not_silently_normalized() -> None:
    with pytest.raises(SessionDeliveryCursorError, match="session_delivery_cursor_unrecoverable"):
        resolve_session_delivery_cursor(
            event_count=2,
            storage_first_sequence=1,
            storage_last_sequence=1_777_000_000_000_000_000,
        )
