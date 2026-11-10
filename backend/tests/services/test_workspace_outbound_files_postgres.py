"""Outbound files use the requester's real workspace read authority."""

import base64
from email import message_from_string
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from tests.services.test_session_input_control_v2 import _seed_session


@pytest.fixture
async def outbound_files(owner_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.models.user import User
    from app.services.agent_tool_domains import email, workspace
    from app.services import email_accounts, email_service, tool_config_service
    from app.services.workspace_resource_authority import load_workspace_authority_scope, register_workspace_path
    from app.tools.runtime import ToolExecutionContext

    tenant, requester, agent, session, _ = await _seed_session(owner_sessionmaker)
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    root = tmp_path / str(agent)
    own, foreign = "workspace/mine/own.png", "workspace/foreign.png"
    root.mkdir()
    own_bytes = b"Requester-created image bytes"
    result = workspace._write_file(root, own, own_bytes.decode())
    # R11g: write_file returns a member receipt; model-visible text is unchanged
    assert "Written to" in result.model_visible_text
    assert str(result) == "已写入文件「own.png」。"
    (root / foreign).write_bytes(b"Other member's private bytes")
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"Outside bytes")
    (root / "workspace/escape.png").symlink_to(outside)
    staging = root / "memory/.staging/x.png"
    staging.parent.mkdir(parents=True)
    staging.write_bytes(b"Platform staging bytes")
    recovery = root / "runtime_artifacts/recovery_manifests/private.png"
    recovery.parent.mkdir(parents=True)
    recovery.write_bytes(b"Platform recovery bytes")
    other = uuid4()
    async with owner_sessionmaker() as db:
        db.add(User(id=other, tenant_id=tenant, username=f"other-{other.hex}",
            email=f"{other.hex}@fixture.test", password_hash="fixture", display_name="Other", role="member"))
        await db.flush()
        for path, owner in ((own, requester), (foreign, other)):
            await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=path,
                owner_user_id=owner, root_session_id=None, source="fixture",
                content_hash=hashlib.sha256((root / path).read_bytes()).hexdigest())
        await db.commit()
        user = await db.get(User, requester)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    assert scope.can_read(own)
    assert not scope.can_read(foreign)
    context = ToolExecutionContext(agent_id=agent, user_id=requester, tenant_id=str(tenant),
        session_id=str(session), workspace=root, workspace_authority_scope=scope)
    config = {"email_provider": "gmail", "email_address": "sender@fixture.test", "auth_code": "fixture"}
    monkeypatch.setattr(email, "_get_email_config", AsyncMock(return_value=config))
    monkeypatch.setattr(email_accounts, "resolve_email_config", AsyncMock(return_value=config))
    monkeypatch.setattr(tool_config_service, "resolve_tool_config", AsyncMock(return_value={"private_key": "fixture"}))
    smtp_calls, messages, uploads = [], [], []

    class SMTP:
        def __init__(self, *args, **kwargs):
            smtp_calls.append(args)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def login(self, *args):
            pass

        def sendmail(self, sender, recipients, message):
            messages.append(message_from_string(message))

    class UploadClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            uploads.append(kwargs)
            return httpx.Response(201, json={"url": "https://fixture.test/image.png", "fileId": "fixture"})

    monkeypatch.setattr(email_service.smtplib, "SMTP_SSL", SMTP)
    monkeypatch.setattr(email_service.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(httpx, "AsyncClient", UploadClient)
    send = AsyncMock(wraps=email_service.send_email)
    monkeypatch.setattr(email_service, "send_email", send)
    return SimpleNamespace(context=context, own=own, own_bytes=own_bytes, messages=messages,
        smtp_calls=smtp_calls, uploads=uploads, send=send)


async def _invoke(n, tool_name, paths, *, domain=False):
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import communication, email
    from app.tools.runtime import ToolExecutionRequest

    arguments = ({"to": "recipient@fixture.test", "subject": "Report", "body": "Attached.", "attachments": paths}
        if tool_name == "send_email" else {"file_path": paths[-1]})
    request = ToolExecutionRequest(tool_name=tool_name, arguments=arguments, context=n.context)
    if domain:
        from app.services.agent_tool_domains.image_upload import _upload_image
        # Exercise the same trusted context through the lower-level entry too.
        import inspect

        kwargs = {"authority_scope": n.context.workspace_authority_scope} if "authority_scope" in inspect.signature(_upload_image).parameters else {}
        return await _upload_image(n.context.agent_id, n.context.workspace, arguments, **kwargs)
    handler = email.send_email if tool_name == "send_email" else communication.upload_image
    return await adapt_and_call(handler.meta, handler, request)


@pytest.mark.parametrize("entry", ["send_email", "upload_image", "image_domain"])
@pytest.mark.parametrize("path", ["/etc/hosts", "../outside.png", "workspace/escape.png",
    "workspace/foreign.png", "memory/.staging/x.png", "runtime_artifacts/recovery_manifests/private.png"])
async def test_outbound_denies_unreadable_files_without_delivery(outbound_files, entry, path):
    n = outbound_files
    tool_name = "upload_image" if entry == "image_domain" else entry
    result = await _invoke(n, tool_name, [n.own, path], domain=entry == "image_domain")
    assert n.smtp_calls == []
    assert n.messages == []
    assert n.uploads == []
    n.send.assert_not_called()
    assert path in result
    assert "cannot be attached" in result if tool_name == "send_email" else "cannot be uploaded" in result


@pytest.mark.parametrize("entry", ["send_email", "upload_image", "image_domain"])
async def test_outbound_delivers_requester_created_file_bytes(outbound_files, entry):
    from app.services.agent_tool_domains.workspace import _read_file

    n = outbound_files
    readable = _read_file(n.context.workspace, n.own, authority_scope=n.context.workspace_authority_scope)
    assert base64.b64decode(readable.blocks[0].data) == n.own_bytes
    tool_name = "upload_image" if entry == "image_domain" else entry
    result = await _invoke(n, tool_name, [n.own], domain=entry == "image_domain")
    assert "✅" in result
    if tool_name == "send_email":
        assert len(n.messages) == 1
        parts = [part for part in n.messages[0].walk() if part.get_content_disposition() == "attachment"]
        assert [(part.get_filename(), part.get_payload(decode=True)) for part in parts] == [("own.png", n.own_bytes)]
        assert n.uploads == []
    else:
        assert len(n.uploads) == 1
        assert n.uploads[0]["files"] == {"file": ("own.png", n.own_bytes)}
        assert n.smtp_calls == []


@pytest.mark.parametrize("tool_name", ["send_email", "upload_image"])
@pytest.mark.parametrize("selected_root,path", [
    ("workspace/selected-session", "workspace/mine/own.png"),
    ("workspace/mine", "workspace//mine/own.png"),
])
async def test_outbound_respects_exact_session_read_root(outbound_files, tool_name, selected_root, path):
    from app.services.agent_tool_domains.workspace import _read_file

    n = outbound_files
    n.context.permission_profile = {
        "capability_policy_snapshot": {"session_exact_scope": True},
        "writable_roots": [selected_root],
        "readable_roots": [selected_root],
    }
    readable = _read_file(n.context.workspace, path, authority_scope=n.context.workspace_authority_scope,
        permission_profile=n.context.permission_profile)
    assert "auth_or_permission" in str(readable)
    result = await _invoke(n, tool_name, [path])
    assert path in result
    assert "cannot be attached" in result if tool_name == "send_email" else "cannot be uploaded" in result
    assert n.smtp_calls == []
    assert n.uploads == []
    n.send.assert_not_called()
