"""Outbound tools need deliver authority and consume only approved file bytes."""

from pathlib import Path
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from tests.services.test_workspace_outbound_files_postgres import (
    _invoke,
    outbound_files as outbound_files,
)

TOOLS = ("send_email", "upload_image", "feishu_base_record_upload_attachment")


@pytest.fixture
def delivery_files(outbound_files, monkeypatch):
    from app.services.agent_tool_domains import feishu_base
    from app.tools.handlers import feishu

    n = outbound_files
    n.base_updates, n.cli_uploads = [], []
    (n.context.workspace / "soul.md").write_bytes(b"Internal root file")
    (n.context.workspace / "workspace/internal-alias.png").symlink_to(n.context.workspace / "workspace/foreign.png")
    monkeypatch.setattr(feishu, "_check_feishu_office_access", AsyncMock(return_value=True))
    monkeypatch.setattr(feishu_base, "_get_feishu_token", AsyncMock(return_value=("fixture", "fixture-token")))
    monkeypatch.setattr(feishu_base, "_feishu_api_url", lambda _token, path: "https://fixture.test" + path)
    n.cli_available = AsyncMock(return_value=True)
    monkeypatch.setattr(feishu_base, "_feishu_cli_available", n.cli_available)

    async def update(_token, path, body):
        n.base_updates.append((path, body))

    async def cli(args):
        path = Path(args[args.index("--file") + 1])
        name = args[args.index("--name") + 1] if "--name" in args else path.name
        n.cli_uploads.append((path, name, path.read_bytes()))
        return {"record": {"record_id": "record"}, "attachment": {"file_token": "fixture-upload", "name": name}}

    monkeypatch.setattr(feishu_base, "_base_api_put", update)
    monkeypatch.setattr(feishu_base, "_run_feishu_base_shortcut", cli)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            files = kwargs.get("files", {})
            n.uploads.append({name: (value[0], value[1].read() if hasattr(value[1], "read") else value[1])
                for name, value in files.items()})
            return httpx.Response(200, json={"code": 0, "data": {"file_token": "fixture-upload"},
                "url": "https://fixture.test/image.png", "fileId": "fixture-upload"})

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    return n


async def invoke(n, tool_name, path):
    if tool_name in {"send_email", "upload_image"}:
        return await _invoke(n, tool_name, [path])
    if tool_name == "email_service":
        return await n.send(config={"email_provider": "gmail", "email_address": "sender@fixture.test", "auth_code": "fixture"},
            to="recipient@fixture.test", subject="Report", body="Attached.", attachments=[path],
            workspace_path=n.context.workspace, authority_scope=n.context.workspace_authority_scope)
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.feishu import feishu_base_record_upload_attachment as handler
    from app.tools.runtime import ToolExecutionRequest

    request = ToolExecutionRequest(tool_name, {"base_token": "base", "table_id": "table", "record_id": "record",
        "field_id": "field", "file_path": path}, n.context)
    return await adapt_and_call(handler.meta, handler, request)


def refusal(tool_name, path):
    verb = "attached" if tool_name in {"send_email", "email_service"} else "uploaded"
    return f"❌ {path} cannot be {verb}. Use a file under workspace/ that you may send."


def no_effects(n):
    assert n.smtp_calls == []
    assert n.messages == []
    assert n.uploads == []
    assert n.base_updates == []
    assert n.cli_uploads == []


@pytest.mark.parametrize("tool_name", TOOLS)
async def test_read_only_grant_cannot_deliver_foreign_bytes(delivery_files, owner_sessionmaker, tool_name):
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    n = delivery_files
    path = "workspace/foreign.png"
    async with owner_sessionmaker() as db:
        db.add(ResourcePermission(tenant_id=UUID(n.context.tenant_id),
            principal_type="user", principal_id=n.context.user_id, resource_type="workspace_file",
            resource_id=workspace_resource_id(n.context.agent_id, path), actions=["read"], conditions={}))
        await db.commit()
        user = await db.get(User, n.context.user_id)
        n.context.workspace_authority_scope = await load_workspace_authority_scope(db, user,
            agent_id=n.context.agent_id, session_id=UUID(n.context.session_id))
    scope = n.context.workspace_authority_scope
    assert scope.can_read(path)
    assert not scope.can_perform(path, "deliver")
    result = await invoke(n, tool_name, path)
    no_effects(n)
    assert result == refusal(tool_name, path)


