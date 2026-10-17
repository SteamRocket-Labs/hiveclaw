"""Native filesystem readers cannot bypass private-session source selection."""

import pytest

from app.models.chat_session import ChatSession
from app.models.capability_policy import CapabilityPolicy
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g38_session_recall_actor_postgres import _sources
from tests.memory.test_retrieval_pipeline import _write_t2_package

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def _files(n, owner_sessionmaker, kind):
    own, foreign = await _sources(n, owner_sessionmaker, source="ledger")
    async with owner_sessionmaker() as db:
        db.add(CapabilityPolicy(tenant_id=n.tenant, capability="workspace.file.read", allowed=True,
            requires_approval=False))
        await db.commit()
    if kind == "t0":
        paths = {sid: next((n.root / f"memory/t0/sessions/{sid}").rglob("source.md")) for sid in (own, foreign)}
    else:
        paths = {}
        for sid, marker in [(own, "OWN_PRIVATE_EVIDENCE"), (foreign, "OTHER_REQUESTER_PRIVATE_EVIDENCE")]:
            if kind == "t2":
                _write_t2_package(n.root.parent, n.agent, session_id=str(sid), summary=marker)
                paths[sid] = next((n.root / f"memory/t2/sessions/{sid}").rglob("summary.md"))
            else:
                paths[sid] = n.root / f"memory/sessions/{sid}/history.md"
                paths[sid].parent.mkdir(parents=True)
                paths[sid].write_text(marker)
    return own, foreign, {sid: path.relative_to(n.root).as_posix() for sid, path in paths.items()}


async def _call(n, operation, path, **kwargs):
    tool, _, mode = operation.partition(".")
    args = {"root" if tool in {"glob_search", "grep_search"} else "path": path, **kwargs}
    if mode:
        args["mode"] = mode
    return str(await n.service.execute(tool, args, agent_id=n.agent, user_id=n.user,
        session_id=str(n.session), runtime_task_id=str(n.run), emit_runtime_hooks=False))


@pytest.mark.parametrize("kind", ["t0", "t2", "legacy"])
@pytest.mark.parametrize("operation", ["read_file", "read_document", "fs_read.text", "fs_read.document",
    "list_files", "glob_search", "grep_search", "fs_read.glob", "fs_read.grep"])
async def test_all_registered_readers_filter_private_session_sources(
    native_workspace, owner_sessionmaker, kind, operation,  # noqa: F811
):
    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, kind)
    before = {path: (n.root / path).read_bytes() for path in paths.values()}
    listing = operation in {"list_files", "glob_search", "fs_read.glob"}
    searching = operation in {"grep_search", "fs_read.grep"}
    if listing or searching:
        root = f"memory/{'t0/sessions' if kind == 't0' else 't2/sessions' if kind == 't2' else 'sessions'}"
        args = {"pattern": "PRIVATE_EVIDENCE" if searching else "**/*"} if operation != "list_files" else {}
        result = await _call(n, operation, root, **args)
        assert str(own) in result, result
        assert str(foreign) not in result and "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result, result
        assert "approval_required" not in result, result
        if kind == "t0":
            assert "UNKNOWN_PRIVATE_EVIDENCE" not in result, result
            if operation == "list_files":
                assert "sessions/ (1 items)" in await _call(n, operation, "memory/t0")
    else:
        assert "OWN_PRIVATE_EVIDENCE" in await _call(n, operation, paths[own])
        result = await _call(n, operation, paths[foreign])
        assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "auth_or_permission" in result, result
        missing = await _call(n, operation, paths[foreign] + ".missing")
        assert "auth_or_permission" in missing and "not_found" not in missing, missing
    assert all((n.root / path).read_bytes() == body for path, body in before.items())


