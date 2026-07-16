from __future__ import annotations

import json
from uuid import uuid4


def test_runtime_result_payload_is_deterministic_lossless_and_hash_pinned():
    from app.services.runtime_result_store import (
        decode_runtime_result_payload,
        encode_runtime_result_payload,
        runtime_result_ref,
    )

    summary = "尾部决定性证据:" + ("结果🙂" * 180_000)
    artifacts = [
        {
            "type": "artifact",
            "artifact_id": str(uuid4()),
            "path": "workspace/final-report.md",
            "content_sha256": "a" * 64,
        }
    ]
    metadata = {
        "model_context": {"verdict": "use the complete tail", "tail": "decisive-tail"},
        "nested": {"items": [3, 2, 1]},
    }

    first = encode_runtime_result_payload(summary=summary, artifacts=artifacts, metadata=metadata)
    second = encode_runtime_result_payload(summary=summary, artifacts=artifacts, metadata=metadata)

    assert first.payload_bytes == second.payload_bytes
    assert first.sha256 == second.sha256
    assert first.size_bytes == len(first.payload_bytes)
    assert decode_runtime_result_payload(first.payload_bytes) == {
        "artifacts": artifacts,
        "metadata": metadata,
        "schema": "hive.runtime_result.v1",
        "summary": summary,
    }
    result_id = uuid4()
    assert runtime_result_ref(result_id=result_id, sha256=first.sha256) == (
        f"runtime-result://{result_id}/{first.sha256}"
    )


def test_100_way_large_results_partition_into_lossless_ref_only_pages():
    from app.services.runtime_result_store import (
        RuntimeResultDescriptor,
        build_runtime_result_integration_pages,
    )

    raw_marker = "RAW-RESULT-BYTES-MUST-NOT-ENTER-PARENT"
    descriptors = [
        RuntimeResultDescriptor(
            outbox_id=uuid4(),
            mailbox_sequence=index + 1,
            source_kind="subagent" if index % 2 == 0 else "workflow",
            source_run_id=str(uuid4()),
            task_type="subagent" if index % 2 == 0 else "workflow",
            terminal_status=("completed" if index < 73 else "failed" if index < 91 else "cancelled"),
            child_session_id=uuid4(),
            child_agent_name=f"worker-{index}",
            result_ref=f"runtime-result://{uuid4()}/{'b' * 64}",
            result_sha256="b" * 64,
            result_size_bytes=1_048_576,
            artifact_count=1,
        )
        for index in range(100)
    ]

    pages = build_runtime_result_integration_pages(
        descriptors,
        page_item_limit=25,
        starting_epoch=9,
        root_runtime_task_id=uuid4(),
        coverage={
            "requested": 100,
            "admitted": 100,
            "deferred": 0,
            "not_admitted": 0,
            "expected": 100,
            "terminal": 100,
            "running": 0,
            "waiting_approval": 0,
            "conserved": True,
        },
    )

    assert [page.integration_epoch for page in pages] == [9, 10, 11, 12]
    assert [len(page.items) for page in pages] == [25, 25, 25, 25]
    flattened = [item.outbox_id for page in pages for item in page.items]
    assert flattened == [item.outbox_id for item in descriptors]
    assert len(set(flattened)) == 100
    assert all(page.coverage["terminal"] == 100 for page in pages)
    assert all(page.complete_page for page in pages)

    rendered = json.dumps([page.to_manifest() for page in pages], ensure_ascii=False)
    assert raw_marker not in rendered
    assert "summary" not in rendered
    assert "model_context" not in rendered
    assert len(rendered.encode("utf-8")) < 120_000


def test_result_integration_runtime_context_contains_refs_not_child_bytes():
    from app.services.agent_session_continuation import build_result_integration_runtime_context
    from app.services.runtime_result_store import RuntimeResultDescriptor, RuntimeResultIntegrationPage

    descriptor = RuntimeResultDescriptor(
        outbox_id=uuid4(),
        mailbox_sequence=41,
        source_kind="a2a_delegation",
        source_run_id=str(uuid4()),
        task_type="a2a_delegation",
        terminal_status="completed",
        child_session_id=uuid4(),
        child_agent_name="Researcher",
        result_ref=f"runtime-result://{uuid4()}/{'c' * 64}",
        result_sha256="c" * 64,
        result_size_bytes=1_048_576,
        artifact_count=2,
    )
    page = RuntimeResultIntegrationPage(
        integration_epoch=7,
        root_runtime_task_id=uuid4(),
        items=(descriptor,),
        coverage={"expected": 2, "terminal": 1, "running": 1, "conserved": True},
        complete_page=True,
    )

    result = build_result_integration_runtime_context(page.to_manifest())

    assert descriptor.result_ref in result
    assert "read_runtime_result" in result
    assert "terminal=1/expected=2" in result
    assert "Researcher" in result
    assert "Summary:" not in result
    assert len(result.encode("utf-8")) < 8_000
