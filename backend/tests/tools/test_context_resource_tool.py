from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest


def _request(*, arguments: dict, tmp_path: Path):
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    return ToolExecutionRequest(
        tool_name="read_context_resource",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=uuid4(),
            user_id=uuid4(),
            tenant_id=str(uuid4()),
            workspace=tmp_path,
            session_id=str(uuid4()),
        ),
    )


@pytest.mark.asyncio
async def test_context_resource_tool_pages_hash_pinned_agent_context(monkeypatch, tmp_path):
    from app.services.agent_context import AgentContextResource
    from app.tools.handlers import context_resources

    content = "0123456789" * 80
    calls: list[tuple[object, object, str]] = []

    async def fake_load(*, agent_id, tenant_id, resource_ref):
        calls.append((agent_id, tenant_id, resource_ref))
        return AgentContextResource(
            ref=resource_ref,
            source_ref=f"agent-context://{resource_ref}",
            content=content,
        )

    monkeypatch.setattr(context_resources, "load_agent_context_resource", fake_load)
    request = _request(arguments={"ref": "company", "offset": 11, "limit": 256}, tmp_path=tmp_path)

    first = json.loads(await context_resources.read_context_resource(request))

    assert first["schema"] == "hive.agent_context_resource_page.v1"
    assert first["status"] == "ok"
    assert first["ref"] == "company"
    assert first["content"] == content[11:267]
    assert first["offset"] == 11
    assert first["next_offset"] == 267
    assert first["complete"] is False
    assert first["total_chars"] == len(content)
    assert len(first["sha256"]) == 64
    assert calls == [(request.context.agent_id, request.context.tenant_id, "company")]

    stale_request = _request(
        arguments={
            "ref": "company",
            "offset": first["next_offset"],
            "limit": 256,
            "expected_sha256": "0" * 64,
        },
        tmp_path=tmp_path,
    )
    stale = json.loads(await context_resources.read_context_resource(stale_request))

    assert stale["status"] == "stale_resource"
    assert stale["restart_offset"] == 0
    assert stale["actual_sha256"] == first["sha256"]
    assert "content" not in stale


@pytest.mark.asyncio
async def test_context_resource_tool_rejects_caller_selected_principal(tmp_path):
    from app.tools.handlers.context_resources import read_context_resource

    request = _request(
        arguments={"ref": "soul", "agent_id": str(uuid4())},
        tmp_path=tmp_path,
    )

    result = json.loads(await read_context_resource(request))

    assert result["status"] == "invalid_arguments"
    assert result["unknown_fields"] == ["agent_id"]


def test_context_resource_tool_is_always_available_but_excludes_personal_kb():
    from app.services.capability_gate import _CAPABILITY_GATE_EXEMPT_TOOLS
    from app.services.governance_capability_taxonomy import CORE_TOOL_NAMES
    from app.tools.collector import collect_tools
    from app.tools.governance import _STATIC_SAFE_TOOLS
    from app.tools.plan_mode_policy import PLAN_MODE_READONLY_TOOLS

    collected = collect_tools()
    schema = next(
        tool["function"] for tool in collected.openai_tools if tool["function"]["name"] == "read_context_resource"
    )
    result_schema = next(
        tool["function"] for tool in collected.openai_tools if tool["function"]["name"] == "read_runtime_result"
    )

    assert "read_context_resource" in CORE_TOOL_NAMES
    assert "read_context_resource" in _CAPABILITY_GATE_EXEMPT_TOOLS
    assert "read_context_resource" in _STATIC_SAFE_TOOLS
    assert "read_context_resource" in PLAN_MODE_READONLY_TOOLS
    assert "read_runtime_result" in CORE_TOOL_NAMES
    assert "read_runtime_result" in _CAPABILITY_GATE_EXEMPT_TOOLS
    assert "read_runtime_result" in _STATIC_SAFE_TOOLS
    assert "read_runtime_result" in PLAN_MODE_READONLY_TOOLS
    assert result_schema["parameters"]["required"] == ["result_ref"]
    assert "callers cannot select another principal" in result_schema["description"]
    ref_schema = schema["parameters"]["properties"]["ref"]
    static_refs = ref_schema["anyOf"][0]["enum"]
    assert static_refs == [
        "index",
        "soul",
        "company",
        "organization",
        "channels",
        "a2a-collaborators",
    ]
    assert ref_schema["anyOf"][1]["pattern"].startswith("^recovery-manifest://")
    assert not any("personal" in ref or "knowledge" in ref for ref in static_refs)
    assert "does not search or read Personal Knowledge Base" in schema["description"]


