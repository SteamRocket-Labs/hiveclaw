"""X1-r2 ruling 5: creation-result bytes verify through the real reader.

The receipt's verifiable bytes must not depend on a mutable name: a result
stored with the base bytes still verifies, a flipped byte fails, and renaming
the employee after the result is stored still verifies. Everything goes
through `_load_creation_result_sources`, not only the receipt builder.
"""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.hr_creation import HrCreationDraft
from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.tenant import Tenant
from app.models.user import User

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _hash(value) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _seed_creation(db_sessionmaker, *, name="周报助手", first_task="介绍一下你自己",
                         first_status="dispatched", draft_status="completed", task_status="completed",
                         failure_code=None):
    """Craft one complete, hash-pinned creation chain and store its receipt."""
    from app.services.config_versioning import canonical_content_hash  # noqa: F401  (parity import)
    from app.services.hr_provisioning_runtime import build_hr_provisioning_runtime_task
    from app.services.employee_first_task import creation_receipt_notification
    from app.services.runtime_notification_outbox import enqueue_completion_notification

    tenant_id, user_id, agent_id, session_id = (uuid.uuid4() for _ in range(4))
    run_id, request_id, call_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tool_arguments = {
        "name": name, "role_description": "整理周报",
        "core_outputs": ["周报"], "boundaries": "仅整理", "first_task": first_task,
    }
    async with db_sessionmaker() as db:
        db.add(Tenant(id=tenant_id, name=f"X1 验证 {name}", slug=f"x1-verify-{uuid.uuid4().hex[:8]}"))
        await db.flush()
        db.add(User(id=user_id, tenant_id=tenant_id, username=f"x1v{uuid.uuid4().hex[:6]}",
            email=f"{uuid.uuid4().hex[:8]}@x1.test", password_hash="x", display_name="验收成员"))
        db.add(Agent(id=agent_id, tenant_id=tenant_id, name="HR", creator_id=user_id, owner_user_id=user_id))
        await db.flush()
        db.add(ChatSession(id=session_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id))
        await db.flush()
        from app.models.runtime_task import RuntimeTask
        turn_id = f"turn-{run_id.hex}"
        db.add(RuntimeTask(
            id=run_id, tenant_id=tenant_id, task_type="web_chat_turn",
            parent_agent_id=agent_id, parent_session_id=str(session_id),
            root_session_id=str(session_id), root_user_id=user_id,
            status="completed",
            metadata_json={"turn_id": turn_id},
        ))
        await db.flush()

        response = {
            "id": str(request_id),
            "tool_calls": [{"id": str(call_id), "type": "function",
                "function": {"name": "create_employee_from_request",
                    "arguments": json.dumps(tool_arguments, ensure_ascii=False)}}],
        }
        snapshot = {"input_bindings": {"schema": "hive.model_input_bindings.v1",
            "dependencies": [], "dependency_coverage": {"complete": True}, "messages": []}}
        db.add(SessionModelResult(
            id=uuid.uuid4(),
            tenant_id=tenant_id, session_id=session_id, run_id=run_id,
            turn_id=turn_id, round_id="round-1",
            provider_request_id=str(request_id),
            state="sealed",
            model_request_snapshot_json=snapshot, model_request_hash=_hash(snapshot),
            seal_json={"response": response,
                "provider_response_ref": f"provider-response:{request_id}:{_hash(response)}"},
        ))
        db.add(SessionToolInvocation(
            id=uuid.uuid4(),
            tenant_id=tenant_id, session_id=session_id, run_id=run_id,
            round_id="round-1",
            provider_request_id=str(request_id), provider_tool_use_id=str(call_id),
            tool_name="create_employee_from_request",
            provider_arguments_json=tool_arguments,
            args_hash=_hash({"tool_name": "create_employee_from_request", "arguments": tool_arguments}),
            invocation_item_id=uuid.uuid4(),
            authority_snapshot_hash=_hash({"authority": "x1-verify"}),
            effect_idempotency_key=f"x1-verify:{call_id}",
            effect_state="effect_started",
        ))
        await db.flush()

        invocation = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.tenant_id == tenant_id,
            SessionToolInvocation.provider_tool_use_id == str(call_id)))).one()
        model = (await db.scalars(select(SessionModelResult).where(
            SessionModelResult.tenant_id == tenant_id,
            SessionModelResult.provider_request_id == str(request_id)))).one()

        proof = {
            "schema": "hive.employee_creation_origin.v1",
            "draft_id": None,  # filled once the draft id exists
            "parent_invocation_id": str(invocation.id),
            "parent_model_result_id": str(model.id),
            "parent_model_request_hash": model.model_request_hash,
            "content": first_task,
            "requester_user_id": str(user_id),
            "call_id": str(call_id),
        }
        from app.services.hr_creation_service import canonical_hr_blueprint_hash as _bp_hash
        draft = HrCreationDraft(
            tenant_id=tenant_id, hr_agent_id=agent_id, session_id=session_id,
            requested_by_user_id=user_id, status="confirmed",
            blueprint_json=dict(tool_arguments),
            blueprint_hash=_bp_hash(dict(tool_arguments)),
            blueprint_version=1,
            preview_json={},
            provisioning_json={"first_task": {"status": first_status}},
        )
        db.add(draft)
        await db.flush()
        proof["draft_id"] = str(draft.id)
        digest = _hash(proof)
        source_id = uuid.uuid5(invocation.id, f"delegation-instruction:{digest}")
        db.add(InvocationSpan(
            id=source_id, tenant_id=tenant_id, agent_id=agent_id, user_id=user_id,
            session_id=str(session_id), runtime_task_id=run_id,
            trace_id=f"session-tool:{invocation.id}", span_id=f"delegation-instruction:{source_id}",
            span_type="delegation_input", name="model_authored_instruction", status="captured",
            duration_ms=0, started_at=datetime.now(timezone.utc),
            metadata_json={"instruction_origin": proof},
        ))
        draft.preview_json = {
            "creation_authorization": {
                "schema": "hive.employee_creation_authorization.v1",
                "mode": "policy_direct", "tenant_id": str(tenant_id),
                "requester_user_id": str(user_id), "source_agent_id": str(agent_id),
                "source_session_id": str(session_id), "blueprint_hash": draft.blueprint_hash,
                "package": "private_native_employee.v1",
                "creation_origin": {"source_id": str(source_id), "source_hash": digest},
                "source_run_id": str(run_id), "input_id": str(uuid.uuid4()),
                "approved_model_id": str(uuid.uuid4()),
                "tool_arguments": tool_arguments, "first_task": first_task,
            },
        }
        draft.confirmed_by_user_id = user_id
        draft.confirmed_at = datetime.now(timezone.utc)
        await db.flush()

        task = build_hr_provisioning_runtime_task(draft)
        db.add(task)
        await db.flush()
        draft.provisioning_task_id = task.id
        draft.status = draft_status
        task.status = task_status
        task.result_summary = task_status
        draft.created_agent_id = agent_id
        if failure_code:
            draft.failure_code = failure_code
        await db.flush()
        await db.commit()
        return {"tenant_id": tenant_id, "agent_id": agent_id, "draft_id": draft.id, "task_id": task.id}


