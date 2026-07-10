from types import SimpleNamespace
from uuid import uuid4


def test_agent_revision_applier_restores_config_and_lifecycle() -> None:
    from app.services.ai_asset_adapters import apply_agent_content

    agent = SimpleNamespace(
        name="Current",
        role_description="Current role",
        deleted_at=object(),
        deactivated_at=object(),
        deactivation_reason="deleted",
    )

    apply_agent_content(
        agent,
        {
            "asset_type": "agent",
            "config": {"name": "Restored", "role_description": "Restored role"},
            "control": {"lifecycle_status": "active"},
        },
    )

    assert agent.name == "Restored"
    assert agent.role_description == "Restored role"
    assert agent.deleted_at is None
    assert agent.deactivated_at is None
    assert agent.deactivation_reason is None


def test_agent_projection_contains_config_not_runtime_usage() -> None:
    from app.services.ai_asset_adapters import project_agent

    tenant_id = uuid4()
    owner_id = uuid4()
    model_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        name="Researcher",
        role_description="Research",
        bio=None,
        avatar_url=None,
        owner_user_id=owner_id,
        sponsor_user_id=owner_id,
        creator_id=owner_id,
        primary_model_id=model_id,
        fallback_model_id=None,
        agent_type="native",
        agent_class="internal_tenant",
        security_zone="standard",
        execution_mode="standard",
        smart_model_routing=None,
        context_window_size=100,
        max_tool_rounds=200,
        max_triggers=20,
        min_poll_interval_min=5,
        webhook_rate_limit=5,
        timezone="Asia/Shanghai",
        subagent_evolution_auto_approve=False,
        deleted_at=None,
        deactivated_at=None,
        tokens_used_total=999,
    )

    projection = project_agent(agent)

    assert projection.native_key == f"agent:{agent.id}"
    assert projection.owner_id == owner_id
    assert projection.dependencies == [str(model_id)]
    assert projection.content["config"]["max_tool_rounds"] == 200
    assert projection.content["control"] == {"lifecycle_status": "active"}
    assert "tokens_used_total" not in projection.content["config"]


def test_skill_projection_is_file_order_stable_and_marks_explicit_owner() -> None:
    from app.services.ai_asset_adapters import project_skill

    owner_id = uuid4()
    skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Deploy",
        description="Deploy safely",
        category="engineering",
        icon="D",
        folder_name="deploy",
        is_builtin=False,
        files=[
            SimpleNamespace(path="scripts/z.py", content="z"),
            SimpleNamespace(path="SKILL.md", content="instructions"),
        ],
    )

    projection = project_skill(skill, owner_user_id=owner_id)

    assert [item["path"] for item in projection.content["files"]] == ["SKILL.md", "scripts/z.py"]
    assert projection.owner_type == "user"
    assert projection.owner_id == owner_id
    assert projection.trust_state == "trusted"
    assert projection.content["control"] == {
        "lifecycle_status": "active",
        "trust_state": "trusted",
        "admission_state": "admitted",
    }


def test_skill_projection_can_capture_deleted_lifecycle_as_revision_content() -> None:
    from app.services.ai_asset_adapters import project_skill

    owner_id = uuid4()
    skill = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Deploy",
        description="Deploy safely",
        category="engineering",
        icon="D",
        folder_name="deploy",
        is_builtin=False,
        files=[SimpleNamespace(path="SKILL.md", content="instructions")],
    )

    projection = project_skill(skill, owner_user_id=owner_id, lifecycle_status="deleted")

    assert projection.lifecycle_status == "deleted"
    assert projection.content["control"]["lifecycle_status"] == "deleted"


