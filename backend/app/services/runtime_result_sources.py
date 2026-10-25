"""Revalidate canonical delegated results before disclosing their committed bytes."""

import hashlib
import json
import uuid

from sqlalchemy import select

from app.models.agent import Agent
from app.models.chat_artifact import ChatArtifact
from app.models.hr_creation import HrCreationDraft
from app.models.invocation_span import InvocationSpan
from app.models.organization_identity import ContentInputReceipt
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionModelResult, SessionToolInvocation, SessionTurnInput
from app.services.content_input_receipts import (
    ContentInputReceiptError,
    _runtime_input_actor,
    _validate_input_dependencies,
)
from app.services.session_model_inputs import _model_dependencies, _stored_dependencies


async def _published_owned_delegation_input(db, dependency, task, *, receipt_ids=frozenset()):
    kind, source_id = dependency["source_kind"], dependency["source_id"]
    if kind == "agent_directory_observation":
        from app.services.agent_directory_sources import owned_directory_source

        return await owned_directory_source(
            db,
            dependency=dependency,
            agent_id=task.child_agent_id,
            tenant_id=task.tenant_id,
            session_id=uuid.UUID(task.child_session_id),
            user_id=task.root_user_id,
        )
    if kind in {
        "agent_identity",
        "agent_context_resource",
        "agent_resident_memory",
        "agent_platform_skill_file",
        "owner_action_policy",
    }:
        try:
            agent_id = uuid.UUID(source_id.split("\n")[0])
        except ValueError:
            return False
        ref = source_id.split("\n")[1:]
        allowed = {
            "agent_identity": {"identity"},
            "agent_context_resource": {"soul", "a2a-collaborators"},
            "agent_resident_memory": {"memory/self/self.md"},
        }
        return agent_id == task.child_agent_id and (
            kind in {"agent_platform_skill_file", "owner_action_policy"} or bool(ref) and ref[0] in allowed[kind]
        )
    if kind == "session_tool_invocation":
        try:
            row = await db.get(SessionToolInvocation, uuid.UUID(source_id), populate_existing=True)
        except ValueError:
            return False
        if (
            row is None
            or row.tenant_id != task.tenant_id
            or row.run_id != task.id
            or row.session_id != uuid.UUID(task.child_session_id)
        ):
            return False
        from app.services.runtime_tool_timeout_sources import native_tool_timeout_bound

        if await native_tool_timeout_bound(db, agent_id=task.child_agent_id, invocation=row):
            return True
        if row.tool_name in {
            "write_file",
            "fs_write",
            "run_command",
            "execute_code",
            "ask_user_question",
            "browser_manage",
            "browser_observe",
            "browser_action",
        }:
            return True
        from app.services.work_ledger_tool_sources import LEDGER_RESULT_TOOLS, ledger_result_sources

        ledger_read = row.tool_name in LEDGER_RESULT_TOOLS
        directory_read = row.tool_name in {"list_files", "fs_list", "glob_search"}
        search_read = row.tool_name == "grep_search"
        clock_read = row.tool_name == "get_current_time"
        if (
            not ledger_read
            and not directory_read
            and not search_read
            and not clock_read
            and row.tool_name not in {"read_file", "read_document", "fs_read"}
        ):
            return False
        from app.models.chat_transcript_event import ChatTranscriptEvent
        from app.services.content_input_receipts import load_native_read_denial_evidence, load_workspace_input_evidence
        from app.services.session_semantic_history import _event_payload

        event = await db.scalar(
            select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.id == row.result_event_id,
                ChatTranscriptEvent.tenant_id == task.tenant_id,
                ChatTranscriptEvent.agent_id == task.child_agent_id,
                ChatTranscriptEvent.session_id == row.session_id,
                ChatTranscriptEvent.run_id == task.id,
                ChatTranscriptEvent.invocation_id == row.id,
            )
        )
        payload = _event_payload(event) if event else {}
        model = await db.scalar(
            select(SessionModelResult).where(
                SessionModelResult.tenant_id == task.tenant_id,
                SessionModelResult.session_id == row.session_id,
                SessionModelResult.run_id == task.id,
                SessionModelResult.provider_request_id == row.provider_request_id,
            )
        )
        if model is None:
            return False
        if clock_read:
            from app.services.content_input_receipts import load_native_clock_read_evidence

            pointer = (payload.get("metadata") or {}).get("native_clock_read_receipt")
            if not isinstance(pointer, dict) or pointer.get("status") != "verified":
                return False
            try:
                return pointer == await load_native_clock_read_evidence(
                    db,
                    agent_id=task.child_agent_id,
                    invocation=row,
                    model_result=model,
                    arguments=row.effective_arguments_json or row.provider_arguments_json,
                    content_hash=payload.get("content_hash"),
                    capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
                )
            except (ContentInputReceiptError, KeyError, TypeError, ValueError):
                return False
        if search_read:
            from app.services.content_input_receipts import load_workspace_execution_input_evidence

            try:
                _ref, sources = await load_workspace_execution_input_evidence(
                    db,
                    agent_id=task.child_agent_id,
                    invocation=row,
                    model_result=model,
                    arguments=row.effective_arguments_json or row.provider_arguments_json,
                    content_hash=payload.get("content_hash"),
                )
                return all(uuid.UUID(source["source_id"]) in receipt_ids for source in sources)
            except (ContentInputReceiptError, KeyError, TypeError, ValueError):
                return False
        if ledger_read:
            _sources, complete = await ledger_result_sources(
                db,
                tenant_id=task.tenant_id,
                agent_id=task.child_agent_id,
                invocation=row,
                model=model,
                content_hash=payload.get("content_hash"),
            )
            return complete
        if directory_read:
            from app.services.content_input_receipts import (
                load_native_directory_read_evidence,
                load_native_empty_read_evidence,
            )

            metadata = payload.get("metadata") or {}
            directory = "native_directory_read_receipt" in metadata
            pointer = metadata.get("native_directory_read_receipt" if directory else "native_empty_read_receipt")
            if not isinstance(pointer, dict) or pointer.get("status") != "verified":
                return False
            try:
                loader = load_native_directory_read_evidence if directory else load_native_empty_read_evidence
                return pointer == await loader(
                    db,
                    agent_id=task.child_agent_id,
                    invocation=row,
                    model_result=model,
                    arguments=row.effective_arguments_json or row.provider_arguments_json,
                    content_hash=payload.get("content_hash"),
                    capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
                )
            except (ContentInputReceiptError, KeyError, TypeError, ValueError):
                return False
        pointer = (payload.get("metadata") or {}).get("native_read_denial_receipt")
        if pointer is not None:
            if (
                row.effect_state not in {"effect_committed", "failed"}
                or not isinstance(pointer, dict)
                or pointer.get("status") != "verified"
            ):
                return False
            try:
                return pointer == await load_native_read_denial_evidence(
                    db,
                    agent_id=task.child_agent_id,
                    invocation=row,
                    model_result=model,
                    arguments=row.effective_arguments_json or row.provider_arguments_json,
                    content_hash=payload.get("content_hash"),
                    capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
                )
            except (ContentInputReceiptError, KeyError, TypeError, ValueError):
                return False
        if not receipt_ids:
            return False  # A body read still needs the exact published artifact pins.
        pointer = (payload.get("metadata") or {}).get("workspace_input_receipt")
        if not isinstance(pointer, dict):
            return False
        try:
            bound = await load_workspace_input_evidence(
                db,
                agent_id=task.child_agent_id,
                invocation=row,
                model_result=model,
                arguments=row.effective_arguments_json or row.provider_arguments_json,
                content_hash=payload.get("content_hash"),
                capture={"status": "captured", "receipt_ref": pointer.get("evidence_ref")},
            )
            return bound == pointer and uuid.UUID(bound["content_input_receipt_id"]) in receipt_ids
        except (ContentInputReceiptError, KeyError, TypeError, ValueError):
            return False
    if kind == "session_command":
        from app.models.chat_transcript_event import ChatTranscriptEvent

        try:
            command_id = uuid.UUID(source_id)
        except ValueError:
            return False
        pair = (
            await db.execute(
                select(SessionTurnInput, SessionCommand)
                .join(SessionCommand, SessionCommand.id == SessionTurnInput.command_id)
                .where(
                    SessionCommand.id == command_id,
                    SessionCommand.tenant_id == task.tenant_id,
                    SessionCommand.session_id == uuid.UUID(task.child_session_id),
                    SessionCommand.namespace == "human_input",
                    SessionCommand.command_kind == "answer_request",
                    SessionCommand.principal_type == "user",
                    SessionCommand.principal_id == task.root_user_id,
                    SessionTurnInput.tenant_id == task.tenant_id,
                    SessionTurnInput.session_id == SessionCommand.session_id,
                    SessionTurnInput.intent == "answer_request",
                    SessionTurnInput.target_run_id == task.id,
                    SessionTurnInput.status == "applied",
                )
            )
        ).first()
        if pair is None:
            return False
        answer, command = pair
        question = await db.scalar(
            select(ChatTranscriptEvent)
            .where(
                ChatTranscriptEvent.tenant_id == task.tenant_id,
                ChatTranscriptEvent.session_id == answer.session_id,
                ChatTranscriptEvent.run_id == task.id,
                ChatTranscriptEvent.item_kind == "user_question",
                ChatTranscriptEvent.item_id == answer.request_item_id,
                ChatTranscriptEvent.input_id == answer.id,
                ChatTranscriptEvent.lifecycle == "completed",
            )
            .order_by(ChatTranscriptEvent.sequence.desc())
            .limit(1)
        )
        payload = (question.metadata_json or {}).get("v2_payload") or {} if question else {}
        return (
            payload.get("requested_by_user_id") == str(task.root_user_id)
            and payload.get("answer_input_revision") == answer.revision
            and (command.target_json or {}).get("request_item_id") == str(answer.request_item_id)
        )
    if kind in {"session_actor_identity", "delegation_context"}:
        try:
            row = await db.get(InvocationSpan, uuid.UUID(source_id), populate_existing=True)
        except ValueError:
            return False
        return (
            row is not None
            and row.tenant_id == task.tenant_id
            and row.agent_id == task.child_agent_id
            and row.session_id == str(uuid.UUID(task.child_session_id))
        )
    return False


