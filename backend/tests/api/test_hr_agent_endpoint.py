"""Tests for the HR agent system endpoint — GET /agents/system/hr."""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import MultipleResultsFound


def test_internal_system_agents_excluded_from_list():
    """The HR_AGENT_NAME constant should match the filter in list_agents."""
    from app.api.agents import HR_AGENT_NAME

    assert HR_AGENT_NAME == "__system_hr__"


def test_agent_class_internal_system_literal_is_valid():
    """internal_system must be a valid AgentClass literal."""
    from app.schemas.schemas import AgentClass

    # AgentClass is a Literal type — verify it includes internal_system
    import typing

    args = typing.get_args(AgentClass)
    assert "internal_system" in args
    assert "internal_tenant" in args


def test_hr_template_version_tracks_current_session_and_company_kb_contract() -> None:
    """Changing the HR creation contract must force existing HR workspaces to resync."""
    from app.api import agents as agents_api

    assert agents_api.HR_TEMPLATE_VERSION.startswith("hr-flow-v6-")
    assert "current-session-company-kb" in agents_api.HR_TEMPLATE_VERSION


class _FakeScalars:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self._rows


class _FakeResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> SimpleNamespace | None:
        if len(self._rows) > 1:
            raise MultipleResultsFound("multiple HR agents")
        return self._rows[0] if self._rows else None

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeDb:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.commits = 0

    async def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self.rows)

    async def commit(self) -> None:
        self.commits += 1


class _FakeAgentManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _agent_dir(self, agent_id: uuid.UUID) -> Path:
        path = self.root / str(agent_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def initialize_agent_files(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("existing canonical HR agent should not be initialized")


@pytest.mark.asyncio
async def test_get_hr_agent_uses_oldest_canonical_when_duplicate_rows_exist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Duplicate legacy HR agents must not make GET /agents/system/hr raise 500."""
    from app.api import agents as agents_api
    import app.services.agent_manager as agent_manager_module

    tenant_id = uuid.uuid4()
    older = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=agents_api.HR_AGENT_NAME,
        agent_class="internal_system",
        status="idle",
        primary_model_id=uuid.uuid4(),
    )
    newer = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=agents_api.HR_AGENT_NAME,
        agent_class="internal_system",
        status="idle",
        primary_model_id=uuid.uuid4(),
    )
    fake_db = _FakeDb([older, newer])
    monkeypatch.setattr(agent_manager_module, "agent_manager", _FakeAgentManager(tmp_path))

    result = await agents_api.get_or_create_hr_agent(
        current_user=SimpleNamespace(tenant_id=tenant_id),
        db=fake_db,  # type: ignore[arg-type]
    )

    assert result == {"id": str(older.id), "name": agents_api.HR_AGENT_NAME, "status": "idle"}
    assert fake_db.commits == 0


@pytest.mark.asyncio
async def test_get_hr_agent_refreshes_existing_workspace_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Existing HR workspaces must be upgraded when the template contract changes."""
    from app.api import agents as agents_api
    import app.services.agent_manager as agent_manager_module

    tenant_id = uuid.uuid4()
    hr_agent = SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=agents_api.HR_AGENT_NAME,
        agent_class="internal_system",
        status="idle",
        primary_model_id=uuid.uuid4(),
    )
    agent_dir = tmp_path / str(hr_agent.id)
    agent_dir.mkdir(parents=True)
    (agent_dir / "soul.md").write_text(
        "OLD HR FLOW: run a fixed five-round interview before creation.\n",
        encoding="utf-8",
    )
    (agent_dir / "HEARTBEAT.md").write_text("OLD HEARTBEAT\n", encoding="utf-8")
    legacy_hr_guide = agent_dir / "skills" / "hr-guide" / "SKILL.md"
    legacy_hr_guide.parent.mkdir(parents=True)
    legacy_hr_guide.write_text("# Legacy HR Guide\n", encoding="utf-8")

    fake_db = _FakeDb([hr_agent])
    monkeypatch.setattr(agent_manager_module, "agent_manager", _FakeAgentManager(tmp_path))

    result = await agents_api.get_or_create_hr_agent(
        current_user=SimpleNamespace(tenant_id=tenant_id),
        db=fake_db,  # type: ignore[arg-type]
    )

    assert result == {"id": str(hr_agent.id), "name": agents_api.HR_AGENT_NAME, "status": "idle"}
    soul = (agent_dir / "soul.md").read_text(encoding="utf-8")
    assert "dynamic rounds, mandatory gates" in soul
    assert "fixed five-round interview" not in soul
    assert (agent_dir / ".hr_template_version").read_text(encoding="utf-8") == agents_api.HR_TEMPLATE_VERSION
    assert (agent_dir / ".soul.md.pre-hr-template.bak").read_text(encoding="utf-8").startswith("OLD HR FLOW")
    assert not legacy_hr_guide.exists()
    retired = list((agent_dir / "skills" / ".retired" / "hr-guide").glob("*/SKILL.md"))
    assert len(retired) == 1
    assert retired[0].read_text(encoding="utf-8") == "# Legacy HR Guide\n"
