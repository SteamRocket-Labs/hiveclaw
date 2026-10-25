"""A completed native write keeps its byte receipt when execution times out."""

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace

import pytest

from app.database import pin_rls_tenant_context
from app.models.invocation_span import InvocationSpan
from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _admitted_write_messages,
    _prepare_write,
    native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_timeout_after_native_overwrite_retains_verified_current_bytes(
    native_workspace,  # noqa: F811
    app_user_sessionmaker,
    monkeypatch,
):
    from app.services import content_input_receipts
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.web_chat_runtime import _persist_tool_call
    from app.tools import registry

    n = native_workspace
    original = content_input_receipts.persist_workspace_execution_evidence
    policy = registry.tool_execution_policy
    entered = asyncio.Event()

    async def delayed_settlement(**kwargs):
        if kwargs["tool_name"] == "write_file" and kwargs.get("descriptor_writes"):
            entered.set()
            await asyncio.sleep(6)
        return await original(**kwargs)

    monkeypatch.setattr(content_input_receipts, "persist_workspace_execution_evidence", delayed_settlement)
    monkeypatch.setattr(
        registry,
        "tool_execution_policy",
        lambda name: replace(policy(name), timeout_seconds=5) if name == "write_file" else policy(name),
    )
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        from app.core.resource_authority import workspace_resource_id
        from app.models.security_audit import ResourcePermission

        content_input_receipts.record_workspace_receipt(
            db, n.tenant, n.agent, n.path, hashlib.sha256(n.target.read_bytes()).hexdigest(), None
        )
        db.add(
            ResourcePermission(
                tenant_id=n.tenant,
                principal_type="user",
                principal_id=n.user,
                resource_type="workspace_file",
                resource_id=workspace_resource_id(n.agent, n.path),
                actions=["read", "write"],
                conditions={},
            )
        )
        await db.commit()
        invocation, arguments, turn = await _prepare_write(db, n, messages=await _admitted_write_messages(db, n))

        async def effect_fence(_boundary):
            await _persist_tool_call(
                agent_id=n.agent,
                user_id=n.user,
                session_id=str(n.session),
                data={
                    "name": "write_file",
                    "args": arguments,
                    "status": "effect_started",
                    "tool_call_id": invocation.provider_tool_use_id,
                    "provider_request_id": invocation.provider_request_id,
                    "runtime_task_id": str(n.run),
                },
            )

        trace = {}
        raw = str(
            await n.service.execute(
                "write_file",
                arguments,
                agent_id=n.agent,
                user_id=n.user,
                session_id=str(n.session),
                runtime_task_id=str(n.run),
                turn_id=turn,
                tool_call_id=invocation.provider_tool_use_id,
                emit_runtime_hooks=False,
                pre_effect_callback=effect_fence,
                trace_metadata_sink=trace,
            )
        )
        assert n.target.read_text() == arguments["content"], raw
        assert entered.is_set()
        assert json.loads(raw.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])["error_class"] == "timeout"
        assert json.loads(raw.split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])["retryable"] is False
        proof = await db.get(InvocationSpan, uuid.uuid5(invocation.id, "workspace-output-evidence"))
        assert proof is not None, "Completed descriptor output lost its provenance at the timeout boundary"
        evidence = proof.metadata_json["workspace_execution_evidence"]
        assert evidence["invocation_id"] == str(invocation.id)
        assert evidence["states"][n.path]["sha256"] == hashlib.sha256(arguments["content"].encode()).hexdigest()
        receipt = await db.get(ContentInputReceipt, uuid.uuid5(invocation.id, f"workspace-content:{n.path}"))
        assert receipt is not None and receipt.revision == 2
        assert receipt.content_sha256 == evidence["states"][n.path]["sha256"]
        envelopes = await _persist_tool_call(
            agent_id=n.agent,
            user_id=n.user,
            session_id=str(n.session),
            data={
                "name": "write_file",
                "args": arguments,
                "status": "failed",
                "result": raw,
                "tool_call_id": invocation.provider_tool_use_id,
                "provider_request_id": invocation.provider_request_id,
                "runtime_task_id": str(n.run),
                "tool_execution_evidence": _execution_evidence(trace),
            },
        )
        result = next(item["payload"] for item in envelopes if item["item_kind"] == "tool_result")
        assert result["outcome"] == "failed"
        assert result["retryable"] is False
        assert result["metadata"]["native_tool_timeout_receipt"]["status"] == "verified"
        from app.services.session_model_inputs import _origin_dependencies

        # The callback committed in its own session; consume the current row.
        await db.refresh(invocation)
        assert (
            await _origin_dependencies(
                db, tenant_id=n.tenant, agent_id=n.agent, ref=f"session-tool-result:{invocation.id}"
            )
        )[1]
        await db.refresh(invocation)
        assert invocation.effect_state == "failed"
    read = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.path})
    assert read.status_code == 200, read.text
    assert read.json()["content"] == arguments["content"]
