"""One employee's startup failure must not withhold defaults from later employees."""

from pathlib import Path
from uuid import uuid4

from loguru import logger
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.agent import Agent
from app.models.skill import Skill, SkillFile
from tests.integration.test_web_chat_worker_restart_kernel_recovery import _seed_run

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("failure_point", ["push", "legacy_cleanup"])
async def test_startup_skill_failure_does_not_block_later_agents(
    owner_sessionmaker, monkeypatch, tmp_path, failure_point,
):
    from app.services import agent_manager, skill_seeder

    seed = await _seed_run(owner_sessionmaker, tmp_path)
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENT_DATA_DIR", str(seed["data_root"]))
    monkeypatch.setattr(agent_manager, "settings", settings)
    monkeypatch.setattr(skill_seeder, "async_session", owner_sessionmaker)
    before = "---\nname: Startup Package\n---\nApplication package v1.\n"
    after = "---\nname: Startup Package\n---\nApplication package v2.\n"
    async with owner_sessionmaker() as db:
        db.add_all(Agent(id=uuid4(), tenant_id=seed["tenant_id"], name=f"Later employee {index}",
            creator_id=seed["user_id"], owner_user_id=seed["user_id"]) for index in range(2))
        skill = Skill(name="Startup Package", folder_name="mcp-installer", is_builtin=True, is_default=True)
        skill.files = [SkillFile(path="SKILL.md", content=after)]
        db.add(skill)
        await db.commit()
        skill_id = skill.id
        agents = list(await db.scalars(select(Agent.id)))
    roots = {identity: seed["data_root"] / str(identity) for identity in agents}
    for root in roots.values():
        canonical = root / "skills/mcp-installer/SKILL.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(before)
        (root / "skills/MCP_INSTALLER.md").write_text("Legacy package guidance.")

    push = skill_seeder._push_default_skill_packages_to_agent
    cleanup = skill_seeder.remove_legacy_flat_skill_files
    pushes, cleanups, warnings = [], [], []

    def push_one(*, agent_dir, default_skills):
        pushes.append(Path(agent_dir))
        if failure_point == "push" and len(pushes) == 1:
            raise PermissionError("PRIVATE_FILE_CONTENT_SENTINEL")
        return push(agent_dir=agent_dir, default_skills=default_skills)

    def cleanup_one(agent_dir):
        cleanups.append(Path(agent_dir))
        if failure_point == "legacy_cleanup" and len(cleanups) == 1:
            raise PermissionError("PRIVATE_FILE_CONTENT_SENTINEL")
        return cleanup(agent_dir)

    monkeypatch.setattr(skill_seeder, "_push_default_skill_packages_to_agent", push_one)
    monkeypatch.setattr(skill_seeder, "remove_legacy_flat_skill_files", cleanup_one)
    sink = logger.add(lambda message: warnings.append(str(message)), level="WARNING")
    try:
        await skill_seeder.push_default_skills_to_existing_agents()
        assert set(pushes) == set(roots.values())
        assert len(pushes) == len(roots) and len(pushes) >= 3
        failed = pushes[0]
        assert (failed / "skills/mcp-installer/SKILL.md").read_text() == (
            before if failure_point == "push" else after)
        assert (failed / "skills/MCP_INSTALLER.md").exists()
        for root in pushes[1:]:
            assert (root / "skills/mcp-installer/SKILL.md").read_text() == after
            assert not (root / "skills/MCP_INSTALLER.md").exists()
        assert cleanups == (pushes[1:] if failure_point == "push" else pushes)
        assert len(warnings) == 1
        assert failed.name in warnings[0] and "PermissionError" in warnings[0]
        assert "PRIVATE_FILE_CONTENT_SENTINEL" not in warnings[0]
        print("R20A_STARTUP_ISOLATION", failure_point, "updated_later_agents", len(pushes) - 1)
    finally:
        logger.remove(sink)
        async with owner_sessionmaker() as db:
            await db.delete(await db.get(Skill, skill_id))
            await db.commit()
