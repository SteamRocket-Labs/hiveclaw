"""The parent's task reader must not bypass a refused colleague source."""

import json
from uuid import uuid5

import pytest
from sqlalchemy import select

from app.models.invocation_span import InvocationSpan
from app.models.session_v2 import SessionModelResult, SessionToolInvocation
from app.models.user_assistant_binding import UserAssistantBinding
from tests.integration import test_web_chat_worker_restart_kernel_recovery as kernel
from tests.integration.test_failed_delegation_continuation import _call, _execute
from tests.integration.test_r19a_consumed_result import auxiliary_models  # noqa: F401
from tests.integration.test_r19b_delegation_result_postgres import _tool_results
from tests.integration.test_r22a_owned_delegation_result_postgres import (
    CSV,
    RESULT,
    _deliver_continuation,
    _file_result,
    _revoke_source,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url", "auxiliary_models")


@pytest.mark.parametrize("revoked", [False, True], ids=["granted", "revoked"])
async def test_parent_task_read_respects_the_same_member_source_as_runtime_result(
    owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path, revoked,
):
    import app.runtime.invoker as invoker

    seed_run = kernel._seed_run

    async def seed_assistant(*args, **kwargs):
        seed = await seed_run(*args, **kwargs)
        async with owner_sessionmaker() as db:
            db.add(UserAssistantBinding(
                tenant_id=seed["tenant_id"], user_id=seed["user_id"], agent_id=seed["agent_id"],
                source="explicit_selection", status="active", owner_user_id_at_bind=seed["user_id"],
                requester_user_id=seed["user_id"],
            ))
            await db.commit()
        return seed

    monkeypatch.setattr(kernel, "_seed_run", seed_assistant)

    async def revoke_after_colleague_used_the_source(seed):
        await _revoke_source(owner_sessionmaker, seed)

    # The existing real upload/file/delegation fixture grants the member a
    # third person's document, pins that source on the uploaded file, and has
    # the colleague read it before completing. Only then can access be revoked.
    seed = await _file_result(
        owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,
        source_grant=True,
        before_handback=revoke_after_colleague_used_the_source if revoked else None,
    )
    return_id = await _deliver_continuation(owner_sessionmaker, app_user_sessionmaker, seed)
    client = kernel.ScriptedLLMClient([
        {"tool_calls": [_call("read_runtime_result", {"result_ref": seed["result_ref"]}, "r27a-pinned-read")],
            "finish_reason": "tool_calls"},
        {"tool_calls": [_call("check_async_task", {"task_id": str(seed["child_id"])}, "r27a-task-read")],
            "finish_reason": "tool_calls"},
        {"content": "The colleague task has ended."},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: client)
    await _execute(owner_sessionmaker, return_id, "r27a-source-reader")
    assert len(client.requests) == 3
    assert seed["result_ref"] in json.dumps(client.requests[0], ensure_ascii=False)

    results = await _tool_results(owner_sessionmaker, return_id)
    pinned = json.loads(results["r27a-pinned-read"]["content"])
    checked = json.loads(results["r27a-task-read"]["content"])
    if revoked:
        assert (pinned["status"], pinned["reason_code"], pinned["retryable"]) == (
            "source_access_denied", "content_source_access_denied", False,
        )
        assert RESULT not in json.dumps(pinned) and "Gamma,150" not in json.dumps(pinned)
    else:
        assert pinned["status"] == "ok" and pinned["complete"] is True
        assert RESULT in pinned["content"]
    assert checked["status"] == "completed"

    wires = [json.dumps(request["messages"], ensure_ascii=False) for request in client.requests]
    # A child_session timeline item may contain the answer, but the automatic
    # continuation must initially expose only the result reference to the model.
    assert RESULT not in wires[0] and CSV not in wires[0]
    if revoked:
        assert RESULT not in wires[1] and "Gamma,150" not in wires[1]
    else:
        assert RESULT in wires[1]

    async with owner_sessionmaker() as db:
        models = list(await db.scalars(select(SessionModelResult).where(
            SessionModelResult.run_id == return_id).order_by(SessionModelResult.round_id)))
        assert len(models) == 3
        snapshots = [row.model_request_snapshot_json for row in models]
        for index, snapshot in enumerate(snapshots):
            assert (RESULT in json.dumps(snapshot["wire_request"], ensure_ascii=False)) == (RESULT in wires[index])
        call = (await db.scalars(select(SessionToolInvocation).where(
            SessionToolInvocation.run_id == return_id,
            SessionToolInvocation.provider_tool_use_id == "r27a-task-read"))).one()
        proof_row = await db.get(InvocationSpan, uuid5(call.id, "runtime-task-read"))
        proof = (proof_row.metadata_json or {}).get("runtime_task_read") if proof_row else None

    observation = {
        "revoked": revoked,
        "automatic_model_has_answer": RESULT in wires[0],
        "pinned_read_status": pinned["status"],
        "model_after_pinned_read_has_answer": RESULT in wires[1],
        "task_read_status": checked["status"],
        "task_result_has_answer": RESULT in json.dumps(checked, ensure_ascii=False),
        "model_after_task_read_has_answer": RESULT in wires[2],
        "task_read_proof_present": proof is not None,
        "task_read_unbound": proof.get("unbound") if proof else None,
        "task_read_source_kinds": sorted({row["source_kind"] for row in proof["sources"]}) if proof else [],
        "final_model_dependency_kinds": sorted({row["source_kind"] for row in
            snapshots[2]["input_bindings"]["dependencies"]}),
    }
    print("R27A_SOURCE_READER_OBSERVATION", json.dumps(observation, sort_keys=True))
    if revoked:
        assert RESULT not in wires[2], "check_async_task exposed a colleague answer whose source the member cannot read"
    else:
        assert RESULT in wires[2]
