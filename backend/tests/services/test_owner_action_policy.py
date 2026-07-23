from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_default_owner_action_policy_is_exact_and_typed() -> None:
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_default_owner_action_policy,
    )

    policy = build_default_owner_action_policy(
        agent_id=uuid4(),
        tenant_id=uuid4(),
    )

    assert policy.valid is True
    assert policy.zone_for(ACTION_EXTERNAL_EFFECT) == CharterZone.CONFIRM_FIRST
    assert policy.zone_for(ACTION_LOCAL_READ) == CharterZone.FULL_AUTHORITY
    assert policy.zone_for(ACTION_LOCAL_WRITE) == CharterZone.FULL_AUTHORITY
    assert policy.zone_for("send external message about credentials") == CharterZone.CONFIRM_FIRST


def test_owner_action_policy_prompt_section_exposes_complete_typed_runtime_boundary() -> None:
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        build_owner_action_policy,
        render_owner_action_policy_prompt_section,
    )

    policy = build_owner_action_policy(
        agent_id=uuid4(),
        tenant_id=uuid4(),
        actions={
            ACTION_EXTERNAL_EFFECT: CharterZone.FULL_AUTHORITY,
            ACTION_LOCAL_READ: CharterZone.FULL_AUTHORITY,
            ACTION_LOCAL_WRITE: CharterZone.NEVER_DO,
        },
        version=6,
        content_hash="hash-v6",
    )

    section = render_owner_action_policy_prompt_section(policy)

    assert "## Owner Action Policy" in section
    assert "tool.external_effect: full_authority" in section
    assert "tool.local_read: full_authority" in section
    assert "tool.local_write: never_do" in section
    assert 'version="6"' in section
    assert 'content_hash="hash-v6"' in section
    assert "natural-language examples do not change these zones" in section


@pytest.mark.parametrize(
    "actions",
    [
        {
            "tool.external_effect": "confirm_first",
            "tool.local_read": "full_authority",
        },
        {
            "tool.external_effect": "confirm_first",
            "tool.local_read": "full_authority",
            "tool.local_write": "full_authority",
            "tool.unknown": "never_do",
        },
        {
            "tool.external_effect": "sometimes",
            "tool.local_read": "full_authority",
            "tool.local_write": "full_authority",
        },
    ],
)
def test_owner_action_policy_rejects_missing_extra_or_unknown_machine_contract(actions) -> None:
    from app.services.owner_action_policy import OwnerActionPolicyValidationError, validate_owner_action_policy_actions

    with pytest.raises(OwnerActionPolicyValidationError):
        validate_owner_action_policy_actions(actions)