@pytest.mark.asyncio
async def test_context_resource_tool_reads_only_current_authority_recovery_snapshot(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.runtime.ccplus_contracts import permission_profile_snapshot_hash
    from app.runtime.recovery_manifest_store import (
        RecoveryAuthorityFrame,
        load_recovery_manifest,
        persist_recovery_manifest,
    )
    from app.runtime.session import SessionContext
    from app.tools.handlers.context_resources import read_context_resource
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = str(uuid4())
    root_task_id = str(uuid4())
    permission_profile = {"mode": "default"}
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), JWT_SECRET_KEY="test-secret"),
    )
    authority = RecoveryAuthorityFrame(
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        requester_user_id=str(user_id),
        session_id=session_id,
        root_session_id=session_id,
        root_runtime_task_id=root_task_id,
        principal_type="delegated_user",
        principal_id=str(user_id),
        principal_snapshot_hash="principal-hash",
        policy_snapshot_hash=permission_profile_snapshot_hash(permission_profile),
        config_snapshot_hash="config-hash",
        base_transcript_sequence=12,
    )
    session = SessionContext(
        session_id=session_id,
        pending_items=["PRIVATE_RECOVERY_SENTINEL"],
        metadata={
            "permission_profile": permission_profile,
            "base_transcript_sequence": 12,
        },
    )
    assert persist_recovery_manifest(authority, session, data_root=tmp_path).status == "written"
    loaded = load_recovery_manifest(authority, data_root=tmp_path)
    loaded.render_restoration_text(budget_chars=700)
    assert loaded.manifest_ref is not None
    assert loaded.envelope_sha256 is not None

    def request_for(*, requester_id, requested_session=session_id):
        return ToolExecutionRequest(
            tool_name="read_context_resource",
            arguments={
                "ref": loaded.manifest_ref,
                "expected_sha256": loaded.envelope_sha256,
                "limit": 12000,
            },
            context=ToolExecutionContext(
                agent_id=agent_id,
                user_id=requester_id,
                tenant_id=str(tenant_id),
                workspace=tmp_path / str(agent_id),
                session_id=requested_session,
                runtime_task_id=root_task_id,
                permission_profile=permission_profile,
                execution_identity=SimpleNamespace(
                    identity_type="delegated_user",
                    identity_id=user_id,
                ),
            ),
        )

    allowed = json.loads(await read_context_resource(request_for(requester_id=user_id)))
    denied = json.loads(await read_context_resource(request_for(requester_id=uuid4())))

    assert allowed["status"] == "ok"
    assert allowed["source_ref"] == loaded.manifest_ref
    assert allowed["sha256"] == loaded.envelope_sha256
    assert "PRIVATE_RECOVERY_SENTINEL" in allowed["content"]
    assert denied["status"] == "authority_denied"
    assert "content" not in denied


@pytest.mark.asyncio
async def test_context_resource_tool_rejects_tampered_recovery_snapshot(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app.runtime.ccplus_contracts import permission_profile_snapshot_hash
    from app.runtime.recovery_manifest_store import (
        RecoveryAuthorityFrame,
        load_recovery_manifest,
        persist_recovery_manifest,
    )
    from app.runtime.session import SessionContext
    from app.tools.handlers.context_resources import read_context_resource
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = str(uuid4())
    root_task_id = str(uuid4())
    permission_profile = {"mode": "default"}
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path), JWT_SECRET_KEY="test-secret"),
    )
    authority = RecoveryAuthorityFrame(
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        requester_user_id=str(user_id),
        session_id=session_id,
        root_session_id=session_id,
        root_runtime_task_id=root_task_id,
        principal_type="delegated_user",
        principal_id=str(user_id),
        principal_snapshot_hash="principal-hash",
        policy_snapshot_hash=permission_profile_snapshot_hash(permission_profile),
        config_snapshot_hash="config-hash",
        base_transcript_sequence=None,
    )
    session = SessionContext(
        session_id=session_id,
        pending_items=["ORIGINAL"],
        metadata={"permission_profile": permission_profile},
    )
    persist_recovery_manifest(authority, session, data_root=tmp_path)
    loaded = load_recovery_manifest(authority, data_root=tmp_path)
    loaded.render_restoration_text(budget_chars=700)
    assert loaded.resource_path is not None
    loaded.resource_path.write_bytes(b'{"forged":"FORGED_RECOVERY_SENTINEL"}')
    request = ToolExecutionRequest(
        tool_name="read_context_resource",
        arguments={"ref": loaded.manifest_ref, "expected_sha256": loaded.envelope_sha256},
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=tmp_path / str(agent_id),
            session_id=session_id,
            runtime_task_id=root_task_id,
            permission_profile=permission_profile,
            execution_identity=SimpleNamespace(identity_type="delegated_user", identity_id=user_id),
        ),
    )

    result = json.loads(await read_context_resource(request))

    assert result["status"] == "integrity_mismatch"
    assert "content" not in result
    assert "FORGED_RECOVERY_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize("ref", ["company", "organization"])
def test_native_empty_context_observation_does_not_attest_nonempty_or_forged_pages(ref):
    import hashlib

    from app.services.content_input_receipts import _native_empty_read_matches

    observation = {"kind": "context_resource_empty", "ref": ref}
    page = {"schema": "hive.agent_context_resource_page.v1", "status": "ok", "ref": ref,
        "context_ref": f"agent-context://{ref}", "source_ref": f"agent-context://{ref}",
        "sha256": hashlib.sha256(b"").hexdigest(), "offset": 0, "next_offset": 0,
        "complete": True, "total_chars": 0, "content": ""}
    def matches(body, *, arguments=None, proof=None):
        return _native_empty_read_matches("read_context_resource", arguments or {"ref": ref},
            proof or observation, session_id=str(uuid4()), content=json.dumps(body))

    assert matches(page)
    for altered in [dict(page, content="Private organization body"), dict(page, sha256="0" * 64),
        dict(page, offset=True), dict(page, complete=False), dict(page, total_chars=1),
        dict(page, source_ref="agent-context://soul"), dict(page, additional="forged")]:
        assert not matches(altered)
    assert not matches(page, arguments={"ref": "soul"})
    assert not matches(page, proof=dict(observation, additional="forged"))