async def _stored_result(db_sessionmaker, seed):
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject

    async with db_sessionmaker() as db:
        ref = (await db.scalars(select(RuntimeNotificationOutbox.result_ref).where(
            RuntimeNotificationOutbox.tenant_id == seed["tenant_id"],
            RuntimeNotificationOutbox.source_kind == "employee_creation"))).one()
        result_id, sha = ref[len("runtime-result://"):].split("/")
        return uuid.UUID(result_id), sha


async def _verify(db_sessionmaker, result_id):
    from app.models.runtime_result import RuntimeResultObject
    from app.services.runtime_result_sources import _load_creation_result_sources

    async with db_sessionmaker() as db:
        result = await db.get(RuntimeResultObject, result_id)
        return await _load_creation_result_sources(db, result)


async def _enqueue(db_sessionmaker, seed, legacy):
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.services.employee_first_task import creation_receipt_notification
    from app.services.runtime_notification_outbox import enqueue_completion_notification

    async with db_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, seed["draft_id"], populate_existing=True)
        task = await db.get(RuntimeTask, seed["task_id"], populate_existing=True)
        notification = await creation_receipt_notification(db, draft, task, legacy_summary=legacy)
        assert notification is not None
        await enqueue_completion_notification(db, notification)
        await db.commit()


async def test_result_stored_with_base_bytes_still_verifies_complete(owner_sessionmaker):
    seed = await _seed_creation(owner_sessionmaker)
    # Base bytes: enqueue the legacy-format notification for the same chain.
    await _enqueue(owner_sessionmaker, seed, legacy=True)
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    verified = await _verify(owner_sessionmaker, result_id)
    assert verified is not False and verified is not None
    task, dependencies, unbound = verified
    assert task.id == seed["task_id"] and unbound == set()


