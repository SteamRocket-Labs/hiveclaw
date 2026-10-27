"""Audited human inspection never becomes a recipient's source grant."""

from contextlib import nullcontext

import pytest
from sqlalchemy import select

from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call, shared_memory  # noqa: F401
from tests.integration.test_m4d_group_memory_postgres import group_memory, save_group  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def derived_group_artifact(
    group_memory,  # noqa: F811
    app_user_sessionmaker,
    owner_sessionmaker,
):
    from app.database import pin_rls_tenant_context
    from app.models.chat_artifact import ChatArtifact
    from app.models.user import User

    g = group_memory
    n = g.n
    await save_group(g, app_user_sessionmaker)
    loaded, _ = await memory_call(
        n,
        app_user_sessionmaker,
        "load_memory",
        {"ids": [g.entry_id]},
        user=n.foreign,
        session=g.s.second_session,
        run=g.s.second_run,
    )
    assert "GROUP_RELEASE_MARKER" in loaded
    path = "workspace/group-release.md"
    await memory_call(
        n,
        app_user_sessionmaker,
        "write_file",
        {"path": path, "content": "Release note uses GROUP_RELEASE_MARKER."},
        user=n.foreign,
        session=g.s.second_session,
        run=g.s.second_run,
        include_history=True,
    )
    assert (n.root / path).read_text() == "Release note uses GROUP_RELEASE_MARKER."
    g.members.remove("memory-author")
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        artifact = await db.scalar(
            select(ChatArtifact).where(ChatArtifact.agent_id == n.agent, ChatArtifact.path == path)
        )
        assert artifact is not None and artifact.owner_user_id == n.foreign
        return n, artifact.id, artifact.root_session_id or artifact.session_id


@pytest.mark.parametrize("surface", ["content", "download"])
@pytest.mark.parametrize("purpose", ["ordinary", "inspection", "recipient"])
async def test_artifact_source_purpose_preserves_inspection_and_recipient_boundaries(
    derived_group_artifact, purpose, surface
):
    from app.core.source_read_purpose import source_recipient_read

    n, artifact, session = derived_group_artifact
    parameters = (
        {}
        if purpose == "ordinary"
        else {
            "operator_view": True,
            "operator_reason": "Inspect the group-derived release note",
        }
    )
    scope = source_recipient_read(session_id=session, user_id=n.user) if purpose == "recipient" else nullcontext()
    with scope:
        response = await n.client.get(f"/agents/{n.agent}/files/artifacts/{artifact}/{surface}", params=parameters)
    if purpose == "inspection":
        assert response.status_code == 200, response.text
        if surface == "content":
            assert "GROUP_RELEASE_MARKER" in response.json()["content"]
            assert response.json()["authority_source"] == "scoped_business_admin"
        else:
            assert response.content == b"Release note uses GROUP_RELEASE_MARKER."
    else:
        assert response.status_code == 403, response.text
        assert "GROUP_RELEASE_MARKER" not in response.text