async def load_canonical_result_sources(db, result_object):
    if result_object.source_kind == "employee_creation":
        return await _load_creation_result_sources(db, result_object)
    if result_object.source_kind == "approval":
        return await _load_company_result_sources(db, result_object)
    if result_object.source_kind != "a2a_delegation":
        return False  # Legacy/non-model results retain unknown dependency coverage.
    try:
        task_id = uuid.UUID(result_object.source_run_id)
    except ValueError:
        return False
    task = await db.scalar(
        select(RuntimeTask).where(
            RuntimeTask.id == task_id,
            RuntimeTask.tenant_id == result_object.tenant_id,
            RuntimeTask.task_type == "delegation",
        )
    )
    if task is None:
        return False
    native = await db.scalar(
        select(SessionModelResult.id)
        .where(SessionModelResult.tenant_id == task.tenant_id, SessionModelResult.run_id == task.id)
        .limit(1)
    )
    if native is None:
        return False
    if task.status != "completed":
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "The delegated result is not a canonical completion.", 503
        )
    from app.services.direct_invocation_terminal_boundary_processor import _build_direct_terminal_spec
    from app.services.runtime_terminal_boundary_outbox import TerminalBoundaryCanonicalMismatch

    try:
        spec = await _build_direct_terminal_spec(db, task)
    except (TerminalBoundaryCanonicalMismatch, RuntimeError) as exc:
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "Reconcile the canonical delegated result before reading it.", 503
        ) from exc
    statement = select(SessionModelResult).where(
        SessionModelResult.tenant_id == task.tenant_id,
        SessionModelResult.run_id == task.id,
        SessionModelResult.session_id == spec.terminal_event.session_id,
    )
    # The terminal spec has verified this publication's exact model binding.
    # A final message follows its round commit; it is not the round event itself.
    native_final_id = (spec.terminal_event.metadata_json or {}).get("native_final_model_result_id")
    if native_final_id is not None:
        statement = statement.where(SessionModelResult.id == uuid.UUID(str(native_final_id)))
    else:
        statement = statement.where(SessionModelResult.round_committed_event_id == spec.terminal_event.id)
    model = await db.scalar(statement)
    dependencies, complete = _model_dependencies(model) if model else ([], False)
    if not complete or task.status != "completed":
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "The delegated result has incomplete input sources.", 503
        )
    if (
        hashlib.sha256(bytes(result_object.payload_bytes)).hexdigest() != result_object.sha256
        or len(result_object.payload_bytes) != result_object.size_bytes
    ):
        raise ContentInputReceiptError("content_source_changed", "The committed result bytes have changed.", 403)
    payload = json.loads(bytes(result_object.payload_bytes).decode(result_object.encoding))
    if payload.get("summary") != task.result_summary:
        raise ContentInputReceiptError(
            "runtime_result_source_changed", "The result does not match its canonical completion.", 409
        )
    receipt_ids, returned_sources, artifact_dependencies = set(), set(), []
    for part in payload.get("artifacts") or []:
        try:
            artifact_id = uuid.UUID(part.get("source_artifact_id") or part["artifact_id"])
            artifact = await db.get(ChatArtifact, artifact_id, populate_existing=True)
            snapshot = artifact.snapshot_json if artifact else {}
            receipt = await db.get(
                ContentInputReceipt, uuid.UUID(snapshot["content_input_receipt_id"]), populate_existing=True
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ContentInputReceiptError(
                "runtime_result_artifact_unverified", "Reconcile the original artifact source pin.", 503
            ) from exc
        if (
            artifact is None
            or artifact.tenant_id != task.tenant_id
            or artifact.agent_id != task.child_agent_id
            or artifact.runtime_task_id != task.id
            or artifact.session_id != uuid.UUID(task.child_session_id)
            or artifact.path != part.get("path")
            or artifact.snapshot_hash != part.get("revision_id")
            or snapshot.get("preview_content") != part.get("preview_snapshot_content")
            or receipt is None
            or receipt.tenant_id != task.tenant_id
            or receipt.carrier_kind != "workspace_artifact"
            or receipt.carrier_ref != f"{artifact.agent_id}\n{artifact.path}"
            or receipt.content_sha256 != snapshot.get("content_hash")
        ):
            raise ContentInputReceiptError(
                "runtime_result_artifact_unverified",
                "The returned artifact no longer matches its original source pin.",
                503,
            )
        receipt_ids.add(receipt.id)
        returned_sources.add((str(receipt.id), receipt.content_sha256))
        artifact_dependencies.extend(receipt.dependencies_json)
    # A read of the exact returned version is already represented by its artifact pin.
    # Keep every carrier's inputs, including a real self-reference, in the source graph.
    dependencies = [
        source
        for source in dependencies
        if not (
            source["source_kind"] == "workspace_artifact"
            and (source["source_id"], source["source_version"]) in returned_sources
        )
    ] + artifact_dependencies
    dependencies = _stored_dependencies(dependencies)
    if dependencies is None:
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "The result's input source inventory is invalid.", 503
        )
    return task, dependencies, receipt_ids


