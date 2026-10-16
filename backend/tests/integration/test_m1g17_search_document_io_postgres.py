"""Search and document parsers must consume the authorized file bytes."""

import json
import os

import pytest

from tests.integration.test_m1g15_workspace_actions_postgres import read_granted_file  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def input_alias(read_granted_file, alias):  # noqa: F811
    tenant, user, agent, session, root, path, mine, scope, _grant = read_granted_file
    foreign = root / "workspace/theirs/report.txt"
    foreign.parent.mkdir()
    foreign.write_text("PRIVATE_SEARCH_DOCUMENT_SENTINEL")
    assert not scope.can_read("workspace/theirs/report.txt")
    replaced = []

    def replace():
        held = mine.parent.with_name("held") if alias == "parent_symlink" else mine.with_suffix(".held")
        (mine.parent if alias == "parent_symlink" else mine).rename(held)
        if alias == "parent_symlink":
            mine.parent.symlink_to(foreign.parent, target_is_directory=True)
        elif alias == "symlink":
            mine.symlink_to(foreign)
        else:
            os.link(foreign, mine)
        replaced.append(held)

    def restore():
        held = replaced[-1]
        (mine.parent if alias == "parent_symlink" else mine).unlink()
        held.rename(mine.parent if alias == "parent_symlink" else mine)

    from app.tools.runtime import ToolExecutionContext

    context = ToolExecutionContext(agent_id=agent, user_id=user.id, tenant_id=str(tenant), workspace=root,
        session_id=str(session), workspace_authority_scope=scope)
    return root, path, mine, foreign, scope, context, replace, restore, replaced


def _error(result):
    return json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
@pytest.mark.parametrize("native", [False, True])
async def test_registered_search_never_returns_bytes_from_a_replaced_resource(input_alias, monkeypatch, native):
    from app.services.agent_tool_domains import workspace
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.filesystem import grep_search
    from app.tools.runtime import ToolExecutionRequest

    root, path, _mine, foreign, scope, context, replace, restore, replaced = input_alias
    original_run = workspace.subprocess.run
    original_allow = workspace._authority_allows_path
    native_calls = []
    assert workspace.shutil.which("rg"), "The native rg regression needs the installed executable"

    def native_race(command, **kwargs):
        native_calls.append(command)
        replace()
        try:
            return original_run(command, **kwargs)
        finally:
            restore()

    def checked_scope(loaded, resource_path, **kwargs):
        allowed = original_allow(loaded, resource_path, **kwargs)
        if allowed and loaded is scope and resource_path == path and not replaced:
            replace()
        return allowed

    if native:
        monkeypatch.setattr(workspace.subprocess, "run", native_race)
    else:
        monkeypatch.setattr(workspace.shutil, "which", lambda _name: None)
        monkeypatch.setattr(workspace, "_authority_allows_path", checked_scope)
    result = await adapt_and_call(grep_search.meta, grep_search, ToolExecutionRequest(tool_name="grep_search",
        arguments={"pattern": ".", "root": "workspace"}, context=context))
    assert replaced and foreign.read_text() == "PRIVATE_SEARCH_DOCUMENT_SENTINEL"
    assert "PRIVATE_SEARCH_DOCUMENT_SENTINEL" not in str(result)
    if native:
        assert native_calls and "Other owner's original contents" in str(result)
    else:
        payload = _error(result)
        assert payload["http_status"] == 403 and payload["reason_code"] == "workspace_resource_alias_forbidden"


@pytest.mark.parametrize("alias", ["symlink", "hardlink", "parent_symlink"])
@pytest.mark.parametrize("phase", ["authority", "parser"])
async def test_registered_document_parser_consumes_the_authorized_snapshot(input_alias, monkeypatch, phase):
    from app.services.agent_tool_domains import workspace
    from app.services.document_conversion import DocumentConversionService
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers.filesystem import read_document
    from app.tools.runtime import ToolExecutionRequest

    _root, path, _mine, foreign, scope, context, replace, _restore, replaced = input_alias
    original_allow = workspace._authority_allows_path
    original_convert = DocumentConversionService._convert_source
    parser_calls = []

    def checked_scope(loaded, resource_path, **kwargs):
        allowed = original_allow(loaded, resource_path, **kwargs)
        if phase == "authority" and allowed and loaded is scope and resource_path == path and not replaced:
            replace()
        return allowed

    def parser_race(self, source_path, data, mime, source_uri):
        parser_calls.append(data)
        if phase == "parser":
            assert data == b"Other owner's original contents"
            replace()
        return original_convert(self, source_path, data, mime, source_uri)

    monkeypatch.setattr(workspace, "_authority_allows_path", checked_scope)
    monkeypatch.setattr(DocumentConversionService, "_convert_source", parser_race)
    result = await adapt_and_call(read_document.meta, read_document, ToolExecutionRequest(tool_name="read_document",
        arguments={"path": path}, context=context))
    assert replaced and foreign.read_text() == "PRIVATE_SEARCH_DOCUMENT_SENTINEL"
    assert "PRIVATE_SEARCH_DOCUMENT_SENTINEL" not in str(result)
    if phase == "authority":
        assert not parser_calls
        payload = _error(result)
        assert payload["http_status"] == 403 and payload["reason_code"] == "workspace_resource_alias_forbidden"
    else:
        assert parser_calls == [b"Other owner's original contents"]
        assert "Other owner's original contents" in str(result)
