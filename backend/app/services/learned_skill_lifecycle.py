"""Project witnessed native lifecycle facts without repeating model decisions."""

import json
from pathlib import Path
import uuid

from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.ai_asset import AIAssetRecord
from app.models.config_revision import ConfigRevision
from app.services.agent_asset_transaction import committed_asset_file_snapshot
from app.services.config_versioning import canonical_content_hash
from app.services.content_input_receipts import ContentInputReceiptError

async def native_lifecycle_projection(db, record, *, user=None, reader=None, recover=False):
    from app.config import get_settings
    from app.services.ai_asset_adapters import project_workspace_skill
    from app.services.learned_skill_sources import validate_learned_installation
    from app.services.skill_evolution_registry import LOADABLE_STATES
    from app.services.skill_source_receipts import _installation_id
    from app.services.learned_skill_sources import _revision_valid
    from app.services.agent_asset_transaction import AssetTransactionError, recover_agent_asset_transactions

    locator = dict(getattr(record, "native_locator_json", {}) or {})
    if record.asset_type != "skill" or locator.get("kind") != "agent_workspace":
        return None
    agent_id, folder = uuid.UUID(locator["agent_id"]), locator["folder_name"]
    if (record.owner_id != agent_id or record.owner_type != "agent"
        or record.native_key != f"skill:agent:{agent_id}:{folder}"):
        raise ContentInputReceiptError("skill_lifecycle_asset_mismatch", "Restore the exact installed Skill asset identity.", 403)
    installation = await db.scalar(select(ConfigRevision).where(ConfigRevision.tenant_id == record.tenant_id,
        ConfigRevision.entity_type == "agent_skill_install", ConfigRevision.entity_id == _installation_id(agent_id, folder),
        ConfigRevision.is_active.is_(True)).execution_options(populate_existing=True))
    if installation is None or not (installation.content or {}).get("learning"):
        return None
    await validate_learned_installation(db, installation, tenant_id=record.tenant_id, agent_id=agent_id,
        request=reader, user=user)
    root = Path(get_settings().AGENT_DATA_DIR) / str(agent_id)
    try:
        if recover:
            recover_agent_asset_transactions(root)
        snapshot = committed_asset_file_snapshot(root, "evolution/skill_registry.json")
        registry = json.loads(snapshot[0]) if snapshot is not None else None
        if not isinstance(registry, dict) or not isinstance(registry.get("skills"), dict):
            raise ValueError("native_lifecycle_snapshot_unavailable")
    except (AssetTransactionError, OSError, ValueError) as exc:
        raise ContentInputReceiptError("skill_lifecycle_native_unverified", "Reconcile the committed native lifecycle evidence.", 503) from exc
    if snapshot is None:
        raise ContentInputReceiptError("skill_lifecycle_native_unverified", "Reconcile the committed native lifecycle evidence.", 503)
    content, receipt = snapshot
    entries = [entry for entry in registry["skills"].values()
        if isinstance(entry, dict) and entry.get("target_path") == f"skills/{folder}/SKILL.md"]
    if len(entries) != 1 or entries[0].get("state") not in {*LOADABLE_STATES, "archived", "rolled_back", "needs_review", "blocked"}:
        raise ContentInputReceiptError("skill_lifecycle_state_unavailable", "Reconcile the exact native version state.", 503)
    state = entries[0]["state"]
    lifecycle_rows = await db.scalars(select(ConfigRevision).where(ConfigRevision.tenant_id == record.tenant_id,
        ConfigRevision.entity_type == "learned_skill_lifecycle", ConfigRevision.is_active.is_(True),
        ConfigRevision.content["agent_id"].astext == str(agent_id),
        ConfigRevision.content["installation_revision_id"].astext == str(installation.id),
        ConfigRevision.content["state"].astext == "retired"))
    retired = any(_revision_valid(row, tenant_id=record.tenant_id, entity_type="learned_skill_lifecycle")
        and row.content.get("schema") == "hive.learned_skill_lifecycle.v1" for row in lifecycle_rows)
    if retired:
        state = "archived"
    projection = project_workspace_skill(workspace=root, folder_name=folder, tenant_id=record.tenant_id, agent_id=agent_id,
        lifecycle_status="active" if state in LOADABLE_STATES else "deprecated", evolution_state=state)
    expected = {f"skills/{folder}/{file['path']}": file["content"] for file in projection.content["files"]}
    import hashlib

    if {path: hashlib.sha256(body.encode()).hexdigest() for path, body in expected.items()} != installation.content["files"]:
        raise ContentInputReceiptError("skill_lifecycle_bytes_changed", "Restore the exact installed Skill bytes before projection.", 403)
    if retired or "learned_retired" in (record.compatibility_json or {}):
        projection.content["control"]["learned_retired"] = retired
        projection.compatibility["learned_retired"] = retired
    return projection, receipt


async def synchronize_lifecycle(db, record, *, user=None, reader=None, recover=False):
    from app.services.ai_assets import register_projection
    from app.services.learned_skill_asset_rollback import pending_asset_operation, _pending_error

    pending = await pending_asset_operation(db, record)
    if pending is not None:
        raise _pending_error(pending.content)

    snapshot = await native_lifecycle_projection(db, record, user=user, reader=reader, recover=recover)
    if snapshot is None:
        return None
    projection, receipt = snapshot
    digest = canonical_content_hash(projection.content)
    if digest != record.content_hash:
        await register_projection(db, projection, change_source="native_lifecycle",
            actor_user_id=getattr(user, "id", None), actor_agent_id=record.owner_id,
            change_message="Committed native Skill lifecycle reconciled")
    else:
        record.projection_status, record.projection_error = "applied", None
    return {"status": "applied", "expected_hash": digest, "actual_hash": digest, "transaction_id": receipt.transaction_id}