async def _load_creation_result_sources(db, result):
    """Verify a completed native provisioning receipt against its original creator and exact producer."""
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.delegation_input_origin import load_employee_creation_dependencies
    from app.services.employee_first_task import creation_receipt_notification
    from app.services.runtime_notification_outbox import _normalized_base_values
    from app.services.runtime_result_store import (
        decode_runtime_result_payload,
        encode_runtime_result_payload,
        runtime_result_object_id,
    )

    try:
        payload = decode_runtime_result_payload(bytes(result.payload_bytes))
        draft = await db.get(HrCreationDraft, uuid.UUID(payload["metadata"]["draft_id"]), populate_existing=True)
        task = await db.get(RuntimeTask, uuid.UUID(result.source_run_id), populate_existing=True)
        if (
            draft is None
            or task is None
            or draft.tenant_id != result.tenant_id
            or task.tenant_id != result.tenant_id
            or draft.status != "completed"
            or task.status != "completed"
            or draft.provisioning_task_id != task.id
            or str(task.id) != result.source_run_id
            or task.task_type != "hr_provisioning"
        ):
            return False
        original = await load_employee_creation_dependencies(db, draft=draft)
        if original is None or not original[1]:
            return False
        notification = await creation_receipt_notification(db, draft, task)
        if notification is None:
            return False
        values = _normalized_base_values(notification)
        encoded = encode_runtime_result_payload(
            summary=values["summary"], artifacts=values["artifacts"], metadata=values["metadata"]
        )
        row = await db.get(RuntimeNotificationOutbox, values["id"], populate_existing=True)
        if (
            row is None
            or any(
                getattr(row, key) != values[key]
                for key in (
                    "tenant_id",
                    "source_kind",
                    "source_run_id",
                    "parent_session_id",
                    "parent_agent_id",
                    "parent_user_id",
                    "terminal_status",
                    "task_type",
                    "delivery_mode",
                )
            )
            or row.root_runtime_task_id != uuid.UUID(str(notification.root_runtime_task_id))
            or row.result_object_id != result.id
            or row.result_sha256 != result.sha256
            or row.result_size_bytes != result.size_bytes
            or row.result_ref != f"runtime-result://{result.id}/{result.sha256}"
            or row.artifact_count != 0
            or encoded.payload_bytes != bytes(result.payload_bytes)
            or encoded.sha256 != result.sha256
            or encoded.size_bytes != result.size_bytes
            or result.id
            != runtime_result_object_id(
                tenant_id=result.tenant_id,
                source_kind="employee_creation",
                source_run_id=result.source_run_id,
                sha256=result.sha256,
            )
        ):
            return False
        first_id = values["metadata"].get("first_task_runtime_id")
        if first_id is not None:
            first = await db.get(RuntimeTask, uuid.UUID(first_id), populate_existing=True)
            if (
                first is None
                or first.id != uuid.uuid5(draft.id, "explicit-first-task.v1")
                or first.tenant_id != draft.tenant_id
                or first.parent_agent_id != draft.hr_agent_id
                or first.parent_session_id != str(draft.session_id)
                or first.child_agent_id != draft.created_agent_id
                or first.root_user_id != draft.requested_by_user_id
                or first.root_runtime_task_id != row.root_runtime_task_id
            ):
                return False
        return task, original[0], set()
    except (ValueError, TypeError, KeyError, AttributeError):
        # Legacy or changed observations remain readable with unknown source coverage.
        return False