def test_workspace_skill_projection_is_portable_and_round_trips(tmp_path) -> None:
    from app.services.ai_asset_adapters import apply_workspace_skill_content, project_workspace_skill

    tenant_id = uuid4()
    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    skill_dir = workspace / "skills" / "deploy-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Deploy Review\ndescription: Review deployments.\n---\n\n# Deploy Review\n",
        encoding="utf-8",
    )
    (skill_dir / "references.md").write_text("evidence", encoding="utf-8")

    projection = project_workspace_skill(
        workspace=workspace,
        folder_name="deploy-review",
        tenant_id=tenant_id,
        agent_id=agent_id,
        lifecycle_status="active",
        evolution_state="provisional",
    )

    assert projection.native_key == f"skill:agent:{agent_id}:deploy-review"
    assert projection.native_locator == {
        "kind": "agent_workspace",
        "agent_id": str(agent_id),
        "folder_name": "deploy-review",
    }
    assert projection.display_name == "Deploy Review"
    assert [item["path"] for item in projection.content["files"]] == ["SKILL.md", "references.md"]
    assert str(tmp_path) not in str(projection.native_locator)

    (skill_dir / "SKILL.md").write_text("changed", encoding="utf-8")
    (skill_dir / "extra.txt").write_text("remove me", encoding="utf-8")
    apply_workspace_skill_content(projection.content, workspace=workspace, folder_name="deploy-review")

    assert (skill_dir / "SKILL.md").read_text(encoding="utf-8").startswith("---\nname: Deploy Review")
    assert not (skill_dir / "extra.txt").exists()


def test_subagent_projection_round_trips_native_definition(tmp_path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.services.ai_asset_adapters import apply_subagent_content, project_subagent

    tenant_id = uuid4()
    owner_id = uuid4()
    spec = SubagentSpec(name="reviewer", description="Review changes", system_prompt="Verify evidence.")
    projection = project_subagent(
        spec,
        tenant_id=tenant_id,
        scope="tenant",
        owner_id=owner_id,
        base_dir=tmp_path,
    )

    applied = apply_subagent_content(projection.content, locator=projection.native_locator)

    assert applied.name == "reviewer"
    assert applied.system_prompt == "Verify evidence."
    assert projection.content["control"] == {
        "lifecycle_status": "active",
        "trust_state": "trusted",
        "admission_state": "admitted",
    }
    assert (tmp_path / "reviewer.md").exists()


def test_legacy_tenant_subagent_without_owner_enters_review_queue(tmp_path) -> None:
    from app.agents.subagent import SubagentSpec
    from app.services.ai_asset_adapters import project_subagent

    projection = project_subagent(
        SubagentSpec(name="reviewer", description="Review", system_prompt="Review."),
        tenant_id=uuid4(),
        scope="tenant",
        owner_id=None,
        base_dir=tmp_path,
    )

    assert projection.lifecycle_status == "quarantined"
    assert projection.trust_state == "review_required"
    assert projection.admission_state == "review_required"
    assert projection.quarantine_reason == "subagent owner is unknown"


def test_file_native_snapshot_restores_existing_and_absent_subagent_files(tmp_path) -> None:
    from app.services.ai_asset_adapters import capture_file_native_state, restore_file_native_state

    target = tmp_path / "reviewer.md"
    target.write_bytes(b"original")
    record = SimpleNamespace(
        asset_type="subagent",
        native_locator_json={"base_dir": str(tmp_path), "name": "reviewer"},
    )
    existing = capture_file_native_state(record)
    target.write_bytes(b"changed")
    restore_file_native_state(existing)
    assert target.read_bytes() == b"original"

    target.unlink()
    absent = capture_file_native_state(record)
    target.write_bytes(b"created during failed rollback")
    restore_file_native_state(absent)
    assert not target.exists()


def test_external_projection_never_treats_revoked_snapshot_as_trusted() -> None:
    from app.services.ai_asset_adapters import project_external_capability

    snapshot = SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        snapshot_key="plugin:one",
        normalized_name="Plugin One",
        status="revoked",
        source_hash="abc",
        source_format="cc_plugin",
        source_uri="repo://one",
        source_ref="v1",
        admission_class="governed_runtime",
        component_manifest_json={"dependencies": ["base"]},
        governance_projection_json={},
        approved_by_user_id=uuid4(),
    )

    projection = project_external_capability(snapshot)

    assert projection.lifecycle_status == "revoked"
    assert projection.trust_state == "revoked"
    assert projection.admission_state == "revoked"
    assert projection.content["control"] == {
        "lifecycle_status": "revoked",
        "trust_state": "revoked",
        "admission_state": "revoked",
    }
