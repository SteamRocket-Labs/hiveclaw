"""Revalidate exact sources once within one complete runtime-result read."""

from collections import Counter
from contextvars import ContextVar
import hashlib
import json
import platform

import pytest

from app.core.source_read_purpose import current_source_recipient
from app.services import content_input_receipts as receipts
from app.services import runtime_result_sources as results
from tests.integration.test_m1g49_delegation_instruction_origin_postgres import (
    test_governed_delegation_preserves_its_parent_instruction_origin as exercise_first_task,
)

pytestmark = [
    pytest.mark.usefixtures("migrated_pg_url"),
    pytest.mark.skipif(platform.system() != "Darwin", reason="Native macOS context gate; Linux delivery is deferred."),
]


async def test_one_runtime_result_read_reuses_only_its_exact_successful_sources(
    owner_sessionmaker,
    app_user_sessionmaker,
    monkeypatch,
    tmp_path,
    migrated_pg_url,
):
    validate = results.validate_runtime_result_sources
    batch = receipts._validate_input_dependency_batch
    checking = ContextVar("runtime_result_validation_under_test", default=None)
    successful = []

    async def read_sources(db, request, result_object, *, user=None, session=None):
        counts = Counter()
        token = checking.set(counts)
        try:
            value = await validate(db, request, result_object, user=user, session=session)
            if value is True:
                successful.append({"result_id": str(result_object.id), "counts": counts})
            return value
        finally:
            checking.reset(token)

    async def count_sources(db, tenant_id, dependencies, **kwargs):
        counts = checking.get()
        active = receipts._ACTIVE_GROUP_TOOL_SOURCES.get()
        keys = []
        if counts is not None and isinstance(dependencies, list):
            for source in dependencies:
                if not isinstance(source, dict) or set(source) != {"source_kind", "source_id", "source_version"}:
                    continue
                if any(not isinstance(value, str) for value in source.values()):
                    continue
                if (
                    source["source_kind"] == "session_tool_invocation"
                    and (source["source_id"], source["source_version"]) in active
                ):
                    continue  # Active back-edges must still reach the cycle check.
                keys.append(
                    (
                        id(db),
                        tenant_id,
                        id(kwargs.get("user")),
                        id(kwargs.get("runtime_request")),
                        current_source_recipient(),
                        kwargs.get("active_workspace_receipts", frozenset()),
                        source["source_kind"],
                        source["source_id"],
                        source["source_version"],
                    )
                )
        value = await batch(db, tenant_id, dependencies, **kwargs)
        if counts is not None:
            counts.update(keys)
        return value

    monkeypatch.setattr(results, "validate_runtime_result_sources", read_sources)
    monkeypatch.setattr(receipts, "_validate_input_dependency_batch", count_sources)
    await exercise_first_task(
        owner_sessionmaker=owner_sessionmaker,
        app_user_sessionmaker=app_user_sessionmaker,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        input_case="first_task_child_clock",
        migrated_pg_url=migrated_pg_url,
    )

    assert successful
    report = [
        {
            "result_id": row["result_id"],
            "checks": [
                {
                    "reader_key_sha256": hashlib.sha256(repr(key[:-3]).encode()).hexdigest(),
                    "source_kind": key[-3],
                    "source_id": key[-2],
                    "source_version": key[-1],
                    "count": count,
                }
                for key, count in row["counts"].items()
            ],
        }
        for row in successful
    ]
    (tmp_path / "runtime-result-validation-counts.json").write_text(json.dumps(report, indent=2))
    assert all(count == 1 for row in successful for count in row["counts"].values()), report
