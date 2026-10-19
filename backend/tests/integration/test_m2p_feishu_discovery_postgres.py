"""Company-only Feishu connections reach ordinary work through native discovery.

PostgreSQL, scoped app-role sessions, the worker and tool/result persistence are
real. Model and Feishu HTTP transports are controlled; no live provider is used.
"""

import json
import sys
from uuid import uuid4, uuid5

import httpx
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.agent import Agent
from app.models.channel_config import ChannelConfig
from app.models.tenant_channel_config import TenantChannelConfig
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run, ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")

OFFICE_TOOLS = {"feishu_doc_read", "feishu_approval_query"}


@pytest.fixture
async def ordinary_work(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    import app.database as database
    from app.config import get_settings
    from app.services import agent_context, agent_tools

    seed = await _seed_run(owner_sessionmaker, tmp_path,
        prompt="Read the company document and approval status, then prepare the report in this work.")
    factory = database.async_session
    for module in list(sys.modules.values()):
        if module is not None and getattr(module, "__name__", "").startswith("app."):
            for name, value in list(vars(module).items()):
                if value is factory:
                    monkeypatch.setattr(module, name, app_user_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(get_settings(), "FEISHU_CLI_ENABLED", False)
    monkeypatch.setattr(agent_tools, "WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", seed["data_root"])
    monkeypatch.setattr(agent_tools, "_TOOL_RUNTIME_SERVICE", None)
    monkeypatch.setattr(agent_context, "PERSISTENT_DATA", seed["data_root"])
    monkeypatch.setattr(agent_context, "TOOL_WORKSPACE", seed["data_root"])
    async with owner_sessionmaker() as db:
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu",
            app_id="cli_company_work", app_secret="synthetic-company-work-secret", is_active=True)
        db.add(config)
        await db.commit()
    seed["config_id"] = config.id
    return seed


async def _discovered(agent_id):
    from app.services.agent_tools import get_agent_tools_for_llm

    tools = await get_agent_tools_for_llm(agent_id, requested_names=sorted(OFFICE_TOOLS))
    return {tool["function"]["name"] for tool in tools} & OFFICE_TOOLS


async def test_company_discovery_matches_runtime_credentials_and_original_work_readback(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.services.agent_tool_domains.feishu_helpers import _get_feishu_app_credentials
    from app.services.session_channel_configuration import channel_configuration_snapshot
    from app.services.channel_delivery_service import channel_delivery_target

    seed = ordinary_work
    def no_provider(*_args, **_kwargs):
        raise AssertionError("Discovery must not request provider authentication")
    monkeypatch.setattr(httpx, "AsyncClient", no_provider)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        snapshot = await channel_configuration_snapshot(db, agent_id=seed["agent_id"], channel_type="feishu")
        assert snapshot["scope"] == "company" and snapshot["id"] == str(seed["config_id"])
    assert await _get_feishu_app_credentials(seed["agent_id"]) == ("cli_company_work", "synthetic-company-work-secret")
    assert await _discovered(seed["agent_id"]) == OFFICE_TOOLS
    for field, value in [("is_active", False), ("app_secret", "")]:
        async with owner_sessionmaker() as db:
            config = await db.get(TenantChannelConfig, seed["config_id"])
            setattr(config, field, value)
            await db.commit()
        assert await _discovered(seed["agent_id"]) == set()
        assert await _get_feishu_app_credentials(seed["agent_id"]) is None
        async with owner_sessionmaker() as db:
            config = await db.get(TenantChannelConfig, seed["config_id"])
            config.is_active = True
            config.app_secret = "synthetic-company-work-secret"
            await db.commit()
        assert await _discovered(seed["agent_id"]) == OFFICE_TOOLS
    # A saved enterprise target cannot select an unrelated employee installation.
    async with owner_sessionmaker() as db:
        db.add(ChannelConfig(tenant_id=seed["tenant_id"], agent_id=seed["agent_id"], channel_type="feishu",
            app_id="cli_employee_work", app_secret="synthetic-employee-secret", is_configured=True))
        await db.commit()
    assert await _get_feishu_app_credentials(seed["agent_id"]) == ("cli_employee_work", "synthetic-employee-secret")
    token = channel_delivery_target.set({"tenant_channel_config_id": str(uuid4())})
    try:
        assert await _discovered(seed["agent_id"]) == set()
        assert await _get_feishu_app_credentials(seed["agent_id"]) is None
    finally:
        channel_delivery_target.reset(token)


async def test_foreign_company_connection_does_not_create_discovery_access(
    ordinary_work, owner_sessionmaker, tmp_path,
):
    seed = await _seed_run(owner_sessionmaker, tmp_path / "another-company")
    assert seed["tenant_id"] != ordinary_work["tenant_id"]
    assert await _discovered(seed["agent_id"]) == set()


async def _exercise_ordinary_office_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, *, follow_up=None,
):
    from app.runtime import invoker
    from app.models.capability_policy import CapabilityPolicy
    from app.models.llm import LLMModel
    from app.models.chat_artifact import ChatArtifact
    from app.models.runtime_task import RuntimeTask
    from app.models.session_v2 import SessionToolInvocation
    from app.services.agent_manager import AgentManager
    from app.services.agent_tools import _get_tool_runtime_service
    from app.services.secrets_provider import get_secrets_provider
    from app.services.runtime_task_fence import set_runtime_task_fence, reset_runtime_task_fence
    from app.services.web_chat_runtime import execute_web_chat_run
    from app.services.runtime_terminal_boundary_outbox import RuntimeTerminalBoundaryOutboxService
    from app.services.web_terminal_boundary_processor import WebTerminalBoundaryProcessor

    seed = ordinary_work
    (seed["agent_root"] / "workspace").rmdir()
    seed["agent_root"].rmdir()
    async with owner_sessionmaker() as db:
        agent = await db.get(Agent, seed["agent_id"])
        agent.default_session_permission_mode = "bypassPermissions"
        model = await db.get(LLMModel, agent.primary_model_id)
        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-company-work-model")
        model.max_input_tokens = 256_000
        await AgentManager().initialize_agent_files(db, agent, personality="Use the company connection",
            original_author_id=seed["user_id"])
        for capability in ("workspace.file.write", "channel.feishu.document", "channel.feishu.approval"):
            db.add(CapabilityPolicy(tenant_id=seed["tenant_id"], capability=capability,
                allowed=True, requires_approval=False))
        await db.commit()

    requests = []
    provider_state = {"secret": "synthetic-company-work-secret"}
    def provider(request):
        assert request.url.host == "open.feishu.cn", request.url
        requests.append((request.method, request.url.path))
        if "/auth/v3/" in request.url.path:
            assert json.loads(request.content) == {"app_id": "cli_company_work", "app_secret": provider_state["secret"]}
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "synthetic-company-work-token"})
        assert request.headers["Authorization"] == "Bearer synthetic-company-work-token"
        if request.url.path.endswith("/spaces/get_node"):
            return httpx.Response(200, json={"code": 131004, "msg": "Not a wiki node"})
        if request.url.path == "/open-apis/docx/v1/documents/company_doc/raw_content":
            return httpx.Response(200, json={"code": 0, "data": {"content": "COMPANY_DOCUMENT_RESULT"}})
        assert request.url.path == "/open-apis/approval/v4/instances/query", request.url
        assert json.loads(request.content) == {"approval_code": "company_approval"}
        return httpx.Response(200, json={"code": 0, "data": {"instance_code_list": ["COMPANY_APPROVAL_RESULT"]}})

    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**{**kw, "transport": httpx.MockTransport(provider)}))
    def call(name, arguments):
        return {"tool_calls": [{"id": f"company-{name}", "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}], "finish_reason": "tool_calls"}
    script = ScriptedLLMClient([
        call("tool_search", {"query": "feishu"}),
        call("feishu_doc_read", {"document_token": "company_doc"}),
        call("feishu_approval_query", {"approval_code": "company_approval"}),
        call("write_file", {"path": "workspace/company-report.md", "content": "COMPANY_DOCUMENT_RESULT\nCOMPANY_APPROVAL_RESULT"}),
        {"content": "The company report is ready in this work.\nDELIVERABLE: workspace/company-report.md", "finish_reason": "stop"},
    ])
    monkeypatch.setattr(invoker, "create_llm_client", lambda **_kwargs: script)
    monkeypatch.setattr("random.uniform", lambda *_args: 0)
    fence = set_runtime_task_fence(task_id=seed["run_id"], worker_id="kernel-worker-1", claim_version=1)
    try:
        await execute_web_chat_run(seed["run_id"])
    finally:
        reset_runtime_task_fence(fence)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        run = await db.get(RuntimeTask, seed["run_id"])
        assert run.status == "completed", (run.status, run.result_summary)
        assert run.root_user_id == seed["user_id"] and run.root_session_id == str(seed["session_id"])
        invocations = (await db.scalars(select(SessionToolInvocation).where(SessionToolInvocation.run_id == seed["run_id"]))).all()
        assert OFFICE_TOOLS <= {row.tool_name for row in invocations}
        from app.services.feishu_read_receipts import load_feishu_read
        from app.services.session_model_inputs import _origin_dependencies, _hash
        from app.models.invocation_span import InvocationSpan
        from copy import deepcopy

        for invocation in invocations:
            dependencies, complete = await _origin_dependencies(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                ref=f"session-tool-result:{invocation.id}")
            assert complete, (invocation.tool_name, dependencies)
            if invocation.tool_name not in OFFICE_TOOLS:
                continue
            span = await db.get(InvocationSpan, uuid5(invocation.id, "feishu-read-evidence"))
            original = deepcopy(span.metadata_json)
            digest = original["feishu_read"]["content_hash"]
            proof = await load_feishu_read(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                invocation=invocation, content_hash=digest, current=True)
            assert proof["reader_id"] == str(seed["user_id"])
            assert proof["connection"]["id"] == str(seed["config_id"])
            for field, value in [("content_hash", "0" * 64), ("reader_id", str(uuid4())), ("model_result_id", str(uuid4()))]:
                changed = deepcopy(original)
                changed["feishu_read"][field] = value
                changed["feishu_read"]["receipt_hash"] = _hash({k: v for k, v in changed["feishu_read"].items() if k != "receipt_hash"})
                span.metadata_json = changed
                await db.flush()
                assert await load_feishu_read(db, tenant_id=seed["tenant_id"], agent_id=seed["agent_id"],
                    invocation=invocation, content_hash=digest, current=True) is None
                span.metadata_json = deepcopy(original)
                await db.flush()
        await db.commit()
    assert len(script.requests) == 5
    assert OFFICE_TOOLS <= {tool["function"]["name"] for tool in script.requests[1]["tools"]}
    assert "COMPANY_DOCUMENT_RESULT" in json.dumps(script.requests[3]["messages"])
    assert "COMPANY_APPROVAL_RESULT" in json.dumps(script.requests[3]["messages"])
    assert (seed["agent_root"] / "workspace/company-report.md").read_text() == "COMPANY_DOCUMENT_RESULT\nCOMPANY_APPROVAL_RESULT"
    async def summary(messages, **_kwargs):
        assert "company report" in json.dumps(messages).lower()
        return "Read the company document and approval status and prepared the company report."

    processor = WebTerminalBoundaryProcessor(session_factory=app_user_sessionmaker, data_root=seed["data_root"],
        summary_generator=summary)
    boundary = await RuntimeTerminalBoundaryOutboxService(session_factory=app_user_sessionmaker).drain_once(
        tenant_id=seed["tenant_id"], worker_id="company-report-boundary", canonical_validator=processor.validate,
        process_callback=processor, task_types=("web_chat_turn",))
    assert boundary["delivered"] == 1, boundary
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        report = await db.scalar(select(ChatArtifact).where(ChatArtifact.runtime_task_id == seed["run_id"],
            ChatArtifact.session_id == seed["session_id"], ChatArtifact.path == "workspace/company-report.md"))
        assert report is not None and report.snapshot_json["content_input_receipt_id"]
    from fastapi import FastAPI
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.user import User

    async def current_user():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            return await db.get(User, seed["user_id"])

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            yield db
            await db.commit()

    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    async with client(transport=httpx.ASGITransport(app=app), base_url="http://company-work") as api:
        read = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
        assert read.status_code == 200, read.text
        assert read.json()["content"] == "COMPANY_DOCUMENT_RESULT\nCOMPANY_APPROVAL_RESULT"
    if follow_up is not None:
        await follow_up(seed=seed, app=app, client=client, report=report, processor=processor,
            provider_state=provider_state, requests=requests)
    # Retained tool definitions do not bypass a subsequently disabled connection.
    async with owner_sessionmaker() as db:
        (await db.get(TenantChannelConfig, seed["config_id"])).is_active = False
        await db.commit()
    before = list(requests)
    for name, arguments in [("feishu_doc_read", {"document_token": "company_doc"}),
        ("feishu_approval_query", {"approval_code": "company_approval"})]:
        result = await _get_tool_runtime_service().execute(name, arguments, agent_id=seed["agent_id"], user_id=seed["user_id"],
            session_id=str(seed["session_id"]), emit_runtime_hooks=False)
        assert "not configured" in str(result).lower(), result
    assert requests == before
    async with client(transport=httpx.ASGITransport(app=app), base_url="http://company-work") as api:
        revoked = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
        assert revoked.status_code == 403, revoked.text
    assert requests == before


async def test_ordinary_worker_discovers_company_tools_and_consumes_results_in_original_work(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    await _exercise_ordinary_office_work(ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch)