async def test_a_flipped_byte_fails_verification(owner_sessionmaker):
    seed = await _seed_creation(owner_sessionmaker)
    await _enqueue(owner_sessionmaker, seed, legacy=False)
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    from app.models.runtime_result import RuntimeResultObject

    async with owner_sessionmaker() as db:
        result = await db.get(RuntimeResultObject, result_id)
        original = bytearray(result.payload_bytes)
        original[5] = original[5] ^ 0x01
        result.payload_bytes = bytes(original)
        await db.commit()
    assert await _verify(owner_sessionmaker, result_id) is False


async def test_renaming_the_employee_after_storage_still_verifies(owner_sessionmaker):
    seed = await _seed_creation(owner_sessionmaker)
    await _enqueue(owner_sessionmaker, seed, legacy=False)
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    assert await _verify(owner_sessionmaker, result_id) is not False
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.name = "改名后的员工"
        await db.commit()
    verified = await _verify(owner_sessionmaker, result_id)
    assert verified is not False and verified is not None


async def _rebuild_notification(db_sessionmaker, seed):
    from app.models.hr_creation import HrCreationDraft
    from app.models.runtime_task import RuntimeTask
    from app.services.employee_first_task import creation_receipt_notification

    async with db_sessionmaker() as db:
        draft = await db.get(HrCreationDraft, seed["draft_id"], populate_existing=True)
        task = await db.get(RuntimeTask, seed["task_id"], populate_existing=True)
        return await creation_receipt_notification(db, draft, task)


async def test_failed_draft_with_created_agent_row_never_says_ready(owner_sessionmaker):
    """X1-r3 ruling 2: a provisioning failure after the employee row exists
    must not tell the model the employee "is ready" with a link."""
    from app.services.runtime_notification_outbox import enqueue_completion_notification

    seed = await _seed_creation(owner_sessionmaker, draft_status="failed", task_status="failed",
                                failure_code="workspace_init_failed")
    notification = await _rebuild_notification(owner_sessionmaker, seed)
    assert notification is not None
    assert "is ready" not in notification.summary
    assert "The employee record exists but setup did not finish." in notification.summary
    # No link before the internal tail (the /agents/<id> rule sentence stays).
    assert f"/agents/{seed['agent_id']}" not in notification.summary.split("Internal: employee_id=", 1)[0]
    async with owner_sessionmaker() as db:
        await enqueue_completion_notification(db, notification)
        await db.commit()
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    # A failed creation never verifies as a completed one.
    assert await _verify(owner_sessionmaker, result_id) is False


@pytest.mark.parametrize("first_status", ["blocked", "unknown"])
async def test_blocked_or_unknown_first_task_survives_a_rename(owner_sessionmaker, first_status):
    """X1-r3 ruling 3: the recovery request's bytes read the hash-pinned
    blueprint name, so a rename after the receipt neither breaks verification
    nor silently authorizes a replay."""
    seed = await _seed_creation(owner_sessionmaker, first_status=first_status)
    await _enqueue(owner_sessionmaker, seed, legacy=False)
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    assert await _verify(owner_sessionmaker, result_id) is not False
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.name = "改名后的员工"
        await db.commit()
    # Verification still passes: head and recovery bytes use the blueprint name.
    verified = await _verify(owner_sessionmaker, result_id)
    assert verified is not False and verified is not None
    # Recovery is refused, as before: the recorded request replays the
    # blueprint name, which no longer equals the renamed row, so the resume
    # check at employee_first_task (arguments.agent_name == target.name)
    # refuses with first_task_arguments_mismatch.
    notification = await _rebuild_notification(owner_sessionmaker, seed)
    request = json.loads(notification.summary.split("Recorded first-task recovery request: ", 1)[1])
    assert request["arguments"]["agent_name"] == "周报助手"
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        assert request["arguments"]["agent_name"] != agent.name


async def test_blueprint_name_with_quote_and_newline_cannot_split_the_head(owner_sessionmaker):
    """X1-r3 ruling 10 note 3: the head quotes the collapsed name as one JSON
    string, so a quote or newline cannot forge a second sentence or an
    ``Internal:`` line."""
    seed = await _seed_creation(owner_sessionmaker, name='周报"助手\n小心')
    await _enqueue(owner_sessionmaker, seed, legacy=False)
    result_id, _sha = await _stored_result(owner_sessionmaker, seed)
    verified = await _verify(owner_sessionmaker, result_id)
    assert verified is not False and verified is not None
    notification = await _rebuild_notification(owner_sessionmaker, seed)
    # The collapsed name stays one JSON string, and the quote/newline cannot
    # forge a second Internal line before the real one.
    assert json.dumps('周报"助手 小心', ensure_ascii=False) in notification.summary
    assert notification.summary.count("Internal:") == 1
