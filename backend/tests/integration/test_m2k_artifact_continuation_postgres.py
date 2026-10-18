"""Saved artifact continuation stays in its original work and reuses one input."""

import json
import os
import re
import shlex
import sys
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import delete, select

from app.database import pin_rls_tenant_context
from app.models.chat_artifact import ChatArtifact
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionTurnInput
from tests.integration.test_m2c_managed_environment_postgres import _exercise_real_worker_environment
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("producer,receipted", [("run_command", True), ("execute_code", True), ("run_command", False)])
async def test_artifact_editor_continues_saved_bytes_once_in_the_original_work(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, producer, receipted,
):
    async def continue_file(seed, client, url, scope, current):
        from app.api import files, chat_sessions
        from app.runtime import invoker
        from app.services.runtime_task_claim_service import RuntimeTaskClaimService
        from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
        from app.services.web_chat_runtime import execute_web_chat_run
        from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
        from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

        web = client._transport.app
        web.include_router(files.router, prefix="/api")
        web.include_router(chat_sessions.router, prefix="/api")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            deliveries = list(await db.scalars(select(ChatArtifact).where(
                ChatArtifact.runtime_task_id == seed["run_id"], ChatArtifact.path == "workspace/saved.txt")))
            assert len(deliveries) == 1
            original = deliveries[0]
            artifact_id = str(original.id)
            snapshot_hash = original.snapshot_hash
        if os.environ.get("HIVE_M2K_DOM_BRIDGE") == "1":
            from tests.integration.test_m1g90_company_bot_settings_postgres import _exercise_dom_bridge
            await _exercise_dom_bridge(web, seed, tmp_path, entry_file="e2e/artifact-continuation-native.tsx",
                extra_env={"HIVE_ARTIFACT_SESSION_ID": str(seed["session_id"]), "HIVE_ARTIFACT_ID": artifact_id})
            match = re.search(r'HIVE_ARTIFACT_INPUT_IDS=(\[[^\n]+\])', (tmp_path / "dom-native.log").read_text())
            assert match
            input_ids = json.loads(match.group(1))
            assert len(input_ids) == 2 and input_ids[0] == input_ids[1]
            input_id = UUID(input_ids[0])
        else:
            from uuid import uuid4
            input_id = uuid4()
            file_url = f"/api/agents/{seed['agent_id']}/files/content"
            saved = await client.get(file_url, params={"path": "workspace/saved.txt"})
            response = await client.put(file_url, params={"path": "workspace/saved.txt"}, json={
                "content": "CANONICAL_ENVIRONMENT_FILE_HUMAN_CORRECTION_决定性尾部",
                "expected_current_hash": saved.json()["content_hash"]})
            assert response.status_code == 200, response.text
            request = {"content": "Apply my saved correction in workspace/saved.txt", "input_id": str(input_id),
                       "idempotency_key": f"session-input:{input_id}", "permission_mode": "bypassPermissions"}
            run_url = f"/api/agents/{seed['agent_id']}/sessions/{seed['session_id']}/runs"
            for _ in range(2):
                response = await client.post(run_url, json=request)
                assert response.status_code == 200, response.text
        if not receipted:
            from app.models.organization_identity import ContentInputReceipt
            from app.services.content_input_receipts import workspace_artifact_ref

            async with owner_sessionmaker() as db:
                removed = await db.execute(delete(ContentInputReceipt).where(
                    ContentInputReceipt.tenant_id == seed["tenant_id"],
                    ContentInputReceipt.carrier_ref == workspace_artifact_ref(seed["agent_id"], "workspace/saved.txt")))
                assert removed.rowcount == 2
                await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert await db.get(SessionTurnInput, input_id) is not None
            rows = list(await db.scalars(select(RuntimeTask).where(
                RuntimeTask.parent_agent_id == seed["agent_id"], RuntimeTask.parent_session_id == str(seed["session_id"]),
                RuntimeTask.task_type == "web_chat_turn", RuntimeTask.id != seed["run_id"])))
            assert len(rows) == 1
            run_id = rows[0].id
            assert rows[0].root_user_id == seed["user_id"]
            claims = await RuntimeTaskClaimService(db=db, worker_id="artifact-follow-up", lease_seconds=600).claim_available()
            claim = next(row for row in claims if row.id == run_id)
            version = claim.claim_version
            await db.commit()
        code = "from pathlib import Path; p=Path('saved.txt'); data=p.read_text(); assert '_HUMAN_CORRECTION_决定性尾部' in data; print(data); p.write_text(data+'_ARTIFACT_FOLLOWED')"
        command = f"{shlex.quote(str(Path(sys.executable).resolve()))} -I -c {shlex.quote(code)}"
        script = ScriptedLLMClient([
            {"tool_calls": [{"id": "artifact-follow-up-command", "type": "function", "function": {
                "name": producer, "arguments": json.dumps({"command": command, "timeout": 10} if producer == "run_command"
                    else {"language": "python", "code": code, "timeout": 10})}}], "finish_reason": "tool_calls"},
            {"content": "Saved correction continued.\nDELIVERABLE: workspace/saved.txt", "finish_reason": "stop"},
        ])
        monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: script)
        fence = set_runtime_task_fence(task_id=run_id, worker_id="artifact-follow-up", claim_version=version)
        try:
            await execute_web_chat_run(run_id)
        finally:
            reset_runtime_task_fence(fence)
        processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"])
        boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
            tenant_id=seed["tenant_id"], worker_id="artifact-follow-up-boundary", canonical_validator=processor.validate,
            process_callback=processor, task_types=("web_chat_turn",))
        assert boundary["delivered"] == 1, boundary
        assert "_HUMAN_CORRECTION_决定性尾部" in json.dumps(script.requests[-1]["messages"], ensure_ascii=False)
        assert (seed["agent_root"] / "workspace/saved.txt").read_text().endswith("_ARTIFACT_FOLLOWED")
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            assert (await db.get(RuntimeTask, run_id)).status == "completed"
            assert (await db.get(ChatArtifact, UUID(artifact_id))).snapshot_hash == snapshot_hash
            continued = list(await db.scalars(select(ChatArtifact).where(
                ChatArtifact.runtime_task_id == run_id, ChatArtifact.path == "workspace/saved.txt")))
            if receipted:
                assert len(continued) == 1
                assert continued[0].snapshot_hash != snapshot_hash
                assert continued[0].snapshot_json["content_input_receipt_id"]
                delivered = await client.get(
                    f"/api/agents/{seed['agent_id']}/files/artifacts/{continued[0].id}/content")
                assert delivered.status_code == 200, delivered.text
                assert delivered.json()["content"] == (seed["agent_root"] / "workspace/saved.txt").read_text()
            else:
                from app.models.invocation_span import InvocationSpan

                assert not continued
                proof = await db.scalar(select(InvocationSpan).where(
                    InvocationSpan.runtime_task_id == run_id, InvocationSpan.span_type == "workspace_output"))
                assert proof.metadata_json["workspace_execution_evidence"]["execution_inputs"]["unbound"] == ["workspace/saved.txt"]
                assert proof.metadata_json["native_content_receipts"] == {
                    "status": "unavailable", "reason_code": "workspace_execution_inputs_unverified", "receipts": {}}
        (tmp_path / "artifact-continuation-observation.json").write_text(json.dumps({
            "input_id": str(input_id), "run_id": str(run_id), "session_id": str(seed["session_id"]),
            "original_artifact_id": artifact_id, "original_snapshot_hash": snapshot_hash,
            "mounted_dom": os.environ.get("HIVE_M2K_DOM_BRIDGE") == "1"}, indent=2))
        return (await client.get(url, params=scope)).json()["environment"]

    await _exercise_real_worker_environment(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        recovery=continue_file, initial_delivery=True, recovery_invocation_key="artifact-follow-up-command")