@pytest.mark.asyncio
async def test_missing_legacy_policy_is_backfilled_as_immutable_config_revision(monkeypatch) -> None:
    from app.services.owner_action_policy import (
        OWNER_ACTION_POLICY_ENTITY_TYPE,
        load_owner_action_policy,
    )

    agent_id = uuid4()
    tenant_id = uuid4()
    saved = []

    class _Result:
        def scalar_one_or_none(self):
            return None

    class _DB:
        async def execute(self, _statement):
            return _Result()

        def begin_nested(self):
            class _Nested:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, _exc_type, _exc, _tb):
                    return False

            return _Nested()

    async def fake_save_revision(_db, **kwargs):
        saved.append(kwargs)
        return SimpleNamespace(
            id=uuid4(),
            version=1,
            content_hash="policy-hash",
            content=kwargs["content"],
            change_source=kwargs["change_source"],
        )

    monkeypatch.setattr("app.services.owner_action_policy.save_revision", fake_save_revision)

    policy = await load_owner_action_policy(
        _DB(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        create_default=True,
    )

    assert policy.valid is True
    assert policy.version == 1
    assert saved[0]["entity_type"] == OWNER_ACTION_POLICY_ENTITY_TYPE
    assert saved[0]["entity_id"] == agent_id
    assert saved[0]["tenant_id"] == tenant_id
    assert saved[0]["change_source"] == "migration"
    assert "legacy agent" in saved[0]["change_message"].lower()


@pytest.mark.asyncio
async def test_invalid_persisted_policy_fails_closed_without_rewriting_history() -> None:
    from app.services.action_preflight import CharterZone
    from app.services.owner_action_policy import ACTION_EXTERNAL_EFFECT, load_owner_action_policy

    revision = SimpleNamespace(
        id=uuid4(),
        version=7,
        content_hash="broken-hash",
        content={
            "schema": "hive.owner_action_policy.v1",
            "actions": {
                "tool.external_effect": "full_authority",
            },
        },
        change_source="user",
    )

    class _Result:
        def scalar_one_or_none(self):
            return revision

    class _DB:
        async def execute(self, _statement):
            return _Result()

    policy = await load_owner_action_policy(
        _DB(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        create_default=True,
    )

    assert policy.valid is False
    assert policy.error_code == "invalid_policy_contract"
    assert policy.zone_for(ACTION_EXTERNAL_EFFECT) == CharterZone.NEVER_DO


@pytest.mark.asyncio
async def test_policy_update_requires_expected_active_version_and_records_actor(monkeypatch) -> None:
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        OwnerActionPolicyVersionConflict,
        save_owner_action_policy,
    )

    agent_id = uuid4()
    tenant_id = uuid4()
    actor_id = uuid4()
    active = SimpleNamespace(version=2)
    saved = []

    async def fake_active(*_args, **_kwargs):
        return active

    async def fake_save_revision(_db, **kwargs):
        saved.append(kwargs)
        return SimpleNamespace(
            id=uuid4(),
            version=3,
            content_hash="policy-v3",
            content=kwargs["content"],
            change_source=kwargs["change_source"],
        )

    monkeypatch.setattr("app.services.owner_action_policy._active_policy_revision", fake_active)
    monkeypatch.setattr("app.services.owner_action_policy.save_revision", fake_save_revision)
    actions = {
        ACTION_EXTERNAL_EFFECT: "full_authority",
        ACTION_LOCAL_READ: "full_authority",
        ACTION_LOCAL_WRITE: "confirm_first",
    }

    with pytest.raises(OwnerActionPolicyVersionConflict):
        await save_owner_action_policy(
            object(),
            agent_id=agent_id,
            tenant_id=tenant_id,
            actions=actions,
            changed_by_user_id=actor_id,
            expected_version=1,
        )
    assert saved == []

    policy = await save_owner_action_policy(
        object(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        actions=actions,
        changed_by_user_id=actor_id,
        expected_version=2,
    )

    assert policy.version == 3
    assert saved[0]["tenant_id"] == tenant_id
    assert saved[0]["changed_by_user_id"] == actor_id
    assert saved[0]["change_source"] == "user"


@pytest.mark.asyncio
async def test_policy_rollback_restores_historical_content_as_new_revision(monkeypatch) -> None:
    from app.services.owner_action_policy import (
        ACTION_EXTERNAL_EFFECT,
        ACTION_LOCAL_READ,
        ACTION_LOCAL_WRITE,
        rollback_owner_action_policy,
    )

    agent_id = uuid4()
    tenant_id = uuid4()
    actor_id = uuid4()
    target_id = uuid4()
    target = SimpleNamespace(
        id=target_id,
        version=1,
        content_hash="policy-v1",
        content={
            "schema": "hive.owner_action_policy.v1",
            "actions": {
                ACTION_EXTERNAL_EFFECT: "confirm_first",
                ACTION_LOCAL_READ: "full_authority",
                ACTION_LOCAL_WRITE: "full_authority",
            },
        },
        change_source="migration",
    )
    saved = []

    async def fake_active(*_args, **_kwargs):
        return SimpleNamespace(version=3)

    class _Result:
        def scalar_one_or_none(self):
            return target

    class _DB:
        async def execute(self, _statement):
            return _Result()

    async def fake_save_revision(_db, **kwargs):
        saved.append(kwargs)
        return SimpleNamespace(
            id=uuid4(),
            version=4,
            content_hash="policy-v4",
            content=kwargs["content"],
            change_source=kwargs["change_source"],
        )

    monkeypatch.setattr("app.services.owner_action_policy._active_policy_revision", fake_active)
    monkeypatch.setattr("app.services.owner_action_policy.save_revision", fake_save_revision)

    policy = await rollback_owner_action_policy(
        _DB(),
        agent_id=agent_id,
        tenant_id=tenant_id,
        target_version=1,
        changed_by_user_id=actor_id,
        expected_version=3,
        reason="Restore approved policy",
    )

    assert policy.version == 4
    assert policy.zone_for(ACTION_EXTERNAL_EFFECT).value == "confirm_first"
    assert saved[0]["change_source"] == "rollback"
    assert saved[0]["changed_by_user_id"] == actor_id
    assert saved[0]["rollback_of_revision_id"] == target_id
    assert saved[0]["force_revision"] is True
    assert saved[0]["change_message"] == "Restore approved policy"
