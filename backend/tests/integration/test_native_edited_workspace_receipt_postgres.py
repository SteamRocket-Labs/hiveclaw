"""Native text edits preserve human-saved versions and downloadable artifacts."""

import json

import pytest
from sqlalchemy import select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_native_edits_of_human_saved_file_remain_readable_and_downloadable(
    native_workspace,  # noqa: F811
    app_user_sessionmaker,
):
    import hashlib
    from app.database import pin_rls_tenant_context
    from app.models.chat_artifact import ChatArtifact
    from app.models.organization_identity import ContentInputReceipt
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
        _admitted_write_messages,
        _prepare_write,
    )

    n = native_workspace
    n.path = "workspace/operator-handoff.md"
    url = f"/agents/{n.agent}/files/content"
    original = "# Handoff\nEvidence: pending\nOwner: pending\nReview: pending\n"
    response = await n.client.put(url, params={"path": n.path}, json={"content": original})
    assert response.status_code == 200, response.text
    human = "Human acceptance owner. This document does not authorize deployment.\n\n" + original
    saved = await n.client.put(
        url,
        params={"path": n.path},
        json={"content": human, "expected_current_hash": response.json()["content_hash"]},
    )
    assert saved.status_code == 200, saved.text
    expected = human
    versions = [original, human]
    artifacts = []
    for round_index, (old_text, new_text) in enumerate(
        [
            ("Evidence: pending", "Evidence: ______"),
            ("Owner: pending", "Owner: ______"),
            ("Review: pending", "Review: ______"),
        ],
        start=1,
    ):
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            messages = await _admitted_write_messages(
                db,
                n,
                content="Revise the saved handoff and preserve the human owner statement.",
                round_index=round_index,
            )
            invocation, arguments, turn = await _prepare_write(
                db,
                n,
                messages=messages,
                tool_name="edit_file",
                round_index=round_index,
                arguments={"path": n.path, "old_text": old_text, "new_text": new_text},
            )
            trace = {}
            raw = await n.service.execute(
                "edit_file",
                arguments,
                agent_id=n.agent,
                user_id=n.user,
                session_id=str(n.session),
                runtime_task_id=str(n.run),
                turn_id=turn,
                tool_call_id=invocation.provider_tool_use_id,
                emit_runtime_hooks=False,
                trace_metadata_sink=trace,
            )
            expected = expected.replace(old_text, new_text)
            versions.append(expected)
            assert (n.root / n.path).read_text() == expected, str(raw)
            await _persist_tool_call(
                agent_id=n.agent,
                user_id=n.user,
                session_id=str(n.session),
                data={
                    "name": "edit_file",
                    "args": arguments,
                    "status": "done",
                    "result": str(raw),
                    "tool_call_id": invocation.provider_tool_use_id,
                    "provider_request_id": invocation.provider_request_id,
                    "runtime_task_id": str(n.run),
                    "tool_execution_evidence": _execution_evidence(trace),
                },
            )
            current = await n.client.get(url, params={"path": n.path})
            assert current.status_code == 200, current.text
            assert current.json()["content"] == expected
            assert current.json()["content_revision"] == round_index + 2
            rows = (
                await db.scalars(
                    select(ChatArtifact).where(
                        ChatArtifact.agent_id == n.agent,
                        ChatArtifact.path == n.path,
                    )
                )
            ).all()
            latest = [
                row
                for row in rows
                if row.snapshot_json["content_hash"] == hashlib.sha256(expected.encode()).hexdigest()
            ]
            assert len(latest) == 1
            artifacts.append((latest[0].id, expected))

    raw = await n.service.execute(
        "read_file",
        {"path": n.path},
        agent_id=n.agent,
        user_id=n.user,
        session_id=str(n.session),
        runtime_task_id=str(n.run),
        emit_runtime_hooks=False,
    )
    read = json.loads(str(raw))
    assert read["ok"] is True and read["content"].startswith(expected)
    for artifact_id, body in artifacts:
        downloaded = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact_id}/download")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == body.encode()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipts = (
            await db.scalars(
                select(ContentInputReceipt)
                .where(
                    ContentInputReceipt.tenant_id == n.tenant,
                    ContentInputReceipt.carrier_ref == f"{n.agent}\n{n.path}",
                )
                .order_by(ContentInputReceipt.revision)
            )
        ).all()
        assert [row.content_sha256 for row in receipts] == [
            hashlib.sha256(body.encode()).hexdigest() for body in versions
        ]
