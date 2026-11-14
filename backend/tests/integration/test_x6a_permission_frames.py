"""The approval card renders the two real permission producers in arrival order."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import (
    _prepare_write,
    native_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_permission_events_share_scope_and_keep_rendered_choices(
    native_workspace, owner_sessionmaker, tmp_path  # noqa: F811
):
    from app.services.session_event_contract import serialize_session_event
    from app.services.session_permission_runtime import _execution_evidence
    from app.services.session_tool_runtime import complete_tool_invocation
    from app.services.thread_items import build_live_thread_item

    n = native_workspace
    frames = []
    for index, (tool_name, arguments, allow_session) in enumerate([
        ("plaza_create_post", {"content": "Team update"}, True),
        ("delete_file", {"path": "workspace/obsolete.txt"}, False),
    ], start=1):
        async with owner_sessionmaker() as db:
            invocation, arguments, turn = await _prepare_write(
                db, n, tool_name=tool_name, arguments=arguments, round_index=index
            )
            trace, events = {}, []
            result = await n.service.execute(
                tool_name, arguments, agent_id=n.agent, user_id=n.user,
                session_id=str(n.session), runtime_task_id=str(n.run), turn_id=turn,
                tool_call_id=invocation.provider_tool_use_id, event_callback=events.append,
                emit_runtime_hooks=False, trace_metadata_sink=trace,
            )
            waiting = await complete_tool_invocation(
                db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session,
                invocation_id=invocation.id, provider_result_content=str(result),
                execution_evidence=_execution_evidence(trace),
            )
            legacy = next(event for event in events if event.get("status") == "session_permission_required")
            canonical = next(event for event in waiting if event.item_kind == "tool_permission")
            # Use the live legacy projection and the canonical user envelope,
            # including the baseline's missing legacy flag rather than patching the fixture.
            frames.append({
                "tool_name": tool_name,
                "allow_session": allow_session,
                "legacy": {**legacy, **build_live_thread_item(legacy, agent_id=n.agent, session_id=n.session)},
                "canonical": serialize_session_event(canonical, audience="user"),
            })
            await db.commit()

    fixture = tmp_path / "permission-frames.json"
    fixture.write_text(json.dumps(frames))
    rendered = subprocess.run(
        ["npx", "--no-install", "vitest", "run", "src/pages/session-workbench/ApprovalFrames.x6a.test.tsx"],
        cwd=Path(__file__).resolve().parents[3] / "frontend",
        env={**os.environ, "NODE_OPTIONS": "--no-experimental-webstorage", "HC_X6A_PERMISSION_FRAMES": str(fixture)},
        capture_output=True, text=True, timeout=60,
    )
    print(rendered.stdout + rendered.stderr)
    for frame in frames:
        first = frame["legacy"]["permission_request"]
        second = frame["canonical"]["payload"]["permission_request"]
        assert first["permission_request_id"] == second["permission_request_id"]
        assert first["allow_session_allowed"] is second["allow_session_allowed"] is frame["allow_session"]
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
