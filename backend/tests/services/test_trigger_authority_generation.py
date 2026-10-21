"""Revocation generations are execution facts, not editable routine settings."""

from types import SimpleNamespace
from uuid import uuid4

from app.services.trigger_resource_authority import (
    apply_trigger_authority_reclaim, preserve_trigger_authority, stamp_trigger_authority,
    trigger_authority_generation, trigger_fire_authority_block_reason,
)


def test_reclaim_and_config_replacement_cannot_restore_an_old_generation():
    owner, trigger_id = uuid4(), uuid4()
    trigger = SimpleNamespace(id=trigger_id, config={"created_by": str(owner), "authority_state": "quarantined",
        "_authority_generation": 1}, reply_context=None, is_enabled=False)
    apply_trigger_authority_reclaim(trigger, live_owner_id=owner)
    assert trigger_authority_generation(trigger) == 2
    trigger.config = preserve_trigger_authority(trigger, {"minutes": 30, "_authority_generation": 0})
    assert trigger_authority_generation(trigger) == 2
    assert trigger_fire_authority_block_reason(trigger, {"trigger_authority_generations": {str(trigger_id): 0}}) == "trigger_authority_revoked"
    assert trigger_fire_authority_block_reason(trigger, {"trigger_authority_generations": {str(trigger_id): 2}}) is None


def test_legacy_generation_zero_remains_usable_until_actual_revocation():
    owner = uuid4()
    config = stamp_trigger_authority({"minutes": 30, "_authority_generation": 99}, owner_user_id=owner, root_session_id=None)
    trigger = SimpleNamespace(id=uuid4(), config=config)
    assert trigger_authority_generation(trigger) == 0 and "_authority_generation" not in config
    assert trigger_fire_authority_block_reason(trigger, {}) is None
    trigger.config = {**config, "_authority_generation": 1}
    assert trigger_fire_authority_block_reason(trigger, {}) == "trigger_authority_revoked"


def test_unverified_generation_is_unavailable_rather_than_a_new_grant():
    trigger = SimpleNamespace(id=uuid4(), config={"_authority_generation": 1})
    for recorded in (True, -1, 2, "1"):
        assert trigger_fire_authority_block_reason(trigger, {
            "trigger_authority_generations": {str(trigger.id): recorded}}) == "trigger_authority_generation_unavailable"