@pytest.mark.parametrize("tool_name", (*TOOLS, "email_service"))
async def test_missing_scope_refuses_outbound_file(delivery_files, tool_name):
    n = delivery_files
    n.context.workspace_authority_scope = None
    result = await invoke(n, tool_name, n.own)
    no_effects(n)
    assert result == refusal(tool_name, n.own)


@pytest.mark.parametrize("tool_name", TOOLS)
async def test_noncanonical_foreign_and_missing_file_have_one_refusal(delivery_files, tool_name):
    n = delivery_files
    path = "workspace/./foreign.png"
    present = await invoke(n, tool_name, path)
    (n.context.workspace / "workspace/foreign.png").unlink()
    missing = await invoke(n, tool_name, path)
    assert present == missing == refusal(tool_name, path)
    no_effects(n)


@pytest.mark.parametrize("tool_name", TOOLS)
async def test_owned_bytes_use_approved_path_and_resolved_name(delivery_files, monkeypatch, tool_name):
    from app.services.agent_tool_domains import workspace

    n = delivery_files
    reads, resolves = [], []
    original_read, original_resolve = workspace._read_receipted_workspace_bytes, workspace._resolve_workspace_tool_file

    def read(root, path, scope, authority_path):
        reads.append(path)
        return original_read(root, path, scope, authority_path)

    def resolve(*args, **kwargs):
        resolves.append(args[1])
        return original_resolve(*args, **kwargs)

    monkeypatch.setattr(workspace, "_read_receipted_workspace_bytes", read)
    monkeypatch.setattr(workspace, "_resolve_workspace_tool_file", resolve)
    result = await invoke(n, tool_name, r"workspace\mine\own.png")
    assert "cannot be" not in result
    if tool_name == "send_email":
        assert len(n.messages) == 1
        parts = [part for part in n.messages[0].walk() if part.get_content_disposition() == "attachment"]
        assert [(part.get_filename(), part.get_payload(decode=True)) for part in parts] == [("own.png", n.own_bytes)]
    else:
        assert n.uploads == [{"file": ("own.png", n.own_bytes)}]
        if tool_name == "feishu_base_record_upload_attachment":
            assert n.base_updates[0][1] == {"fields": {"field": [{"file_token": "fixture-upload", "name": "own.png"}]}}
    assert resolves == [r"workspace\mine\own.png"]
    assert reads == [n.own]


@pytest.mark.parametrize("path", ["workspace/foreign.png", "memory/.staging/x.png", "soul.md", "workspace/internal-alias.png"])
async def test_feishu_base_refuses_private_and_internal_files(delivery_files, path):
    n = delivery_files
    result = await invoke(n, "feishu_base_record_upload_attachment", path)
    no_effects(n)
    n.cli_available.assert_not_called()
    assert result == refusal("feishu_base_record_upload_attachment", path)


async def test_feishu_cli_uses_approved_bytes_if_source_changes(delivery_files, monkeypatch):
    from app.services.agent_tool_domains import feishu_base

    n = delivery_files

    async def cli_only(_agent_id):
        target = n.context.workspace / n.own
        target.unlink()
        target.symlink_to(n.context.workspace / "workspace/foreign.png")
        return None

    monkeypatch.setattr(feishu_base, "_get_feishu_token", cli_only)
    result = await invoke(n, "feishu_base_record_upload_attachment", n.own)
    assert "fixture-upload" in result
    assert len(n.cli_uploads) == 1
    path, name, content = n.cli_uploads[0]
    assert (name, content) == ("own.png", n.own_bytes)
    assert not path.exists()
    assert n.uploads == []


async def test_image_limit_applies_to_bytes_read_after_source_grows(delivery_files, monkeypatch):
    from app.services.agent_tool_domains import workspace

    n = delivery_files
    original = workspace._read_receipted_workspace_bytes

    def grow_then_read(root, path, scope, authority_path):
        (root / path).write_bytes(b"x" * (25 * 1024 * 1024 + 1))
        return original(root, path, scope, authority_path)

    monkeypatch.setattr(workspace, "_read_receipted_workspace_bytes", grow_then_read)
    result = await invoke(n, "upload_image", n.own)
    no_effects(n)
    assert "File too large" in result
    assert "Maximum is 25MB" in result