async def _load_company_result_sources(db, result):
    from app.models.audit import ApprovalRequest
    from app.models.chat_session import ChatSession
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.services.approval_ticket import ApprovalTicketError
    from app.services.runtime_result_store import decode_runtime_result_payload, encode_runtime_result_payload
    from app.services.session_approval import group_approval_origin
    from app.services.session_model_inputs import _origin_dependencies
    from app.services.feishu_read_receipts import FEISHU_RESULT_TOOLS, load_approved_feishu_result
    from app.services.approval_execution_runtime import approval_notification_summary

    try:
        payload = decode_runtime_result_payload(bytes(result.payload_bytes))
        approval = await db.get(ApprovalRequest, uuid.UUID(str(payload["metadata"]["approval_id"])))
        session = await db.get(ChatSession, uuid.UUID(str(payload["metadata"]["origin_session_id"])))
        if session is None:
            return False
        if approval is None or approval.tenant_id != result.tenant_id or session.tenant_id != result.tenant_id:
            raise ValueError("ticket_scope_mismatch")
        office = approval.tool_name in FEISHU_RESULT_TOOLS
        workspace_office = approval.tool_name in {"office_document_create", "office_document_apply"}
        machine = approval.requested_by is None and approval.requested_by_external_principal_id is not None
        if session.audience_kind != "group" and not machine and not office and not workspace_office:
            return False
        row = await db.get(RuntimeNotificationOutbox, uuid.UUID(approval.execution_receipt["continuation_outbox_id"]))
        if (
            row is None
            or row.result_object_id != result.id
            or row.source_kind != "approval"
            or row.source_run_id != result.source_run_id
            or row.parent_session_id != session.id
            or row.parent_agent_id != approval.agent_id
            or row.parent_user_id != approval.requested_by
            or row.source_run_id != str(approval.execution_task_id or approval.id)
        ):
            raise ValueError("ticket_return_mismatch")
        encoded = encode_runtime_result_payload(
            summary=payload["summary"], artifacts=payload["artifacts"], metadata=payload["metadata"]
        )
        if (
            encoded.payload_bytes != bytes(result.payload_bytes)
            or encoded.sha256 != result.sha256
            or encoded.size_bytes != result.size_bytes
            or row.result_sha256 != result.sha256
            or row.result_size_bytes != result.size_bytes
            or payload["metadata"].get("approval_status") != approval.execution_status
            or payload["metadata"].get("tool_name") != approval.tool_name
        ):
            raise ValueError("ticket_result_changed")
        origin, invocation, _event = await group_approval_origin(db, session=session, approval=approval)
        if machine:
            from app.services.machine_runtime_authority import machine_approval_return_actor

            await machine_approval_return_actor(
                db, session=session, rows=[row], root_runtime_task_id=row.root_runtime_task_id
            )
        dependencies, complete = await _origin_dependencies(
            db, tenant_id=result.tenant_id, agent_id=session.agent_id, ref=f"session-tool-result:{invocation.id}"
        )
        task = await db.get(RuntimeTask, approval.execution_task_id) if approval.execution_task_id else origin
        expected_summary = str(approval.execution_result or (task.result_summary if task else None) or "")
        if payload["metadata"].get("result_semantics") == "approval_status":
            expected_summary = approval_notification_summary(
                approval.execution_status, expected_summary, approval.execution_receipt
            )
        if (
            not complete
            or task is None
            or task.root_user_id != approval.requested_by
            or task.parent_session_id != str(session.id)
            or task.parent_agent_id != session.agent_id
            or approval.execution_status
            not in {"succeeded", "failed", "needs_reapproval", "needs_reconciliation", "rejected"}
            or payload["summary"] != expected_summary
        ):
            raise ValueError("ticket_execution_unresolved")
        receipt_ids = set()
        if workspace_office and approval.execution_status == "succeeded":
            from app.services.content_input_receipts import load_approved_workspace_result
            from app.services.session_model_inputs import _unique_dependencies

            captured = await load_approved_workspace_result(db, approval=approval)
            if captured is None:
                raise ValueError("ticket_workspace_result_unverified")
            sources, receipt_ids = captured
            dependencies = _unique_dependencies([*dependencies, *sources])
        elif office and (
            approval.execution_status == "succeeded"
            or payload["metadata"].get("result_semantics") == "execution_result"
        ):
            if await load_approved_feishu_result(db, approval=approval) is None:
                raise ValueError("ticket_office_result_unverified")
        elif (
            session.audience_kind != "group"
            and not machine
            and payload["metadata"].get("result_semantics") != "approval_status"
        ):
            raise ValueError("ticket_office_status_unverified")
        return task, dependencies, receipt_ids
    except (ApprovalTicketError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "Reconcile the original company approval result.", 503
        ) from exc


async def _shared_company_result_published(db, *, result, agent_id, session_id):
    if result.source_kind != "approval":
        return False
    canonical = await _load_company_result_sources(db, result)
    if not canonical:
        return False
    task, _dependencies, _receipts = canonical
    if task.parent_agent_id != agent_id or uuid.UUID(task.parent_session_id) != session_id:
        return False
    from app.models.chat_session import ChatSession

    session = await db.get(ChatSession, session_id)
    if session is None or session.audience_kind != "group":
        return False
    from app.services.runtime_result_store import decode_runtime_result_payload

    payload = decode_runtime_result_payload(bytes(result.payload_bytes))
    # Other tools need their post-execution source inventory before sharing new returned bytes.
    if payload["metadata"]["tool_name"] not in {"write_file", "fs_write"}:
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound",
            "Reconcile the approved tool's output sources before group disclosure.",
            503,
        )
    return True


