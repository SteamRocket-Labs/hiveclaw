"""Permission aliases are private; ordinary tool documents remain byte-faithful."""

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_session_permission_execution_once import _mounted_client

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_requestless_permission_aliases_and_untyped_document_at_real_exits(
    native_workspace, owner_sessionmaker,  # noqa: F811
):
    from app.models.audit import ChatMessage
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.security_audit import ResourcePermission
    from app.models.session_v2 import SessionEventOutbox
    from app.services.chat_message_parts import serialize_chat_message
    from app.services.chat_transcript import append_session_event
    from app.services.session_event_contract import serialize_session_event
    from app.services.web_chat_runtime import _persist_runtime_event

    n = native_workspace
    event = {"type": "permission", "tool_name": "plaza_create_post", "status": "governance_hook_denied",
             "message": "Internal hook teaching", "reason": "PII export; ticket SEC-118", "hook_key": "export-guard",
             "credential_family": "mail", "security_zone": "public", "capability": "private-capability"}
    member_event = await _persist_runtime_event(
        agent_id=n.agent, user_id=n.user, session_id=str(n.session), data=event,
    )
    child = {"type": "child_session", "status": "completed", "message": "同事的结果已返回。", "reason": "delegation_completed"}
    await _persist_runtime_event(agent_id=n.agent, user_id=n.user, session_id=str(n.session), data=child)
    seed = SimpleNamespace(tenant_id=n.tenant, user_id=n.user, agent_id=n.agent,
                           session_id=n.session, run_id=n.run)
    async with owner_sessionmaker() as db:
        original = await db.get(ChatTranscriptEvent, uuid.UUID(member_event["event_id"]))
        operator_event = serialize_session_event(original)
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.foreign,
            resource_type="agent", resource_id=n.agent, actions=["operator.inspect"],
            conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
        await db.commit()
        async with _mounted_client(n.app_user_sessionmaker, seed) as client:
            path = f"/api/agents/{n.agent}/sessions/{n.session}"
            for params in ({}, {"operator_view": "true"}):
                response = await client.get(f"{path}/messages", params=params)
                assert response.status_code == 200, response.text
                permission = next(row for row in response.json() if row.get("eventType") == "permission")
                assert permission["content"] == permission["parts"][0]["text"] == "当前没有权限完成这次操作。"
                # Scope reason to the permission copies: the compatibility
                # envelope has its own unrelated adaptation-reason contract.
                payload = member_event["payload"]
                for frame in (permission, permission["parts"][0], payload["metadata"],
                              json.loads(payload["content"]), payload["parts"][0]):
                    assert not {"reason", "hook_key", "credential_family", "security_zone",
                                "eventReason", "eventSecurityZone"} & frame.keys()
                assert event["reason"] not in json.dumps(member_event)
                child_row = next(row for row in response.json() if row.get("eventType") == "child_session")
                assert child_row["parts"][0]["reason"] == "delegation_completed"
            inspector = SimpleNamespace(**{**vars(seed), "user_id": n.foreign})
            async with _mounted_client(n.app_user_sessionmaker, inspector) as operator:
                response = await operator.get(f"{path}/messages", params={
                    "operator_view": "true", "operator_reason": "Inspect original permission context",
                })
                assert response.status_code == 200, response.text
            stored = list(await db.scalars(select(ChatMessage).where(
                ChatMessage.conversation_id == str(n.session)).order_by(ChatMessage.created_at)))
            assert response.json() == [serialize_chat_message(message) for message in stored]
            permission = next(row for row in response.json() if row.get("eventType") == "permission")
            assert permission["eventReason"] == event["reason"]
            assert permission["eventSecurityZone"] == event["security_zone"]
            assert serialize_session_event(original) == operator_event
            assert json.loads(original.content) == event

            # A compatibility write must not treat an ordinary tool's JSON
            # document as a runtime permission frame (B1/B2).
            body = ' { "type": "permission", "status": {"code": 403}, "message": "API document" } \n'
            row = await append_session_event(
                db=db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, user_id=n.user,
                actor_type="tool", event_type="tool_result", role="tool_call", t0_role="tool",
                content=json.dumps({"name": "ordinary_tool", "status": "done", "result": body}),
                metadata={"tool_name": "ordinary_tool", "status": "done"}, source="task_executor",
            )
            await db.commit()
            outbox = await db.scalar(select(SessionEventOutbox).where(SessionEventOutbox.event_id == row.transcript_event.id))
            assert outbox is not None
            assert json.loads(outbox.envelope_json["payload"]["content"])["result"] == body
            response = await client.get(f"{path}/messages")
            assert response.status_code == 200, response.text
            document = next(message for message in response.json() if message["id"] == str(row.chat_message.id))
            assert document["toolResult"] == document["parts"][0]["result"] == body
