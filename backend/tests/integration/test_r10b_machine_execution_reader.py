"""A non-Web machine run retains the publication runtime reader at input time."""

import uuid

import pytest

from tests.integration.test_m3i_machine_tasks_postgres import machine_api  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_trigger_execution_inputs_use_machine_publication_reader(
    machine_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    from app.models.runtime_task import RuntimeTask
    from app.models.chat_session import ChatSession
    from app.models.workspace_resource import WorkspaceResourceManifest
    from sqlalchemy import update
    from app.services import content_input_receipts as receipts
    from app.services.machine_runtime_authority import is_machine_runtime_actor
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    from tests.integration.test_artifact_publication_transactions import stage_report
    from tests.integration.test_m3i_machine_tasks_postgres import _prepare_native_machine
    from tests.integration.test_m5_machine_command_environments_postgres import _start
    from tests.integration.test_r10a_workspace_source_admission import _unverifiable_history

    seed, api = machine_api
    root = await _prepare_native_machine(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path)
    _identity, _headers, _rpc, task_id = await _start(seed, api, app_user_sessionmaker)
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, task_id)
        task.task_type = "trigger"
        session_id = uuid.UUID(task.parent_session_id)
        session = await db.get(ChatSession, session_id)
        root_session = session.root_session_id or session.id
        path, _source = await _unverifiable_history(db, tenant=seed["tenant"], agent=seed["agent"],
            user=None, session=session_id, root=root)
        good_path = "workspace/machine-own.md"
        await stage_report(db, tenant=seed["tenant"], agent=seed["agent"], user=None,
            session=root_session, root=root, path=good_path)
        # Machine writers explicitly bind owned files to their machine work;
        # the general human fixture otherwise quarantines owner-less files.
        await db.execute(update(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == seed["agent"], WorkspaceResourceManifest.path.in_([
                "workspace/old-native-output.md", "workspace/intermediate-report.md", path, good_path]))
            .values(authority_state="owned", root_session_id=root_session))
        await db.commit()
    context = ToolExecutionContext(agent_id=seed["agent"], user_id=None, tenant_id=str(seed["tenant"]),
        session_id=str(session_id), runtime_task_id=str(task_id), workspace=root,
        workspace_authority_scope=WorkspaceAuthorityScope(agent_id=seed["agent"], user_id=None,
            root_session_id=session_id, allowed_paths=frozenset({path, good_path}), operator_view=False,
            authority_source="machine_task", full_control_paths=frozenset({path, good_path})))
    request = ToolExecutionRequest(tool_name="run_command", arguments={"command": "cat machine-own.md"}, context=context)
    readers = []
    native = receipts._validate_workspace_dependency

    async def observe(db, tenant_id, dependency, **kwargs):
        if kwargs["runtime_request"] is request:
            principal, actor = await receipts._runtime_input_actor(db, tenant_id, request)
            assert principal.requester_user_id is None and is_machine_runtime_actor(actor)
            readers.append(dependency["source_id"])
        try:
            return await native(db, tenant_id, dependency, **kwargs)
        except receipts.ContentInputReceiptError as exc:
            print("R10B_MACHINE_WITHHELD", dependency["source_id"], exc.code)
            raise

    monkeypatch.setattr(receipts, "_validate_workspace_dependency", observe)
    assert await receipts.authorize_workspace_execution_inputs(request) is None
    assert readers
    assert context.workspace_authority_scope.allowed_paths == frozenset({good_path})
    assert [read.path for read in context.workspace_authority_scope.input_reads] == [good_path]
