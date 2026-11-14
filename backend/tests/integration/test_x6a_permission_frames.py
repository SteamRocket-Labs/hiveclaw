"""The approval card renders the two real permission producers in arrival order."""

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _prepare_write,
    native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_permission_events_share_scope_and_keep_rendered_choices(
    native_workspace, owner_sessionmaker, tmp_path, monkeypatch  # noqa: F811
):
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_permission_runtime import _execution_evidence
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services import session_tool_runtime, web_chat_runtime
    from app.tools.governance import _detect_destructive_delete
    from app.tools.registry import is_destructive_tool

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=2)

    n = native_workspace
    frames = []
    delivered = []

    async def capture(_agent_id, _session_id, payload):
        delivered.append(payload)

    monkeypatch.setattr(web_chat_runtime.web_chat_broker, "send_session_message", capture)

    async def publish_legacy(event):
        if event.get("status") != "session_permission_required":
            return
        committed = await web_chat_runtime._persist_runtime_event(
            agent_id=n.agent, user_id=n.user, session_id=str(n.session),
            data={**event, "runtime_task_id": str(n.run)},
        )
        await web_chat_runtime.broadcast_web_chat_event(n.agent, n.session, committed)

    for index, (tool_name, arguments, allow_session, later_receipt) in enumerate([
        ("plaza_create_post", {"content": "Team update"}, True, False),
        ("delete_file", {"path": "workspace/obsolete.txt"}, False, False),
        ("retire_memory", {"memory_id": "obsolete-entry", "reason": "obsolete"}, False, False),
        ("plaza_create_post", {"content": "Team update"}, True, True),
    ], start=1):
        if tool_name == "retire_memory":
            assert is_destructive_tool(tool_name) and not _detect_destructive_delete(tool_name, arguments)
        async with owner_sessionmaker() as db:
            invocation, arguments, turn = await _prepare_write(
                db, n, tool_name=tool_name, arguments=arguments, round_index=index
            )
            trace = {}
            result = await n.service.execute(
                tool_name, arguments, agent_id=n.agent, user_id=n.user,
                session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
                tool_call_id=invocation.provider_tool_use_id, event_callback=publish_legacy,
                emit_runtime_hooks=False, trace_metadata_sink=trace,
            )
            if later_receipt:
                # POST_TOOL_USE can rewrite the receipt after the first frame.
                receipt = json.loads(str(result))
                receipt["permission_request"]["expires_at"] = (
                    datetime.now().astimezone() + timedelta(days=1)
                ).isoformat()
                result = json.dumps(receipt)
            with monkeypatch.context() as clock:
                clock.setattr(session_tool_runtime, "datetime", LaterDatetime)
                waiting = await session_tool_runtime.complete_tool_invocation(
                    db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                    invocation_id=invocation.id, provider_result_content=str(result),
                    execution_evidence=_execution_evidence(trace), effective_arguments=trace["effective_arguments"],
                )
            legacy = delivered[-1]
            canonical = next(event for event in waiting if event.item_kind == "tool_permission")
            # Display clocks stay with the original request; permission resolution
            # still uses the existing invocation deadline computed on settlement.
            first_expiry = legacy["payload"]["metadata"]["permission_request"]["expires_at"]
            assert invocation.permission_expires_at - datetime.fromisoformat(first_expiry) >= timedelta(seconds=1)
            await db.commit()
            reloaded = await db.get(ChatTranscriptEvent, canonical.id, populate_existing=True)
            assert legacy["schema"] == "hive.session_event_compatibility"
            frames.append({
                "tool_name": tool_name,
                "allow_session": allow_session,
                "legacy": legacy,
                "canonical": serialize_session_event(canonical, audience="user"),
                "reload": serialize_session_event(reloaded, audience="user"),
            })

    fixture = tmp_path / "permission-frames.json"
    fixture.write_text(json.dumps(frames))
    report_file = tmp_path / "vitest-report.json"
    rendered = subprocess.run(
        ["npx", "--no-install", "vitest", "run", "src/pages/session-workbench/ApprovalFrames.x6a.test.tsx",
         "--reporter=json", f"--outputFile={report_file}"],
        cwd=Path(__file__).resolve().parents[3] / "frontend",
        env={**os.environ, "NODE_OPTIONS": "--no-experimental-webstorage", "HC_X6A_PERMISSION_FRAMES": str(fixture)},
        capture_output=True, text=True, timeout=60,
    )
    print(rendered.stdout + rendered.stderr)
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    report = json.loads(report_file.read_text())
    assert report["numPassedTests"] == 2 * len(frames)
    assert report["numPendingTests"] == 0
    for frame in frames:
        first = frame["legacy"]["payload"]["metadata"]["permission_request"]
        second = frame["canonical"]["payload"]["permission_request"]
        assert first["permission_request_id"] == second["permission_request_id"]
        assert first["allow_session_allowed"] is second["allow_session_allowed"] is frame["allow_session"]
        assert first["destructive"] is second["destructive"] is not frame["allow_session"]
        assert first["arguments"] == second["arguments"]
        # created_at is not rendered, and this assertion pinned the receipt parse that this round removes.
        assert first["expires_at"] == second["expires_at"]
        assert first.get("risk_class") == second.get("risk_class")
        assert first.get("confirmation_kind") == second.get("confirmation_kind")