async def approved_workspace_connector_sources(db, *, result, agent_id, user_id):
    """Mirror only descriptor-proven Office outputs for their original private reader."""
    from app.services.connector_acl import authoritative_connector_source_item
    from app.services.runtime_result_store import decode_runtime_result_payload

    if result.source_kind != "approval" or decode_runtime_result_payload(bytes(result.payload_bytes))["metadata"].get(
        "tool_name"
    ) not in {"office_document_create", "office_document_apply"}:
        return []
    canonical = await load_canonical_result_sources(db, result)
    if not canonical:
        return []
    task, _dependencies, receipt_ids = canonical
    if task.parent_agent_id != agent_id or task.root_user_id != user_id:
        return []
    items = []
    for receipt_id in receipt_ids:
        receipt = await db.get(ContentInputReceipt, receipt_id)
        prefix, path = receipt.carrier_ref.split("\n", 1)
        if prefix != str(agent_id):
            return []
        items.append(
            authoritative_connector_source_item(
                source=f"office://workspace/{path}",
                connector="office",
                resource_type="document",
                tenant_id=result.tenant_id,
                current_user_id=user_id,
                agent_id=agent_id,
            )
        )
    return items


async def runtime_result_publication_bound(db, *, result, agent_id, session_id, user_id):
    """Prove the stored return route; callers must still authorize the current reader and every source."""
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultIntegrationPage

    ref = f"runtime-result://{result.id}/{result.sha256}"
    bound = await db.scalar(
        select(RuntimeNotificationOutbox.id)
        .where(
            RuntimeNotificationOutbox.tenant_id == result.tenant_id,
            RuntimeNotificationOutbox.result_object_id == result.id,
            RuntimeNotificationOutbox.result_ref == ref,
            RuntimeNotificationOutbox.parent_agent_id == agent_id,
            RuntimeNotificationOutbox.parent_session_id == session_id,
            RuntimeNotificationOutbox.parent_user_id == user_id,
        )
        .limit(1)
    )
    if bound is None:
        bound = await db.scalar(
            select(RuntimeResultIntegrationPage.id)
            .where(
                RuntimeResultIntegrationPage.tenant_id == result.tenant_id,
                RuntimeResultIntegrationPage.parent_agent_id == agent_id,
                RuntimeResultIntegrationPage.parent_session_id == session_id,
                RuntimeResultIntegrationPage.parent_user_id == user_id,
                RuntimeResultIntegrationPage.manifest_json["items"].contains([{"result_ref": ref}]),
            )
            .limit(1)
        )
    return bound is not None or await _shared_company_result_published(
        db, result=result, agent_id=agent_id, session_id=session_id
    )


async def validate_runtime_result_sources(db, request, result_object, *, user=None, session=None):
    if result_object.source_kind == "remote_a2a":
        from app.services.remote_a2a_artifacts import load_packet

        task, _result, original, _observation = await load_packet(
            db,
            result_object.tenant_id,
            {"source_id": str(result_object.id), "source_version": result_object.sha256},
            user=user,
            runtime_request=request,
            allow_unconfirmed=True,
        )
        if session is not None and (session.id != original.id or session.agent_id != task.parent_agent_id):
            raise ContentInputReceiptError(
                "content_source_access_denied", "The remote observation belongs to another work.", 403
            )
        # This covers the external packet and known local inputs, never the
        # remote service's internal sources, actions, charges or business truth.
        return True
    canonical = await load_canonical_result_sources(db, result_object)
    if not canonical:
        return False
    task, dependencies, receipt_ids = canonical
    if request is not None:
        principal, requester = await _runtime_input_actor(db, task.tenant_id, request)
        reader_agent, reader_session = principal.agent_id, uuid.UUID(str(principal.session_id))
    else:
        requester = user
        reader_agent, reader_session = session.agent_id, session.id
    if reader_agent != task.parent_agent_id or reader_session != uuid.UUID(task.parent_session_id):
        raise ContentInputReceiptError(
            "content_source_access_denied", "The result belongs to another original work.", 403
        )
    if task.machine_credential_id is not None and (
        requester is None
        or getattr(requester, "machine_credential_id", None) != task.machine_credential_id
        or getattr(requester, "external_principal_id", None) != task.root_external_principal_id
        or requester.id is not None
    ):
        raise ContentInputReceiptError(
            "content_source_access_denied", "The machine result belongs to another requester.", 403
        )
    if requester.id != task.root_user_id:
        from app.models.chat_session import ChatSession
        from app.services.content_input_receipts import _authorize_session_input_source

        if not await _shared_company_result_published(
            db, result=result_object, agent_id=reader_agent, session_id=reader_session
        ):
            raise ContentInputReceiptError(
                "content_source_access_denied", "The result is not published to this reader's work.", 403
            )
        source_session = await db.get(ChatSession, reader_session, populate_existing=True)
        if (
            source_session is None
            or source_session.tenant_id != task.tenant_id
            or source_session.agent_id != reader_agent
        ):
            raise ContentInputReceiptError(
                "content_source_unavailable", "The result's original group work is unavailable.", 503
            )
        await _authorize_session_input_source(db, task.tenant_id, source_session, user=user, runtime_request=request)
    if result_object.source_kind == "approval" and receipt_ids:
        for receipt_id in receipt_ids:
            output = await db.get(ContentInputReceipt, receipt_id)
            await _validate_input_dependencies(
                db,
                task.tenant_id,
                [
                    {
                        "source_kind": "workspace_artifact",
                        "source_id": str(output.id),
                        "source_version": output.content_sha256,
                    }
                ],
                user=user,
                runtime_request=request,
                active_workspace_receipts=frozenset(),
            )
    draft = await db.scalar(
        select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == task.tenant_id,
            HrCreationDraft.created_agent_id == task.child_agent_id,
            HrCreationDraft.hr_agent_id == task.parent_agent_id,
            HrCreationDraft.session_id == uuid.UUID(task.parent_session_id),
            HrCreationDraft.requested_by_user_id == task.root_user_id,
        )
    )
    first_task = (
        draft is not None
        and task.id == uuid.uuid5(draft.id, "explicit-first-task.v1")
        and uuid.UUID(task.child_session_id) == uuid.uuid5(task.id, "session")
    )
    if first_task:
        from app.services.delegation_input_origin import load_employee_creation_dependencies
        from app.services.hr_creation_service import direct_creation_authorization

        original = await load_employee_creation_dependencies(db, draft=draft)
        evidence = direct_creation_authorization(draft)
        child = await db.get(Agent, task.child_agent_id, populate_existing=True)
        if (
            original is None
            or not original[1]
            or child is None
            or child.owner_user_id != requester.id
            or not evidence
            or task.prompt != evidence["first_task"]
            or task.root_runtime_task_id != uuid.UUID(evidence["source_run_id"])
        ):
            raise ContentInputReceiptError(
                "runtime_result_sources_unbound", "The original first-task publication is unavailable.", 503
            )
        # Recheck creating-model inputs before the original work consumes seed facts or its user's bound answers.
        await _validate_input_dependencies(db, task.tenant_id, original[0], user=user, runtime_request=request)
    owned_return = False
    if not first_task and result_object.source_kind == "a2a_delegation" and requester.id == task.root_user_id:
        from app.models.chat_session import ChatSession

        child = await db.get(Agent, task.child_agent_id, populate_existing=True)
        original_work = await db.get(ChatSession, reader_session, populate_existing=True)
        owned_return = (
            child is not None
            and child.tenant_id == task.tenant_id
            and child.owner_user_id == requester.id
            and child.deleted_at is None
            and child.deactivated_at is None
            and original_work is not None
            and original_work.tenant_id == task.tenant_id
            and original_work.agent_id == reader_agent
            and original_work.user_id == requester.id
            and original_work.audience_kind in {None, "private"}
            and await runtime_result_publication_bound(
                db, result=result_object, agent_id=reader_agent, session_id=reader_session, user_id=requester.id
            )
        )
    for dependency in dependencies:
        published = (first_task or owned_return) and await _published_owned_delegation_input(
            db, dependency, task, receipt_ids=receipt_ids
        )
        await _validate_input_dependencies(
            db,
            task.tenant_id,
            [dependency],
            user=requester if published else user,
            runtime_request=None if published else request,
            active_workspace_receipts=frozenset(receipt_ids),
        )
    return True


