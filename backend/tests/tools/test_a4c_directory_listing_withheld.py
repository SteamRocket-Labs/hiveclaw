"""A4c: a directory listing names withheld entries instead of hiding them silently."""

from uuid import uuid4

from app.services.agent_tool_domains.workspace import _list_files


def _scope(allowed_paths=frozenset()):
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope

    return WorkspaceAuthorityScope(
        agent_id=uuid4(),
        user_id=uuid4(),
        root_session_id=uuid4(),
        allowed_paths=frozenset(allowed_paths),
        operator_view=False,
        authority_source="resource_owner",
        known_paths=frozenset(),
        action_permissions=frozenset(),
    )


def test_listing_says_how_many_entries_were_withheld(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mine.md").write_text("authorized")
    (workspace / "foreign.md").write_text("not authorized in this work")

    # The authority scope admits only mine.md; foreign.md must not vanish.
    scope = _scope(allowed_paths=frozenset({"workspace/mine.md"}))
    result = _list_files(tmp_path, "workspace", authority_scope=scope)

    assert "mine.md" in result
    assert "foreign.md" not in result
    assert "有 1 项暂时无法显示，已略过。" in result


def test_listing_without_withheld_entries_has_no_note(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mine.md").write_text("authorized")

    scope = _scope(allowed_paths=frozenset({"workspace/mine.md"}))
    result = _list_files(tmp_path, "workspace", authority_scope=scope)

    assert "mine.md" in result
    assert "暂时无法显示" not in result