async def asset_payload_for_reader(db, record, *, user):
    from app.services.ai_assets import asset_payload
    from app.services.learned_skill_asset_rollback import pending_asset_operation, pending_metadata

    payload = asset_payload(record)
    pending = await pending_asset_operation(db, record)
    if pending is not None:
        payload["projection"] = {"status": "pending", "error": "Resume the pending owner rollback or retire this version.",
            "owner_operation": pending_metadata(pending)}
        return payload
    try:
        snapshot = await native_lifecycle_projection(db, record, user=user)
    except ContentInputReceiptError as exc:
        if exc.code not in {"skill_lifecycle_native_unverified", "skill_lifecycle_state_unavailable"}:
            raise
        payload["projection"] = {"status": "unavailable", "error": exc.code}
        return payload
    if snapshot is not None:
        projection, _receipt = snapshot
        if canonical_content_hash(projection.content) != record.content_hash:
            payload["projection"] = {"status": "drifted",
                "error": f"Native lifecycle is {projection.compatibility['evolution_state']}; reconcile the asset projection."}
    return payload


async def synchronize_after_native(*, workspace, frame, paths, receipt):
    """Confirm SQL readback on response loss; never replay or undo native work."""
    if frame is None:
        return {"status": "not_required"}
    paths = tuple(dict.fromkeys(paths))
    if not paths:
        return {"status": "applied"}
    async def apply(*, verify_only):
        async with tenant_scoped_session(frame.tenant_id) as db:
            for path in paths:
                record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == frame.tenant_id,
                    AIAssetRecord.native_key == f"skill:agent:{frame.agent_id}:{Path(path).parts[1]}")
                    .with_for_update().execution_options(populate_existing=True))
                if record is None:
                    raise ContentInputReceiptError("skill_lifecycle_asset_unavailable", "Restore the original Skill asset projection.", 503)
                if verify_only:
                    snapshot = await native_lifecycle_projection(db, record, reader=frame.reader)
                    if (snapshot is None or record.content_hash != canonical_content_hash(snapshot[0].content)
                        or record.projection_status != "applied" or record.projection_error is not None):
                        raise ContentInputReceiptError("skill_lifecycle_projection_pending", "Reconcile the committed native lifecycle.", 503)
                else:
                    await synchronize_lifecycle(db, record, reader=frame.reader)
            await db.commit()
    try:
        await apply(verify_only=False)
        await apply(verify_only=True)
    except Exception:
        try:
            await apply(verify_only=True)
        except Exception:
            return {"status": "needs_reconciliation", "outcome": "unknown",
                "reason_code": "skill_lifecycle_projection_pending", "native_transaction_id": receipt.transaction_id}
    return {"status": "applied", "native_transaction_id": receipt.transaction_id}


async def reconcile_agent_lifecycles(*, workspace, agent_id, tenant_id):
    """Repair verified projection drift without requiring or calling a model."""
    from app.services.learned_skill_sources import skill_maintenance_inputs, skill_maintenance_targets
    from app.services.skill_evolution_registry import LOADABLE_STATES

    if tenant_id is None:
        return {"status": "unavailable", "reason_code": "skill_maintenance_principal_unavailable", "applied": 0, "held": []}
    snapshot = committed_asset_file_snapshot(workspace, "evolution/skill_registry.json")
    if snapshot is None:
        return {"status": "unavailable", "reason_code": "skill_lifecycle_native_unverified", "applied": 0, "held": []}
    registry, receipt = snapshot
    states = {entry.get("target_path"): entry.get("state") for entry in json.loads(registry).get("skills", {}).values()}
    report = {"status": "applied", "applied": 0, "held": []}
    for target in await skill_maintenance_targets(agent_id=agent_id, tenant_id=tenant_id):
        path, state = target["target_path"], states.get(target["target_path"])
        async with tenant_scoped_session(tenant_id) as db:
            record = await db.scalar(select(AIAssetRecord).where(AIAssetRecord.tenant_id == tenant_id,
                AIAssetRecord.native_key == f"skill:agent:{agent_id}:{Path(path).parts[1]}"))
            current = record is not None and (record.compatibility_json or {}).get("evolution_state") == state and (
                record.lifecycle_status == ("active" if state in LOADABLE_STATES else "deprecated")) and record.projection_status == "applied" and record.projection_error is None
        if current:
            continue
        try:
            async with skill_maintenance_inputs(workspace=workspace, agent_id=agent_id, tenant_id=tenant_id,
                target_path=path, candidate_id=target["candidate_id"]) as frame:
                result = await synchronize_after_native(workspace=workspace, frame=frame, paths=[path], receipt=receipt)
                if result["status"] == "applied":
                    report["applied"] += 1
                else:
                    report["held"].append({"source_ref": target["source_ref"], **result})
        except ContentInputReceiptError as exc:
            report["held"].append({"source_ref": target["source_ref"], "reason_code": exc.code})
    if report["held"]:
        report["status"] = "needs_reconciliation"
    return report