@pytest.mark.parametrize("case", ["admin", "source_transfer", "shared_worker", "shared_source"])
async def test_each_tool_call_uses_current_requester_and_source_audience(
    native_workspace, owner_sessionmaker, case,  # noqa: F811
):
    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    assert "OWN_PRIVATE_EVIDENCE" in await _call(n, "read_file", paths[own])
    async with owner_sessionmaker() as db:
        if case == "admin":
            (await db.get(User, n.user)).role = "org_admin"
        elif case == "source_transfer":
            (await db.get(ChatSession, own)).user_id = n.foreign
        else:
            (await db.get(ChatSession, n.session if case == "shared_worker" else own)).audience_kind = "group"
        await db.commit()
    result = await _call(n, "read_file", paths[own])
    assert ("OWN_PRIVATE_EVIDENCE" in result) is (case == "admin"), result
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in await _call(n, "read_file", paths[foreign])


@pytest.mark.parametrize("case", ["legacy_logs", "file_alias", "directory_alias"])
async def test_unbound_legacy_logs_and_aliases_do_not_supply_private_bytes(
    native_workspace, owner_sessionmaker, case,  # noqa: F811
):
    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    log = n.root / "logs/2026-10-01/behavior/chat-0100-unbound.md"
    log.parent.mkdir(parents=True)
    log.write_text(f"---\nsession_id: {own}\n---\nUNBOUND_PRIVATE_LOG_BODY")
    skill = n.root / "skills/independent.md"
    skill.parent.mkdir(exist_ok=True)
    skill.write_text("INDEPENDENT_SKILL_BODY")
    alias = n.root / "skills/borrowed.md"
    alias.symlink_to(n.root / paths[foreign])
    (n.root / "skills/borrowed-dir").symlink_to((n.root / paths[foreign]).parent)
    assert "INDEPENDENT_SKILL_BODY" in await _call(n, "read_file", "skills/independent.md")
    if case == "file_alias":
        for operation in ["read_file", "read_document", "fs_read.text", "fs_read.document"]:
            result = await _call(n, operation, "skills/borrowed.md")
            assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "auth_or_permission" in result, result
    elif case == "directory_alias":
        for operation in ["list_files", "glob_search", "grep_search"]:
            result = await _call(n, operation, "skills/borrowed-dir", **({"pattern": "**/*" if operation == "glob_search"
                else "PRIVATE_EVIDENCE"} if operation != "list_files" else {}))
            assert "source.md" not in result and "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result, result
            assert "auth_or_permission" in result, result
    else:
        for operation in ["read_file", "read_document", "fs_read.text", "fs_read.document"]:
            result = await _call(n, operation, log.relative_to(n.root).as_posix())
            assert "UNBOUND_PRIVATE_LOG_BODY" not in result and "auth_or_permission" in result, result
        assert "UNBOUND_PRIVATE_LOG_BODY" not in await _call(n, "grep_search", "", pattern="UNBOUND_.*_BODY")
        assert "chat-0100-unbound.md" not in await _call(n, "glob_search", "", pattern="**/*.md")
        assert "chat-0100-unbound.md" not in await _call(n, "list_files", "logs/2026-10-01/behavior")
    assert log.is_file() and (n.root / paths[foreign]).is_file() and own != foreign


async def test_source_alias_cannot_change_between_authority_and_descriptor_open(
    native_workspace, owner_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.services.agent_tool_domains import workspace

    n = native_workspace
    own, foreign, paths = await _files(n, owner_sessionmaker, "t0")
    alias = n.root / "skills/switching.md"
    alias.parent.mkdir(exist_ok=True)
    alias.symlink_to(n.root / paths[own])
    original = workspace._read_receipted_workspace_bytes

    def change_target(root, path, scope, authority_path):
        alias.unlink()
        alias.symlink_to(n.root / paths[foreign])
        return original(root, path, scope, authority_path)

    monkeypatch.setattr(workspace, "_read_receipted_workspace_bytes", change_target)
    result = await _call(n, "read_file", "skills/switching.md")
    assert "OTHER_REQUESTER_PRIVATE_EVIDENCE" not in result and "auth_or_permission" in result, result
