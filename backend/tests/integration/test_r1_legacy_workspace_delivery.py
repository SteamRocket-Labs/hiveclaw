"""Legacy workspace lineage must not contaminate an unrelated report turn."""

import json
import uuid

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from app.models.session_v2 import SessionModelResult
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, new_owner_work

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def legacy_file(n, app_user_sessionmaker, owner_sessionmaker):
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.services.learned_skill_sources import model_source, validate_model_source
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.session_tool_runtime import _sha256

    path = "workspace/legacy-analysis.md"
    result, invocation = await memory_call(n, app_user_sessionmaker, "write_file",
        {"path": path, "content": "LEGACY_UNPROVABLE_BODY"}, command="Write my earlier analysis")
    assert (n.root / path).read_text() == "LEGACY_UNPROVABLE_BODY", result
    async with owner_sessionmaker() as db:
        model = await db.scalar(select(SessionModelResult).where(
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        snapshot = dict(model.model_request_snapshot_json)
        bindings = dict(snapshot["input_bindings"])
        bindings["dependency_coverage"] = {**bindings["dependency_coverage"], "complete": False}
        snapshot["input_bindings"] = bindings
        model.model_request_snapshot_json = snapshot
        model.model_request_hash = _sha256(snapshot)
        source = model_source(model)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        receipt.dependencies_json = [source]
        receipt_id = receipt.id
        await db.commit()
        # Match the lab: a sealed, committed model source whose sole defect is coverage.
        assert model.state == "round_committed"
        with pytest.raises(ContentInputReceiptError, match="Restore the exact sealed workflow"):
            await validate_model_source(db, n.tenant, source, user=await db.get(User, n.user))
    return path, receipt_id


async def artifact_for(n, app_user_sessionmaker, run, path):
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        rows = list(await db.scalars(select(ChatArtifact).where(
            ChatArtifact.runtime_task_id == run, ChatArtifact.path == path)))
        assert len(rows) == 1, [(row.path, str(row.id)) for row in rows]
        return rows[0]


async def download(n, artifact, expected):
    response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/download")
    assert response.status_code == 200, response.text
    assert response.content == expected == (n.root / artifact.path).read_bytes()


async def test_legacy_file_does_not_block_csv_report_or_next_edit(
    native_workspace, app_user_sessionmaker, owner_sessionmaker,  # noqa: F811
):
    from app.services.content_input_receipts import latest_workspace_receipt
    from app.models.invocation_span import InvocationSpan

    n = native_workspace
    from app.models.capability_policy import CapabilityPolicy

    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.code.execute",
            allowed=True, requires_approval=False))
        await db.commit()
    legacy_path, legacy_receipt = await legacy_file(n, app_user_sessionmaker, owner_sessionmaker)
    from app.api import upload as chat_upload

    n.monkeypatch.setattr(chat_upload, "WORKSPACE_ROOT", n.root.parent)
    n.client._transport.app.include_router(chat_upload.router)
    upload = await n.client.post("/chat/upload", data={"agent_id": str(n.agent), "skip_personal_kb": "true"},
        files={"file": ("sales.csv", b"product,sales\nAlpha,12\nBeta,30\n", "text/csv")})
    assert upload.status_code == 200, upload.text
    csv_path = upload.json()["workspace_path"]
    assert (n.root / csv_path).is_file(), upload.text
    session, run = await new_owner_work(n, owner_sessionmaker)
    output, _ = await memory_call(n, app_user_sessionmaker, "read_file", {"path": csv_path},
        session=session, run=run, command="Read the uploaded CSV", include_history=True)
    assert "Alpha,12" in output, output
    output, computation = await memory_call(n, app_user_sessionmaker, "execute_code", {
        "language": "python", "code": "import csv\nprint(sum(int(r['sales']) for r in csv.DictReader(open(" + repr(csv_path.removeprefix("workspace/")) + "))))"},
        session=session, run=run, command="Compute total sales from the uploaded CSV", include_history=True)
    assert any(line.strip() == "42" for line in output.splitlines()), output
    calculation_output = output
    path, body = "workspace/sales-analysis.md", "# Sales report\nTotal sales: 42\n"
    output, _ = await memory_call(n, app_user_sessionmaker, "write_file", {"path": path, "content": body},
        session=session, run=run, command=f"Write and deliver {path}", include_history=True)
    card = await artifact_for(n, app_user_sessionmaker, run, path)
    await download(n, card, body.encode())
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, path)
        assert str(legacy_receipt) not in json.dumps(receipt.dependencies_json)
        evidence = await db.get(InvocationSpan, uuid.uuid5(computation.id, "workspace-output-evidence"))
        inputs = evidence.metadata_json["workspace_execution_evidence"]["execution_inputs"]
        assert inputs["observed"] and not inputs["unbound"]
        assert str(legacy_receipt) not in json.dumps(inputs)
    assert legacy_path in calculation_output and "omitted" in calculation_output, calculation_output
    # A new run in the same conversation edits the delivered version.
    from app.models.runtime_task import RuntimeTask

    edit_run = uuid.uuid4()
    async with owner_sessionmaker() as db:
        (await db.get(RuntimeTask, run)).status = "completed"
        db.add(RuntimeTask(id=edit_run, tenant_id=n.tenant, task_type="web_chat_turn", status="running",
            parent_agent_id=n.agent, child_agent_id=n.agent, parent_session_id=str(session),
            child_session_id=str(session), root_user_id=n.user, root_session_id=str(session),
            root_runtime_task_id=edit_run, prompt="Edit my report", metadata_json={"turn_id": f"turn-{edit_run.hex}"}))
        await db.commit()
    output, _ = await memory_call(n, app_user_sessionmaker, "read_file", {"path": path},
        session=session, run=edit_run, command="Read my current report", include_history=True)
    assert body in json.loads(output)["content"], output
    revised = body + "\n## Next steps\nReview Friday.\n"
    await memory_call(n, app_user_sessionmaker, "edit_file", {"path": path, "old_text": body, "new_text": revised},
        session=session, run=edit_run, command=f"Edit and deliver {path}", include_history=True)
    edited = await artifact_for(n, app_user_sessionmaker, edit_run, path)
    assert edited.id != card.id
    await download(n, edited, revised.encode())
    assert (n.root / legacy_path).read_bytes() == b"LEGACY_UNPROVABLE_BODY"
    # Revoking the CSV's source access still invalidates both captured versions.
    from app.models.workspace_resource import WorkspaceResourceManifest

    async with owner_sessionmaker() as db:
        source = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == csv_path))
        source.owner_user_id = n.foreign
        await db.commit()
    for artifact in (card, edited):
        denied = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact.id}/download")
        assert denied.status_code == 403 and "Total sales" not in denied.text, denied.text