async def validate_published_workspace_inputs(db, request, receipt, *, active_receipts):
    """Reopen a published first-task carrier without granting its upstream raw sources."""
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject
    from app.services.employee_creation_sources import (
        load_employee_creation_context,
        validate_creation_publication_inputs,
    )
    from app.services.hr_creation_service import direct_creation_authorization

    principal, requester = await _runtime_input_actor(db, receipt.tenant_id, request)
    draft = await db.scalar(
        select(HrCreationDraft).where(
            HrCreationDraft.tenant_id == receipt.tenant_id,
            HrCreationDraft.created_agent_id == principal.agent_id,
            HrCreationDraft.requested_by_user_id == requester.id,
            HrCreationDraft.status == "completed",
        )
    )
    if draft is None:
        return False
    task_id = uuid.uuid5(draft.id, "explicit-first-task.v1")
    if str(principal.runtime_task_id) in {str(task_id), task_id.hex} and uuid.UUID(
        str(principal.session_id)
    ) == uuid.uuid5(task_id, "session"):
        return False  # The producing run uses its live input grants, before terminal publication exists.
    artifact = await db.scalar(
        select(ChatArtifact)
        .where(
            ChatArtifact.tenant_id == receipt.tenant_id,
            ChatArtifact.agent_id == principal.agent_id,
            ChatArtifact.runtime_task_id == task_id,
            ChatArtifact.session_id == uuid.uuid5(task_id, "session"),
            receipt.carrier_ref == str(principal.agent_id) + "\n" + ChatArtifact.path,
        )
        .order_by(ChatArtifact.created_at.desc())
        .limit(1)
    )
    if artifact is None:
        return False
    result = await db.scalar(
        select(RuntimeResultObject)
        .join(RuntimeNotificationOutbox, RuntimeNotificationOutbox.result_object_id == RuntimeResultObject.id)
        .where(
            RuntimeResultObject.tenant_id == receipt.tenant_id,
            RuntimeResultObject.source_kind == "a2a_delegation",
            RuntimeResultObject.source_run_id == str(task_id),
            RuntimeNotificationOutbox.tenant_id == receipt.tenant_id,
            RuntimeNotificationOutbox.parent_agent_id == draft.hr_agent_id,
            RuntimeNotificationOutbox.parent_session_id == draft.session_id,
            RuntimeNotificationOutbox.parent_user_id == requester.id,
        )
        .limit(1)
    )
    canonical = await load_canonical_result_sources(db, result) if result is not None else None
    if not canonical:
        raise ContentInputReceiptError(
            "content_source_unavailable", "Reconcile the original first-task publication.", 503
        )
    task, _dependencies, receipt_ids = canonical
    evidence = direct_creation_authorization(draft)
    try:
        pin = await db.get(
            ContentInputReceipt, uuid.UUID(artifact.snapshot_json["content_input_receipt_id"]), populate_existing=True
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ContentInputReceiptError(
            "content_source_unavailable", "The original artifact source pin is missing.", 503
        ) from exc
    if (
        not evidence
        or task.child_agent_id != principal.agent_id
        or task.parent_agent_id != draft.hr_agent_id
        or task.root_user_id != requester.id
        or uuid.UUID(task.parent_session_id) != draft.session_id
        or uuid.UUID(task.child_session_id) != uuid.uuid5(task_id, "session")
        or task.prompt != evidence["first_task"]
        or str(task.root_runtime_task_id) != evidence["source_run_id"]
        or pin is None
        or pin.id not in receipt_ids
        or pin.carrier_ref != receipt.carrier_ref
        or pin.revision > receipt.revision
    ):
        raise ContentInputReceiptError(
            "content_source_unavailable", "The saved carrier lacks its exact first-task publication.", 503
        )
    creation = await load_employee_creation_context(
        db, tenant_id=receipt.tenant_id, agent_id=principal.agent_id, user=None, runtime_request=request
    )
    await validate_creation_publication_inputs(
        db,
        draft=draft,
        dependencies=receipt.dependencies_json,
        creation_dependencies=creation["dependencies"],
        user=None,
        runtime_request=request,
        active_workspace_receipts=active_receipts,
    )
    return True


async def result_source_reference(db, *, tenant_id, agent_id, session_id, task_id):
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject

    result = await db.scalar(
        select(RuntimeResultObject)
        .join(RuntimeNotificationOutbox, RuntimeNotificationOutbox.result_object_id == RuntimeResultObject.id)
        .where(
            RuntimeNotificationOutbox.tenant_id == tenant_id,
            RuntimeResultObject.tenant_id == tenant_id,
            RuntimeNotificationOutbox.parent_agent_id == agent_id,
            RuntimeNotificationOutbox.parent_session_id == uuid.UUID(str(session_id)),
            RuntimeNotificationOutbox.source_kind == "a2a_delegation",
            RuntimeNotificationOutbox.source_run_id == str(task_id),
        )
        .limit(1)
    )
    if result is None:
        return None
    return {
        "source_kind": "runtime_result",
        "source_id": f"{agent_id}\n{session_id}\n{result.id}",
        "source_version": result.sha256,
    }


async def _validate_shared_company_page(db, *, page, session, user, runtime_request):
    from app.models.runtime_notification_outbox import RuntimeNotificationOutbox
    from app.models.runtime_result import RuntimeResultObject
    from app.services.runtime_notification_outbox import _result_descriptor

    items = (page.manifest_json or {}).get("items")
    rows = list(
        await db.scalars(
            select(RuntimeNotificationOutbox).where(
                RuntimeNotificationOutbox.tenant_id == page.tenant_id,
                RuntimeNotificationOutbox.integration_page_id == page.id,
                RuntimeNotificationOutbox.parent_session_id == session.id,
                RuntimeNotificationOutbox.parent_agent_id == session.agent_id,
                RuntimeNotificationOutbox.parent_user_id == page.parent_user_id,
            )
        )
    )
    by_id = {str(row.id): row for row in rows}
    if (
        session.audience_kind != "group"
        or page.delivery_mode != "parent_continuation"
        or page.status not in {"processing", "delivered"}
        or not isinstance(items, list)
        or not items
        or len(by_id) != len(items)
        or {item.get("outbox_id") for item in items if isinstance(item, dict)} != set(by_id)
    ):
        raise ContentInputReceiptError(
            "runtime_result_sources_unbound", "The group return page lacks its exact published results.", 503
        )
    for item in items:
        row = by_id[item["outbox_id"]]
        result = await db.get(RuntimeResultObject, row.result_object_id, populate_existing=True)
        if (
            item != _result_descriptor(row).to_manifest_item()
            or result is None
            or not await _shared_company_result_published(
                db, result=result, agent_id=session.agent_id, session_id=session.id
            )
        ):
            raise ContentInputReceiptError(
                "runtime_result_sources_unbound", "The group return item does not match its committed result.", 503
            )
        await validate_runtime_result_sources(db, runtime_request, result, user=user, session=session)


async def validate_runtime_return_dependency(db, tenant_id, dependency, *, user, runtime_request):
    from app.models.chat_session import ChatSession
    from app.models.runtime_result import RuntimeResultIntegrationPage, RuntimeResultObject
    from app.services.content_input_receipts import _authorize_session_input_source
    from app.services.session_model_inputs import _hash

    kind = dependency["source_kind"]
    try:
        if kind == "runtime_result_page":
            page = await db.get(
                RuntimeResultIntegrationPage, uuid.UUID(dependency["source_id"]), populate_existing=True
            )
            if page is None or page.tenant_id != tenant_id:
                raise ValueError("page_missing")
            agent_id, session_id = page.parent_agent_id, page.parent_session_id
        else:
            parts = dependency["source_id"].split("\n")
            agent_id, session_id = uuid.UUID(parts[0]), uuid.UUID(parts[1])
        session = await db.get(ChatSession, session_id, populate_existing=True)
        if session is None or session.tenant_id != tenant_id or session.agent_id != agent_id:
            raise ValueError("source_session_mismatch")
    except (ValueError, IndexError, TypeError) as exc:
        raise ContentInputReceiptError(
            "content_source_unavailable", "The runtime return source is invalid.", 503
        ) from exc
    await _authorize_session_input_source(db, tenant_id, session, user=user, runtime_request=runtime_request)
    if runtime_request is not None:
        principal, requester = await _runtime_input_actor(db, tenant_id, runtime_request)
        if principal.agent_id != agent_id or uuid.UUID(str(principal.session_id)) != session_id:
            raise ContentInputReceiptError(
                "content_source_access_denied", "The runtime return belongs to another work.", 403
            )
    else:
        requester = user
    if kind == "runtime_result_page":
        manifest = page.manifest_json
        if (
            _hash({k: v for k, v in manifest.items() if k != "manifest_sha256"}) != page.manifest_sha256
            or manifest.get("manifest_sha256") != page.manifest_sha256
            or page.manifest_sha256 != dependency["source_version"]
        ):
            raise ContentInputReceiptError(
                "content_source_changed", "The integration page no longer matches its source.", 403
            )
        if page.parent_user_id != requester.id:
            await _validate_shared_company_page(
                db, page=page, session=session, user=user, runtime_request=runtime_request
            )
        return
    if kind == "runtime_team_context":
        if len(parts) == 3:
            from app.services.content_input_receipts import _validate_input_dependencies

            try:
                row = await db.get(InvocationSpan, uuid.UUID(parts[2]), populate_existing=True)
                proof = (row.metadata_json or {}).get("observation") if row else None
                if (
                    not isinstance(proof, dict)
                    or proof.get("schema") != "hive.runtime_team_publication_observation.v1"
                    or _hash(proof) != dependency["source_version"]
                    or row.tenant_id != tenant_id
                    or row.agent_id != agent_id
                    or row.session_id != str(session_id)
                    or row.user_id != requester.id
                    or row.span_type != kind
                    or row.name != "team_publication_observed"
                    or row.status != "captured"
                    or proof.get("run_id") != str(row.runtime_task_id)
                    or proof.get("user_id") != str(requester.id)
                    or proof.get("agent_id") != str(agent_id)
                    or proof.get("session_id") != str(session_id)
                    or row.id
                    != uuid.uuid5(row.runtime_task_id, f"runtime-team-publication:{dependency['source_version']}")
                    or not proof.get("pending_task_ids")
                    or not isinstance(proof.get("content"), str)
                ):
                    raise ValueError("team_observation_mismatch")
                sources = _stored_dependencies(proof.get("sources"))
                if sources is None:
                    raise ValueError("team_observation_sources_missing")
                await _validate_input_dependencies(db, tenant_id, sources, user=user, runtime_request=runtime_request)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ContentInputReceiptError(
                    "content_source_changed", "The original teammate observation is invalid.", 403
                ) from exc
            return
        from app.services.agent_team_context import build_prompt_facing_team_context

        unbound = []
        rendered = await build_prompt_facing_team_context(
            agent_id=agent_id, tenant_id=tenant_id, session_id=session_id, source_refs=[], source_unbound=unbound
        )
        if unbound or _hash(rendered) != dependency["source_version"]:
            raise ContentInputReceiptError("content_source_changed", "The observed teammate context has changed.", 403)
        return
    try:
        result = await db.get(RuntimeResultObject, uuid.UUID(parts[2]), populate_existing=True)
    except (ValueError, IndexError) as exc:
        raise ContentInputReceiptError(
            "content_source_unavailable", "The runtime result reference is invalid.", 503
        ) from exc
    if (
        result is None
        or result.tenant_id != tenant_id
        or result.sha256 != dependency["source_version"]
        or hashlib.sha256(bytes(result.payload_bytes)).hexdigest() != result.sha256
        or len(result.payload_bytes) != result.size_bytes
    ):
        raise ContentInputReceiptError("content_source_changed", "The committed runtime result is unavailable.", 403)
    if not await runtime_result_publication_bound(
        db, result=result, agent_id=agent_id, session_id=session_id, user_id=requester.id
    ):
        raise ContentInputReceiptError(
            "content_source_access_denied", "The result is not published to this original work.", 403
        )
    if not await validate_runtime_result_sources(db, runtime_request, result, user=user, session=session):
        raise ContentInputReceiptError(
            "content_source_unavailable", "The runtime result source inventory is incomplete.", 503
        )


def runtime_result_denial_bound(*, invocation, payload, content):
    """Bind only an observed native no-data denial, never the denied target's bytes or authority."""
    from app.tools.handlers.context_resources import _RUNTIME_RESULT_REF_RE

    try:
        body = json.loads(content)
        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        return (
            invocation.tool_name == "read_runtime_result"
            and invocation.effect_state == "effect_committed"
            and payload.get("outcome") == "success"
            and str(payload.get("content_or_error_ref") or "").startswith("tool-frame:")
            and isinstance(body, dict)
            and set(body) == {"schema", "status", "reason_code", "result_ref", "retryable", "recovery_action"}
            and body["schema"] == "hive.runtime_result_page.v1"
            and body["status"] == "source_access_denied"
            and body["reason_code"] == "content_source_access_denied"
            and body["retryable"] is False
            and body["recovery_action"] == "reconcile_result_sources"
            and body["result_ref"] == arguments.get("result_ref")
            and _RUNTIME_RESULT_REF_RE.fullmatch(body["result_ref"]) is not None
        )
    except (ValueError, TypeError):
        return False


async def runtime_result_tool_sources(db, *, agent_id, invocation, content):
    """Bind the exact page returned by the native reader to its published result."""
    from app.models.runtime_result import RuntimeResultObject
    from app.tools.handlers.context_resources import _DEFAULT_PAGE_CHARS, _RUNTIME_RESULT_REF_RE

    try:
        body = json.loads(content)
        arguments = invocation.effective_arguments_json or invocation.provider_arguments_json
        match = _RUNTIME_RESULT_REF_RE.fullmatch(arguments.get("result_ref", ""))
        if match is None or body.get("status") != "ok" or body.get("result_ref") != arguments["result_ref"]:
            return None
        result = await db.get(RuntimeResultObject, uuid.UUID(match.group("result_id")))
        if (
            result is None
            or result.tenant_id != invocation.tenant_id
            or result.sha256 != match.group("sha256")
            or hashlib.sha256(bytes(result.payload_bytes)).hexdigest() != result.sha256
            or len(result.payload_bytes) != result.size_bytes
            or body.get("sha256") != result.sha256
        ):
            return None
        offset, limit = arguments.get("offset", 0), arguments.get("limit", _DEFAULT_PAGE_CHARS)
        text = bytes(result.payload_bytes).decode(result.encoding or "utf-8")
        if body.get("offset") != offset or body.get("content") != text[offset : offset + limit]:
            return None
        if body.get("dependency_coverage") != {"complete": True}:
            # A compiler gap is recoverable only from the original native producer and publication.
            if body.get("schema") != "hive.runtime_result_page.v1" or body.get("dependency_coverage") != {
                "complete": False
            }:
                return None
            canonical = await load_canonical_result_sources(db, result)
            if not canonical:
                return None
            task, _dependencies, _receipts = canonical
            if (
                task.parent_agent_id != agent_id
                or uuid.UUID(task.parent_session_id) != invocation.session_id
                or not await runtime_result_publication_bound(
                    db, result=result, agent_id=agent_id, session_id=invocation.session_id, user_id=task.root_user_id
                )
            ):
                return None
        return {
            "source_kind": "runtime_result",
            "source_id": f"{agent_id}\n{invocation.session_id}\n{result.id}",
            "source_version": result.sha256,
        }
    except (ContentInputReceiptError, ValueError, TypeError, KeyError, LookupError):
        return None


async def continuation_page_source(db, *, task, prompt):
    from app.models.runtime_result import RuntimeResultIntegrationPage
    from app.services.agent_session_continuation import build_result_integration_runtime_context
    from app.services.session_model_inputs import _hash

    try:
        page = await db.get(
            RuntimeResultIntegrationPage, uuid.UUID((task.metadata_json or {}).get("integration_page_id", ""))
        )
    except (ValueError, TypeError, AttributeError):
        return None
    # A committed continuation may run before the outbox worker acknowledges page delivery.
    if (
        page is None
        or page.tenant_id != task.tenant_id
        or page.parent_agent_id != task.parent_agent_id
        or page.parent_session_id != uuid.UUID(task.parent_session_id)
        or page.parent_user_id != task.root_user_id
        or page.delivery_mode != "parent_continuation"
        or page.status not in {"processing", "delivered"}
        or _hash({k: v for k, v in page.manifest_json.items() if k != "manifest_sha256"}) != page.manifest_sha256
        or page.manifest_json.get("manifest_sha256") != page.manifest_sha256
        or prompt != build_result_integration_runtime_context(page.manifest_json)
    ):
        return None
    return {"source_kind": "runtime_result_page", "source_id": str(page.id), "source_version": page.manifest_sha256}
