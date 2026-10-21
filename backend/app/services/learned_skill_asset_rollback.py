"""Resume an exact owner lifecycle command without reinstalling immutable Skills."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import uuid

from sqlalchemy import select, text

from app.database import tenant_scoped_session
from app.models.ai_asset import AIAssetRecord
from app.models.config_revision import ConfigRevision
from app.services.config_versioning import canonical_content_hash, save_revision
from app.services.content_input_receipts import ContentInputReceiptError

ENTITY = "learned_skill_asset_operation"
SCHEMA = "hive.learned_skill_asset_operation.v1"


def _valid(row, tenant_id):
    c = row.content if row is not None else None
    try:
        return (row.tenant_id == tenant_id and row.entity_type == ENTITY and isinstance(c, dict)
            and row.content_hash == canonical_content_hash(c) and c["schema"] == SCHEMA
            and row.entity_id == uuid.uuid5(uuid.UUID(c["asset_id"]), c["operation_id"])
            and row.changed_by_user_id is not None and c["state"] in {"pending", "completed", "superseded"})
    except (AttributeError, KeyError, TypeError, ValueError):
        return False


async def pending_operations(db, *, tenant_id, installation_id):
    rows = await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == ENTITY, ConfigRevision.is_active.is_(True),
        ConfigRevision.content["installation_revision_id"].astext == str(installation_id),
        ConfigRevision.content["state"].astext == "pending"))
    result = list(rows)
    if any(not _valid(row, tenant_id) for row in result):
        raise ContentInputReceiptError("skill_owner_operation_unverified", "Reconcile the original owner operation receipt.", 503)
    return result


def pending_metadata(row):
    c = row.content
    return {key: c[key] for key in ("operation_id", "expected_revision_id", "target_version")}


async def require_no_pending_operation(db, *, tenant_id, installation_id):
    rows = await pending_operations(db, tenant_id=tenant_id, installation_id=installation_id)
    if rows:
        raise _pending_error(rows[0].content)


async def pending_asset_operation(db, record):
    if record.asset_type != "skill" or (record.native_locator_json or {}).get("kind") != "agent_workspace":
        return None
    row = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == record.tenant_id,
        ConfigRevision.entity_type == ENTITY, ConfigRevision.is_active.is_(True),
        ConfigRevision.content["asset_id"].astext == str(record.id),
        ConfigRevision.content["state"].astext == "pending"))
    if row is not None and not _valid(row, record.tenant_id):
        raise ContentInputReceiptError("skill_owner_operation_unverified", "Reconcile the original owner operation receipt.", 503)
    return row


def _pending_error(command, *, receipt=None, outcome="unknown"):
    exc = ContentInputReceiptError("skill_owner_operation_pending",
        "Resume the exact owner rollback, or retire this version to supersede it.", 503)
    exc.operation_id = command["operation_id"]
    exc.outcome = outcome
    if receipt is not None:
        exc.native_transaction_id = receipt.transaction_id
    return exc


async def supersede_pending_operations(db, *, tenant_id, installation_id, user):
    for row in await pending_operations(db, tenant_id=tenant_id, installation_id=installation_id):
        await _save_command(db, row, {**row.content, "state": "superseded", "executed_by": str(user.id)}, user)


async def _save_command(db, row, content, user):
    return await save_revision(db, tenant_id=row.tenant_id, entity_type=ENTITY, entity_id=row.entity_id,
        content=content, change_source="user", changed_by_user_id=user.id,
        change_message="Owner Skill lifecycle operation receipt")


async def _lock(db, record):
    # Match install/retirement ordering; never await SQL while holding the native lock.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"registry-skill-install:{record.tenant_id}:{record.owner_id}"})
    return await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == record.tenant_id,
        AIAssetRecord.id == record.id).with_for_update().execution_options(populate_existing=True))


async def _operation(db, tenant_id, identity):
    return await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == tenant_id,
        ConfigRevision.entity_type == ENTITY, ConfigRevision.entity_id == identity,
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))


async def _authorize_owner(db, record, user, target):
    from app.models.user import User
    from app.services.learned_skill_sources import authorize_learned_asset_read

    actor = await db.get(User, user.id, populate_existing=True) if user is not None else None
    if (actor is None or actor.tenant_id != record.tenant_id or actor.role not in {"org_admin", "platform_admin"}
        or record.owner_type != "agent" or record.owner_id is None):
        raise ContentInputReceiptError("skill_owner_authority_required", "An authenticated tenant administrator is required.", 403)
    await authorize_learned_asset_read(db, record, user=actor, revision_content=target.content)
    db.expunge(actor)
    return actor


async def _completed(db, row, record, user):
    from app.services.learned_skill_sources import authorize_learned_asset_read

    if not _valid(row, record.tenant_id) or row.content["state"] != "completed":
        return None
    result = await db.get(ConfigRevision, uuid.UUID(row.content["result_revision_id"]), populate_existing=True)
    if (result is None or result.tenant_id != record.tenant_id or result.entity_type != "ai_asset"
        or result.entity_id != record.id or result.content_hash != row.content["result_content_hash"]
        or canonical_content_hash(result.content) != result.content_hash):
        raise ContentInputReceiptError("skill_owner_operation_unverified", "Restore the original completed rollback receipt.", 503)
    await authorize_learned_asset_read(db, record, user=user, revision_content=result.content)
    # Retry finalization of the original receipt, without touching current lifecycle files.
    from app.config import get_settings
    from app.services.agent_asset_transaction import AgentAssetTransaction, finalize_agent_asset_transaction

    root = Path(get_settings().AGENT_DATA_DIR) / row.content["agent_id"]
    try:
        with AgentAssetTransaction(root, operation="learned_skill_asset_rollback",
            idempotency_key=f"learned-owner:{row.tenant_id}:{row.content['operation_id']}:{row.content['asset_id']}",
            requires_projection=True) as tx:
            receipt = tx.commit() if tx.is_replay else None
        if receipt is not None and receipt.transaction_id == row.content["native_transaction_id"]:
            finalize_agent_asset_transaction(root, receipt, projection_ref=f"ai-asset-revision:{result.id}")
    except Exception:
        pass
    return record, result


def _native_restore(root, installation, command):
    from app.services.agent_asset_transaction import AgentAssetTransaction, committed_asset_file_snapshot
    from app.services.provisional_trial import (DEFAULT_TRIAL_WINDOW_DAYS, SCHEMA as TRIAL_SCHEMA,
        _parse_iso, _stage_trial, load_provisional_trial, trial_rel_path)
    from app.services.skill_curator import load_skill_usage, save_skill_usage
    from app.services.skill_evolution_registry import get_skill_evolution_entry, upsert_skill_evolution_entry

    folder = installation.content["folder_name"]
    path = f"skills/{folder}/SKILL.md"
    candidate = installation.content["learning"]["gate_candidate_id"]
    # Both control files need a committed native write witness, not mere existence.
    snapshots = {p: committed_asset_file_snapshot(root, p) for p in
        ("evolution/skill_registry.json", trial_rel_path(candidate))}
    if any(value is None for value in snapshots.values()):
        raise ContentInputReceiptError("skill_owner_native_unverified", "Restore the original committed trial and registry evidence.", 503)
    now = datetime.now(UTC)
    event = {"operation_id": command["operation_id"], "target_revision_id": command["target_revision_id"],
        "state": command["desired_state"], "retired": command["desired_retired"], "occurred_at": now.isoformat()}
    with AgentAssetTransaction(root, operation="learned_skill_asset_rollback",
        idempotency_key=f"learned-owner:{installation.tenant_id}:{command['operation_id']}:{command['asset_id']}",
        evidence_refs=(f"ai-asset-revision:{command['target_revision_id']}",), requires_projection=True) as tx:
        entry = get_skill_evolution_entry(root, path, transaction=tx)
        trial = load_provisional_trial(root, candidate, transaction=tx)
        if (entry is None or entry.get("last_candidate_id") != candidate or not isinstance(trial, dict)
            or trial.get("schema") != TRIAL_SCHEMA or trial.get("candidate_id") != candidate
            or trial.get("target_path") != path
            or trial.get("candidate_version_hash") != "sha256:" + installation.content["files"][path]
            or any(tx.read_bytes(p) != snapshot[0] for p, snapshot in snapshots.items())
            or any(hashlib.sha256(tx.read_bytes(p) or b"").hexdigest() != digest
                for p, digest in installation.content["files"].items())):
            raise ContentInputReceiptError("skill_owner_native_unverified", "Reconcile the exact installed trial, registry and bytes.", 503)
        if tx.is_replay:
            if (entry.get("metadata", {}).get("owner_operation_id") != command["operation_id"]
                or entry.get("state") != command["desired_state"]):
                raise ContentInputReceiptError("skill_owner_operation_conflict", "Reconcile the intervening native lifecycle change.", 409)
            return tx.commit()
        if command["desired_state"] == "provisional":
            started = _parse_iso(trial.get("started_at"))
            if started is None or now > started + timedelta(days=max(1, int(trial.get("window_days") or DEFAULT_TRIAL_WINDOW_DAYS))):
                raise ContentInputReceiptError("skill_owner_trial_expired", "Review a fresh candidate; the original trial window has expired.", 409)
        trial = deepcopy(trial)
        # Preserve observations, model reviews and the original trial window.
        trial["state"] = {"active": "promoted", "stale": "promoted"}.get(command["desired_state"],
            command["desired_state"] if command["desired_state"] in {"provisional", "rolled_back", "needs_review"} else trial["state"])
        trial["updated_at"] = now.isoformat()
        trial.setdefault("owner_restorations", []).append(event)
        _stage_trial(tx, trial)
        usage = load_skill_usage(root, transaction=tx)
        item = dict(usage.get(folder) or {})
        desired = command["desired_state"]
        item.update(state="archived" if desired in {"archived", "rolled_back", "needs_review", "blocked"} else
            "stale" if desired == "stale" else "active", archived_at=now.isoformat() if desired == "archived" else None,
            owner_operation_id=command["operation_id"])
        item.setdefault("owner_restorations", []).append(event)
        if desired in {"active", "stale", "provisional"}:
            item["restore_registry_state"] = desired
        usage[folder] = item
        save_skill_usage(root, usage, transaction=tx)
        upsert_skill_evolution_entry(root, skill_name=entry["skill_name"], target_path=path,
            skill_origin=entry["skill_origin"], evolvable=entry.get("evolvable", True), state=desired,
            metadata={"owner_operation_id": command["operation_id"], "owner_restoration": event}, transaction=tx)
        return tx.commit()


async def rollback_learned_asset(db, *, record, target, user, operation_id, expected_revision_id):
    from app.config import get_settings
    from app.services.agent_asset_transaction import finalize_agent_asset_transaction
    from app.services.ai_assets import _audit_event, apply_projection_to_record
    from app.services.learned_skill_lifecycle import native_lifecycle_projection
    from app.services.learned_skill_sources import _revision_valid
    from app.services.skill_source_receipts import _installation_id

    locator = record.native_locator_json or {}
    if record.asset_type != "skill" or locator.get("kind") != "agent_workspace":
        return None
    agent_id, folder = uuid.UUID(locator["agent_id"]), locator["folder_name"]
    installation = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == record.tenant_id,
        ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.entity_id == _installation_id(agent_id, folder),
        ConfigRevision.is_active.is_(True)))
    if not (installation is not None and (installation.content or {}).get("learning")) and not folder.startswith("learned-"):
        return None
    record = await _lock(db, record)
    user = await _authorize_owner(db, record, user, target)
    if not _revision_valid(target, tenant_id=record.tenant_id, entity_type="ai_asset") or target.entity_id != record.id:
        raise ContentInputReceiptError("skill_owner_target_unverified", "Restore the exact target asset revision.", 403)
    if operation_id is None or expected_revision_id is None:
        raise ContentInputReceiptError("skill_owner_command_required", "Bind the rollback to an operation and the reviewed active revision.", 409)
    identity = uuid.uuid5(record.id, str(operation_id))
    previous = await _operation(db, record.tenant_id, identity)
    control = target.content.get("control") or {}
    desired, retired = control.get("evolution_state"), control.get("learned_retired", target.change_source == "retire")
    if (target.content.get("schema") != "hive.ai_asset.workspace_skill.v1"
        or target.content.get("config", {}).get("folder_name") != folder
        or desired not in {"active", "provisional", "stale", "archived", "rolled_back", "needs_review", "blocked"}
        or not isinstance(retired, bool) or (retired and desired != "archived")
        or control.get("lifecycle_status") != ("active" if desired in {"active", "stale", "provisional"} else "deprecated")):
        raise ContentInputReceiptError("skill_owner_target_unverified", "Select a verified historical lifecycle state.", 409)
    command = {"schema": SCHEMA, "asset_id": str(record.id), "agent_id": str(agent_id),
        "installation_revision_id": str(installation.id), "installation_hash": installation.content_hash,
        "operation_id": str(operation_id), "expected_revision_id": str(expected_revision_id),
        "target_revision_id": str(target.id), "target_content_hash": target.content_hash, "target_version": target.version,
        "desired_state": desired, "desired_retired": retired}
    if previous is not None:
        if not _valid(previous, record.tenant_id) or any(previous.content.get(k) != v for k, v in command.items()):
            raise ContentInputReceiptError("skill_owner_operation_conflict", "Use the exact original rollback command.", 409)
        done = await _completed(db, previous, record, user)
        if done is not None:
            return done
        if previous.content["state"] != "pending":
            raise ContentInputReceiptError("skill_owner_operation_superseded", "Review the current version before starting another rollback.", 409)
        command = previous.content
    else:
        await require_no_pending_operation(db, tenant_id=record.tenant_id, installation_id=installation.id)
        if record.active_revision_id != expected_revision_id:
            raise ContentInputReceiptError("skill_owner_revision_changed", "Refresh the asset before starting a rollback.", 409)
        command.update(state="pending", requested_by=str(user.id))
        previous = await save_revision(db, tenant_id=record.tenant_id, entity_type=ENTITY, entity_id=identity,
            content=command, change_source="user", changed_by_user_id=user.id, change_message="Owner Skill rollback prepared")
        tenant_id, asset_id = record.tenant_id, record.id
        installation_id, target_id, user_id = installation.id, target.id, user.id
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            # A failed response is not proof that the intent failed to commit.
            async with tenant_scoped_session(tenant_id) as fresh:
                confirmed = await _operation(fresh, tenant_id, identity)
                if confirmed is None or not _valid(confirmed, tenant_id) or confirmed.content != command:
                    raise _pending_error(command, outcome="not_started") from None
            record = await db.get(AIAssetRecord, asset_id, populate_existing=True)
            installation = await db.get(ConfigRevision, installation_id, populate_existing=True)
            target = await db.get(ConfigRevision, target_id, populate_existing=True)
            from app.models.user import User

            user = await db.get(User, user_id, populate_existing=True)
    # Prepare committed: re-establish both current authority and ordering before the native effect.
    record = await _lock(db, record)
    previous = await _operation(db, record.tenant_id, identity)
    if previous is None or not _valid(previous, record.tenant_id) or previous.content["state"] != "pending":
        done = await _completed(db, previous, record, user)
        if done is not None:
            return done
        raise ContentInputReceiptError("skill_owner_operation_superseded", "Review the current asset lifecycle.", 409)
    if record.active_revision_id != expected_revision_id:
        raise ContentInputReceiptError("skill_owner_revision_changed", "Reconcile the intervening asset revision before resuming.", 409)
    user = await _authorize_owner(db, record, user, target)
    root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    receipt = None
    tenant_id, asset_id = record.tenant_id, record.id
    try:
        await native_lifecycle_projection(db, record, user=user, recover=True)
        receipt = _native_restore(root, installation, command)
        await save_revision(db, tenant_id=tenant_id, entity_type="learned_skill_lifecycle",
            entity_id=uuid.uuid5(agent_id, f"learned-skill-lifecycle:{installation.id}"),
            content={"schema": "hive.learned_skill_lifecycle.v1", "agent_id": str(agent_id),
                "family_id": installation.content["learning"]["family_id"], "installation_revision_id": str(installation.id),
                "state": "retired" if retired else "restored"}, change_source="user", changed_by_user_id=user.id,
            change_message="Owner restored an exact historical Skill lifecycle")
        record.compatibility_json = {**(record.compatibility_json or {}), "learned_retired": retired}
        projection, _native = await native_lifecycle_projection(db, record, user=user)
        digest = canonical_content_hash(projection.content)
        apply_projection_to_record(record, projection, content_hash=digest)
        revision = await save_revision(db, tenant_id=tenant_id, entity_type="ai_asset", entity_id=asset_id,
            content=projection.content, change_source="rollback", changed_by_user_id=user.id,
            change_message=f"Rollback to version {target.version}", rollback_of_revision_id=target.id, force_revision=True)
        record.active_revision_id = revision.id
        await _save_command(db, previous, {**command, "state": "completed", "executed_by": str(user.id),
            "native_transaction_id": receipt.transaction_id, "result_revision_id": str(revision.id), "result_content_hash": digest}, user)
        db.add(_audit_event(tenant_id=tenant_id, action="ai_asset.rollback", asset_id=asset_id, actor_user_id=user.id,
            actor_agent_id=None, details={"operation_id": str(operation_id), "target_revision_id": str(target.id),
                "new_revision_id": str(revision.id), "native_transaction_id": receipt.transaction_id}))
        await db.commit()
    except ContentInputReceiptError as exc:
        if receipt is not None:
            exc.outcome = "unknown"
            exc.operation_id = command["operation_id"]
            exc.native_transaction_id = receipt.transaction_id
        raise
    except Exception:
        await db.rollback()
        async with tenant_scoped_session(tenant_id) as fresh:
            current = await fresh.get(AIAssetRecord, asset_id, populate_existing=True)
            confirmed = await _operation(fresh, tenant_id, identity)
            done = await _completed(fresh, confirmed, current, user)
            if done is None:
                raise _pending_error(command, receipt=receipt) from None
            record, revision = done
    if receipt is not None:
        try:
            finalize_agent_asset_transaction(root, receipt, projection_ref=f"ai-asset-revision:{revision.id}")
        except Exception:
            # SQL and native receipts already prove completion; housekeeping can retry.
            pass
    return record, revision
