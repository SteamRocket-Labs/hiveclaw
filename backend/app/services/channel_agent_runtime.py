"""Shared inbound channel runtime for IM-style agent turns."""

from __future__ import annotations

import traceback
import uuid
import inspect
import json
from types import SimpleNamespace
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.permissions import is_agent_expired


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _tool_event_status(event: Any) -> str:
    if isinstance(event, dict):
        return str(event.get("status") or "").lower()
    return str(getattr(event, "status", "") or "").lower()


def _tool_event_terminal(status: str) -> bool:
    return status in {"done", "completed", "failed", "error"}


def _tool_event_content(event: Any, *, terminal: bool) -> str:
    if isinstance(event, dict) and terminal and "result" in event:
        return str(event.get("result") or "")
    try:
        return json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        return str(event)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def try_confirm_channel_plan_from_text(
    *,
    agent_id: uuid.UUID,
    user_id: Any,
    user_text: str,
    session_id: str | None,
    session_source: str,
    allow_bare_latest: bool = False,
) -> str | None:
    """Confirm and hand off a Plan Mode plan from trusted channel text."""
    from app.services import plan_mode_core, plan_mode_service
    from app.services.plan_mode_service import PlanConflictError

    confirmation = plan_mode_core.extract_plan_confirmation_request(user_text)
    is_bare_confirmation = False
    if (
        confirmation is None
        and allow_bare_latest
        and session_id
        and plan_mode_core.is_bare_plan_confirmation_reply(user_text)
    ):
        confirmation = plan_mode_core.PlanConfirmationRequest(latest=True)
        is_bare_confirmation = True
    if confirmation is None:
        return None
    if user_id is None:
        return "计划确认需要可审计的用户身份。请先绑定账号，或到 Web 端计划卡片确认。"

    service = plan_mode_service.get_plan_mode_service()
    plan = None
    if confirmation.plan_id:
        try:
            plan = await service.get_plan(uuid.UUID(confirmation.plan_id))
        except ValueError:
            return "计划确认失败：plan_id 格式不正确。"
        if plan is not None and str(getattr(plan, "agent_id", "")) != str(agent_id):
            plan = None
        if (
            plan is not None
            and session_id
            and getattr(plan, "session_id", None)
            and str(getattr(plan, "session_id", "")) != str(session_id)
        ):
            return "计划确认失败：该 plan_id 不属于当前会话。请确认当前会话中的计划。"
    elif confirmation.latest and session_id:
        plan = await service.find_latest_awaiting_plan_for_session(agent_id=agent_id, session_id=session_id)

    if plan is None:
        if is_bare_confirmation:
            # A bare "可以/开始/go" with no awaiting plan is usually a normal
            # acknowledgement. Let the agent see the turn instead of swallowing it.
            return None
        return "没有找到当前会话待确认的计划。请带上 plan_id，或到 Web 端计划卡片确认。"
    if getattr(plan, "status", None) != "awaiting_confirmation":
        return f"计划无需重复确认：当前状态为 {getattr(plan, 'status', 'unknown')}。"
    if not getattr(plan, "plan_hash", None):
        return "计划还没有生成完成，暂时不能确认。请稍后重试或到 Web 端计划卡片查看。"

    try:
        confirmed = await service.confirm_plan(
            plan_id=plan.id,
            confirming_user_id=user_id,
            plan_version=plan.plan_version,
            plan_hash=plan.plan_hash,
            reason=f"confirmed via {session_source} text",
        )
        handed_off = await service.handoff_confirmed_plan(plan_id=confirmed.id)
    except PermissionError as exc:
        return f"计划确认失败：{exc}"
    except PlanConflictError as exc:
        return f"计划确认失败：{exc.message}"

    handoff_status = getattr(handed_off, "handoff_status", None) or "not_started"
    if handoff_status == "completed":
        return f"已确认计划（plan_id={confirmed.id}），并已启动执行。"
    if handoff_status == "skipped":
        return f"已确认计划（plan_id={confirmed.id}），但当前计划没有可自动启动的 handoff。"
    if handoff_status == "failed":
        payload = getattr(handed_off, "handoff_payload", None) or {}
        return f"已确认计划（plan_id={confirmed.id}），但启动执行失败：{payload.get('error', 'unknown error')}"
    return f"已确认计划（plan_id={confirmed.id}），handoff 状态：{handoff_status}。"


def _parse_channel_permission_action(text: str) -> str | None:
    import re

    clean = text or ""
    if re.search(
        r"(本会话|当前会话|this session|for this session|session).*?(允许|批准|同意|可以|allow|approve|yes)",
        clean,
        re.IGNORECASE,
    ):
        return "allow_session"
    if re.search(r"(拒绝|驳回|不通过|deny|denied|reject|rejected)", clean, re.IGNORECASE):
        return "deny"
    if re.search(r"(允许|批准|同意|可以|approve|approved|allow|allowed|yes|ok)", clean, re.IGNORECASE):
        return "allow_once"
    return None


_CHANNEL_PERMISSION_MODE_LABELS = {
    "default": ("请求批准", "Ask first"),
    "auto": ("替我批准", "Auto"),
    "bypassPermissions": ("完全访问", "Full access"),
}


def _channel_permission_mode_label(mode: str) -> str:
    zh, _en = _CHANNEL_PERMISSION_MODE_LABELS.get(mode, _CHANNEL_PERMISSION_MODE_LABELS["auto"])
    return zh


def _channel_permission_mode_label_with_en(mode: str) -> str:
    zh, en = _CHANNEL_PERMISSION_MODE_LABELS.get(mode, _CHANNEL_PERMISSION_MODE_LABELS["auto"])
    return f"{zh}（{en}）"


def _parse_channel_permission_mode_command(text: str) -> tuple[str, str | None] | None:
    import re

    clean = (text or "").strip()
    if not clean:
        return None
    lower = clean.lower()

    if lower.startswith(("/permissions", "/permission")):
        parts = clean.split()
        if len(parts) == 1:
            return ("show", None)
        arg = parts[1].strip().lower()
        if arg in {"ask", "default", "request", "approve-first"}:
            return ("set", "default")
        if arg in {"auto", "approve", "approve-for-me"}:
            return ("set", "auto")
        if arg in {"full", "bypass", "bypasspermissions", "bypass-permissions", "full-access"}:
            return ("set", "bypassPermissions")
        return ("show", None)

    if re.search(r"(查看|查询|当前).*(权限模式|权限设置)|^权限模式$|^查看权限$", clean, re.IGNORECASE):
        return ("show", None)
    if re.search(r"(切换|设置|改成|改为).*(请求批准|ask first|ask|default)", clean, re.IGNORECASE):
        return ("set", "default")
    if re.search(r"(切换|设置|改成|改为).*(替我批准|auto|approve for me)", clean, re.IGNORECASE):
        return ("set", "auto")
    if re.search(r"(切换|设置|改成|改为).*(完全访问|full access|full|bypass)", clean, re.IGNORECASE):
        return ("set", "bypassPermissions")
    return None


async def _load_channel_session(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    session_id: str | None,
    durable_session: Any = None,
) -> Any | None:
    if durable_session is not None:
        return durable_session
    session_uuid = _uuid_or_none(session_id)
    if session_uuid is None:
        return None
    from app.models.chat_session import ChatSession

    result = await db.execute(
        select(ChatSession).where(ChatSession.id == session_uuid, ChatSession.agent_id == agent_id)
    )
    return result.scalar_one_or_none()


async def try_handle_channel_permission_mode_command(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    user: Any,
    user_text: str,
    session_id: str | None,
    session_source: str,
    durable_session: Any = None,
) -> str | None:
    """Handle IM-local CCPlus session permission mode query/switch commands."""
    parsed = _parse_channel_permission_mode_command(user_text)
    if parsed is None:
        return None

    from app.api import chat_sessions as chat_sessions_api
    from app.models.runtime_task import RuntimeTask
    from app.runtime.ccplus_contracts import DEFAULT_CCPLUS_PERMISSION_MODE, normalize_permission_mode

    action, requested_mode = parsed
    session = await _load_channel_session(
        db=db,
        agent_id=agent_id,
        session_id=session_id,
        durable_session=durable_session,
    )
    if session is None:
        return "当前没有可切换的会话。请先在当前 IM 会话里发起一次任务，或到 Web 端查看权限模式。"

    if action == "show":
        metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
        mode = chat_sessions_api._session_permission_metadata(
            metadata.get("permission_mode") or DEFAULT_CCPLUS_PERMISSION_MODE.value,
            session,
        )["permission_mode"]
        allowed_tools = [
            str(item) for item in (metadata.get("session_permission_allowed_tools") or []) if str(item).strip()
        ]
        allowed_text = ", ".join(allowed_tools) if allowed_tools else "暂无"
        return (
            f"当前权限模式：{_channel_permission_mode_label_with_en(mode)}\n"
            f"本会话已授权工具：{allowed_text}\n"
            "可切换为：\n"
            "1. 请求批准：/permissions ask\n"
            "2. 替我批准：/permissions auto"
        )

    if user is None:
        return "权限模式切换需要可审计的用户身份。请先绑定账号，或到 Web 端会话内切换。"

    mode = normalize_permission_mode(requested_mode or DEFAULT_CCPLUS_PERMISSION_MODE.value).value
    if mode == "bypassPermissions":
        return (
            "完全访问只能由组织管理员通过 Web 会话的 break-glass 流程开启，"
            "并且必须填写原因、限定 session scope 和 1-60 分钟 TTL。"
        )
    permission_metadata = chat_sessions_api._session_permission_metadata(mode, session)
    session_metadata = dict(getattr(session, "transcript_metadata_json", None) or {})
    session_metadata.update(permission_metadata)
    session.transcript_metadata_json = session_metadata

    session_uuid = _uuid_or_none(session_id or getattr(session, "id", None))
    if session_uuid is not None:
        active_result = await db.execute(
            select(RuntimeTask)
            .where(
                RuntimeTask.parent_agent_id == agent_id,
                RuntimeTask.parent_session_id == str(session_uuid),
                RuntimeTask.status.in_(("pending", "running")),
            )
            .order_by(RuntimeTask.created_at.desc())
            .limit(1)
        )
        active_run = active_result.scalar_one_or_none()
        if active_run is not None:
            active_metadata = dict(getattr(active_run, "metadata_json", None) or {})
            active_metadata.update(permission_metadata)
            active_run.metadata_json = active_metadata

    await db.commit()
    return f"已将当前会话权限模式切换为：{_channel_permission_mode_label(mode)}。"


async def try_resolve_channel_session_permission_from_text(
    *,
    db: AsyncSession,
    agent_id: uuid.UUID,
    user: Any,
    user_text: str,
    session_id: str | None,
    session_source: str,
) -> str | None:
    """Resolve a pending CCPlus session permission request from an IM reply."""
    action = _parse_channel_permission_action(user_text)
    if action is None or not session_id:
        return None
    if user is None:
        return "权限确认需要可审计的用户身份。请先绑定账号，或到 Web 端会话内确认。"

    import re
    from app.api import chat_sessions as chat_sessions_api
    from app.models.chat_transcript_event import ChatTranscriptEvent

    permission_id_pattern = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    explicit_permission_id = None
    match = re.search(permission_id_pattern, user_text or "")
    if match:
        try:
            explicit_permission_id = uuid.UUID(match.group(0))
        except ValueError:
            explicit_permission_id = None

    try:
        session_uuid = uuid.UUID(str(session_id))
    except ValueError:
        return None

    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(ChatTranscriptEvent.agent_id == agent_id, ChatTranscriptEvent.session_id == session_uuid)
        .order_by(ChatTranscriptEvent.created_at.desc())
        .limit(300)
    )
    resolved_ids: set[str] = set()
    pending_id: uuid.UUID | None = None
    pending_tool_name = ""
    if hasattr(result, "scalars"):
        permission_events = result.scalars().all()
    elif hasattr(result, "all"):
        permission_events = result.all()
    elif hasattr(result, "scalar_one_or_none"):
        item = result.scalar_one_or_none()
        permission_events = [] if item is None else [item]
    else:
        permission_events = []

    for event in permission_events:
        metadata = dict(getattr(event, "metadata_json", None) or {})
        decision_request_id = metadata.get("permission_request_id")
        if decision_request_id and getattr(event, "event_type", "") in {
            "session_permission_decision",
            "permission_resolved",
        }:
            resolved_ids.add(str(decision_request_id))
            continue

        parsed = chat_sessions_api._permission_request_payload_from_event(event)
        if parsed is None:
            continue
        request_payload, _tool_payload = parsed
        request_id = str(request_payload.get("permission_request_id") or "")
        if not request_id or request_id in resolved_ids:
            continue
        if explicit_permission_id and request_id != str(explicit_permission_id):
            continue
        try:
            pending_id = uuid.UUID(request_id)
        except ValueError:
            continue
        pending_tool_name = str(request_payload.get("tool_display_name") or request_payload.get("tool_name") or "")
        break

    if pending_id is None:
        return None

    body = chat_sessions_api.ResolveSessionPermissionIn(action=action, feedback=f"resolved via {session_source}")
    result_payload = await chat_sessions_api.resolve_session_permission(
        agent_id=agent_id,
        session_id=session_uuid,
        permission_request_id=pending_id,
        body=body,
        current_user=user,
        db=db,
    )
    status = str(result_payload.get("status") or "")
    tool_label = f"：{pending_tool_name}" if pending_tool_name else ""
    if status == "denied":
        return f"已拒绝本次权限请求{tool_label}。"
    if status == "failed":
        return f"权限请求处理失败{tool_label}：{result_payload.get('error') or 'unknown error'}"
    if action == "allow_session":
        return f"已在本会话允许权限请求{tool_label}，我会继续执行。"
    return f"已允许本次权限请求{tool_label}，我会继续执行。"


async def call_agent_llm(
    db: AsyncSession,
    agent_id: uuid.UUID,
    user_text: str,
    history: list[dict] | None = None,
    user_id: Any = None,
    on_chunk: Any = None,
    on_tool_call: Any = None,
    on_thinking: Any = None,
    session_id: str | None = None,
    session_source: str = "feishu",
    session_channel: str = "feishu",
    allow_bare_plan_confirmation: bool = False,
    durable_run: bool = False,
    durable_session: Any = None,
    durable_user: Any = None,
) -> str:
    """Call the agent runtime from an external channel turn."""
    from app.api.websocket import call_llm
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    agent_result = await db.execute(select(Agent).options(selectinload(Agent.sponsor)).where(Agent.id == agent_id))
    agent = agent_result.scalar_one_or_none()
    if not agent:
        return "⚠️ 数字员工未找到"

    if is_agent_expired(agent):
        return "This Agent has expired and is off duty. Please contact your admin to extend its service."

    external_principal_id = getattr(durable_user, "external_principal_id", None)
    external_authority_bound = bool(getattr(durable_user, "authority_bound", False))
    effective_user_id = user_id if external_principal_id is not None else (user_id or agent_id)
    channel_audit_user = durable_user if external_principal_id is None or external_authority_bound else None
    if channel_audit_user is None and user_id is not None:
        channel_audit_user = SimpleNamespace(id=user_id, username=str(user_id), display_name=str(user_id))
    permission_mode_reply = await try_handle_channel_permission_mode_command(
        db=db,
        agent_id=agent_id,
        user=channel_audit_user,
        user_text=user_text,
        session_id=session_id,
        session_source=session_source,
        durable_session=durable_session,
    )
    if permission_mode_reply is not None:
        return permission_mode_reply

    session_permission_reply = await try_resolve_channel_session_permission_from_text(
        db=db,
        agent_id=agent_id,
        user=channel_audit_user,
        user_text=user_text,
        session_id=session_id,
        session_source=session_source,
    )
    if session_permission_reply is not None:
        return session_permission_reply

    plan_confirmation_reply = await try_confirm_channel_plan_from_text(
        agent_id=agent_id,
        user_id=user_id,
        user_text=user_text,
        session_id=session_id,
        session_source=session_source,
        allow_bare_latest=allow_bare_plan_confirmation,
    )
    if plan_confirmation_reply is not None:
        return plan_confirmation_reply

    if durable_run:
        from app.services.web_chat_runtime import start_channel_chat_run_from_saved_turn

        if durable_session is None:
            return "⚠️ 无法启动后台任务: missing channel session"
        durable_user = durable_user or SimpleNamespace(
            id=effective_user_id,
            username=str(effective_user_id),
            display_name="",
        )
        try:
            run = await start_channel_chat_run_from_saved_turn(
                db=db,
                agent=agent,
                user=durable_user,
                session=durable_session,
                content=user_text,
                source_channel=session_channel or session_source or "channel",
            )
        except Exception as exc:
            logger.error("[ChannelRuntime] Failed to start durable channel run: %s", exc, exc_info=True)
            return f"⚠️ 后台任务启动失败: {type(exc).__name__}"
        if run.get("queued_user_message"):
            return f"已接收补充消息，并排队到当前任务（run_id={run.get('run_id')}）。完成后我会回到当前会话。"
        return f"已接收，正在后台处理（run_id={run.get('run_id')}）。完成后我会回到当前会话。"

    from app.services import plan_mode_core

    plan_entry_decision = plan_mode_core.classify_plan_mode_entry(user_text)
    accepted_recommendation = None
    if (
        plan_entry_decision.mode == "explicit"
        and plan_mode_core.is_plan_mode_acceptance_reply(user_text)
        and user_id is not None
        and session_id
    ):
        from app.services.plan_mode_recommendation_service import accept_latest_recommendation_for_user

        try:
            accepted_recommendation = await accept_latest_recommendation_for_user(
                db,
                agent_id=agent_id,
                user_id=user_id,
                session_id=session_id,
            )
            if accepted_recommendation is not None and hasattr(db, "commit"):
                await db.commit()
        except Exception as exc:
            logger.warning(f"[ChannelRuntime] Plan recommendation accept binding failed (non-fatal): {exc}")
            accepted_recommendation = None

    if accepted_recommendation is not None:
        plan_entry_decision = plan_mode_core.PlanModeEntryDecision(
            mode="explicit",
            intent_type=getattr(accepted_recommendation, "intent_type", None) or "autonomous_wake",
            action_kind=getattr(accepted_recommendation, "action_kind", None) or "create_enabled_trigger",
            tool_name=getattr(accepted_recommendation, "tool_name", None) or "set_trigger",
            title=getattr(accepted_recommendation, "title", None)
            or getattr(accepted_recommendation, "original_request", "")[:120],
            reason="accepted_plan_mode_recommendation",
        )
        user_text = getattr(accepted_recommendation, "original_request", None) or user_text

    if plan_entry_decision.mode == "explicit" and plan_entry_decision.action_kind and plan_entry_decision.tool_name:
        from app.services import plan_mode_core
        from app.services.plan_mode_service import get_plan_mode_service
        from app.services.plan_mode_system_run import launch_system_plan_run

        title = plan_entry_decision.title or user_text[:120] or "Plan Mode request"
        if plan_entry_decision.action_kind == "create_enabled_trigger":
            plan_arguments = {"name": title[:80], "type": "cron", "config": {}, "reason": user_text}
        else:
            plan_arguments = {
                "title": title,
                "description": user_text,
                "handoff_target": "continue_current_session",
            }
        plan_service = get_plan_mode_service()
        intent_type, _signature = plan_mode_core.action_kind_to_intent_signature(
            action_kind=plan_entry_decision.action_kind,
            tool_name=plan_entry_decision.tool_name,
            arguments=plan_arguments,
        )
        draft = await plan_service.create_plan_request(
            agent_id=agent_id,
            requested_by_user_id=effective_user_id,
            original_request=user_text,
            intent_type=intent_type,
            source=session_source,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=session_id,
            runtime_task_id=None,
            metadata_json={
                "intercept_action_kind": plan_entry_decision.action_kind,
                "intercept_tool": plan_entry_decision.tool_name,
                "intercept_source": session_source,
            },
        )
        await launch_system_plan_run(
            draft,
            seed_context={
                "tool_name": plan_entry_decision.tool_name,
                "action_kind": plan_entry_decision.action_kind,
                "arguments": plan_arguments,
            },
        )
        plan = await plan_service.get_plan(draft.id) or draft
        return f"已进入计划模式，并生成一份待确认计划（plan_id={plan.id}）。请确认、修改或拒绝；确认后我再开始执行。"

    model = None
    if agent.primary_model_id:
        model_result = await db.execute(
            select(LLMModel).where(LLMModel.id == agent.primary_model_id, LLMModel.tenant_id == agent.tenant_id)
        )
        model = model_result.scalar_one_or_none()

    fallback_model = None
    if agent.fallback_model_id:
        fb_result = await db.execute(
            select(LLMModel).where(LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id)
        )
        fallback_model = fb_result.scalar_one_or_none()

    from app.services.model_resolution import primary_model_unavailable

    if primary_model_unavailable(agent, model):
        logger.error(
            f"[Channel] Primary model {agent.primary_model_id} unavailable for agent "
            f"{getattr(agent, 'id', None)} (deleted/disabled/cross-tenant) - failing loud instead of silent fallback"
        )
        return (
            "你为该数字员工配置的主模型当前不可用(可能已删除、被禁用,或不属于本公司),"
            "请在「设置 → 模型」中重新选择一个本公司可用的模型。"
        )

    if not model and fallback_model:
        model = fallback_model
        fallback_model = None
        logger.warning(f"[Channel] Primary model unavailable, using fallback: {model.model}")

    if model and agent.tenant_id:
        from app.services.model_resolution import choose_runtime_model_pair, resolve_default_model_for_tenant

        default_runtime_model = await resolve_default_model_for_tenant(
            db,
            agent.tenant_id,
            exclude_model_id=model.id,
        )
        previous_fallback = fallback_model
        model, fallback_model = choose_runtime_model_pair(model, fallback_model, default_runtime_model)
        if fallback_model and fallback_model is not previous_fallback:
            logger.info(f"[Channel] Tenant default fallback loaded: {fallback_model.model}")

    if not model:
        return f"⚠️ {agent.name} 未配置 LLM 模型，请在管理后台设置。"

    messages: list[dict] = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    pending_reply_suffix = ""
    try:
        from app.services.pending_reply_service import (
            claim_and_fulfill_pending_replies,
            format_pending_reply_context,
            sender_identity_from_external_conv_id,
        )

        sender_identity = ""
        if session_id:
            from app.models.chat_session import ChatSession

            sess_r = await db.execute(select(ChatSession).where(ChatSession.id == session_id))
            sess_obj = sess_r.scalar_one_or_none()
            if sess_obj:
                sender_identity = sender_identity_from_external_conv_id(sess_obj.external_conv_id or "")
                if not sender_identity and getattr(sess_obj, "delivery_target_json", None):
                    from app.services.channel_delivery_service import ChannelDeliveryService

                    sender_identity = ChannelDeliveryService.identity_from_delivery_target(
                        sess_obj.delivery_target_json
                    )

        if sender_identity:
            claimed = await claim_and_fulfill_pending_replies(db, agent_id=agent_id, sender_identity=sender_identity)
            if claimed:
                pending_reply_suffix = format_pending_reply_context(claimed)
                logger.info(
                    "[PendingReply] Injecting %d context(s) for agent %s, sender %s",
                    len(claimed),
                    agent_id,
                    sender_identity,
                )
                await db.commit()
    except Exception as exc:
        logger.warning("[PendingReply] Injection failed (non-fatal): %s", exc)

    from app.runtime.session import SessionContext

    session_context = SessionContext(
        session_id=session_id,
        source=session_source,
        channel=session_channel,
    )
    turn_seed = uuid.uuid4().hex
    session_context.metadata["turn_id"] = f"turn-{turn_seed}"
    session_context.metadata["intent_id"] = f"intent-{turn_seed}"
    if getattr(agent, "tenant_id", None):
        session_context.metadata["tenant_id"] = str(agent.tenant_id)
    trusted_decline = plan_mode_core.trusted_decline_metadata(
        content=user_text,
        messages=history,
    )
    if trusted_decline:
        from app.services.plan_mode_recommendation_service import decline_latest_recommendation_for_user

        recommendation = await decline_latest_recommendation_for_user(
            db,
            agent_id=agent_id,
            user_id=user_id,
            session_id=session_id,
        )
        if recommendation is None:
            trusted_decline = None
        else:
            trusted_decline["recommendation_id"] = str(recommendation.id)
            if hasattr(db, "commit"):
                await db.commit()
    if trusted_decline:
        session_context.metadata[plan_mode_core.PLAN_MODE_TRUSTED_DECLINE_SESSION_KEY] = trusted_decline
        plan_decline_suffix = (
            "Plan Mode governance: the runtime verified that the user declined the immediately preceding "
            "Plan Mode recommendation. If you create or update a scheduled/monitoring trigger as a direct "
            "follow-up, call the trigger tool normally. Do not add opt-out fields to tool arguments, and do "
            "not use this opt-out for long tasks, delegation, or other high-risk actions."
        )
        pending_reply_suffix = "\n\n".join(part for part in (pending_reply_suffix, plan_decline_suffix) if part)

    ledger_session_id = _uuid_or_none(session_id)
    ledger_user_id = _uuid_or_none(effective_user_id)
    if ledger_session_id is not None:
        from app.services.chat_transcript import append_session_event

        try:
            await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=getattr(agent, "tenant_id", None),
                session_id=ledger_session_id,
                actor_type="user",
                event_type="user_message",
                role="user",
                t0_role="user",
                user_id=ledger_user_id,
                content=user_text,
                metadata={
                    "source_channel": session_channel,
                    "session_source": session_source,
                    "turn_id": session_context.metadata["turn_id"],
                    "intent_id": session_context.metadata["intent_id"],
                },
                source=session_source or session_channel or "channel",
                materialize_chat_message=False,
            )
        except Exception as exc:
            logger.error("[ChannelRuntime] T0 user append failed; refusing to run non-durable channel turn: {}", exc)
            return f"⚠️ 会话记录写入失败，已停止执行: {type(exc).__name__}"

    async def _append_channel_tool_event(event: Any) -> None:
        if ledger_session_id is None:
            return
        status = _tool_event_status(event)
        terminal = _tool_event_terminal(status)
        await append_session_event(
            db=db,
            agent_id=agent_id,
            tenant_id=getattr(agent, "tenant_id", None),
            session_id=ledger_session_id,
            actor_type="tool",
            event_type="tool_result" if terminal else "tool_call",
            role="tool_call",
            t0_role="tool",
            user_id=ledger_user_id,
            content=_tool_event_content(event, terminal=terminal),
            metadata={
                "source_channel": session_channel,
                "session_source": session_source,
                "tool_event": event,
                "tool_status": status,
                "turn_id": session_context.metadata["turn_id"],
                "intent_id": session_context.metadata["intent_id"],
            },
            source=session_source or session_channel or "channel",
            materialize_chat_message=False,
        )

    if ledger_session_id is not None:

        async def _replayable_on_tool_call(event: Any) -> Any:
            await _append_channel_tool_event(event)
            if on_tool_call is None:
                return None
            return await _maybe_await(on_tool_call(event))

        effective_on_tool_call = _replayable_on_tool_call
    else:
        effective_on_tool_call = on_tool_call

    try:
        reply = await call_llm(
            model,
            messages,
            agent.name,
            agent.role_description or "",
            fallback_model=fallback_model,
            agent_id=agent_id,
            user_id=effective_user_id,
            supports_vision=getattr(model, "supports_vision", False),
            on_chunk=on_chunk,
            on_tool_call=effective_on_tool_call,
            on_thinking=on_thinking,
            session_id=session_id,
            memory_messages=messages,
            session_context=session_context,
            auto_close_session=False,
            session_source=session_source,
            session_channel=session_channel,
            system_prompt_suffix=pending_reply_suffix,
        )
        if ledger_session_id is not None:
            await append_session_event(
                db=db,
                agent_id=agent_id,
                tenant_id=getattr(agent, "tenant_id", None),
                session_id=ledger_session_id,
                actor_type="assistant",
                event_type="assistant_message",
                role="assistant",
                t0_role="assistant",
                user_id=ledger_user_id,
                content=reply,
                metadata={
                    "source_channel": session_channel,
                    "session_source": session_source,
                    "turn_id": session_context.metadata["turn_id"],
                    "intent_id": session_context.metadata["intent_id"],
                },
                source=session_source or session_channel or "channel",
                materialize_chat_message=False,
            )
            try:
                from app.runtime.hooks import HookEvent, emit_hook

                await emit_hook(
                    HookEvent.TURN_STOP,
                    agent_id=agent_id,
                    session_id=str(ledger_session_id),
                    source=session_source,
                    messages=[],
                    metadata={
                        "reason": "invoke_complete",
                        "channel": session_channel,
                        "distillation_scope": "semantic_candidate",
                        "checkpoint_kind": "user_turn_stop",
                        "turn_id": session_context.metadata["turn_id"],
                        "intent_id": session_context.metadata["intent_id"],
                        "tenant_id": str(agent.tenant_id) if getattr(agent, "tenant_id", None) else None,
                    },
                )
            except Exception as close_err:
                logger.debug("[ChannelRuntime] TURN_STOP hook failed (non-fatal): {}", close_err)
            try:
                from app.memory.t0.ledger import seal_t0_session_segment

                seal_t0_session_segment(
                    agent_id=agent_id,
                    session_id=ledger_session_id,
                    reason="invoke_complete",
                    metadata={
                        "source": session_source or session_channel or "channel",
                        "channel": session_channel,
                        "distillation_scope": "semantic_candidate",
                        "checkpoint_kind": "user_turn_stop",
                        "turn_id": session_context.metadata["turn_id"],
                        "intent_id": session_context.metadata["intent_id"],
                    },
                )
            except Exception as seal_err:
                logger.debug("[ChannelRuntime] direct T0 seal skipped after TURN_STOP: {}", seal_err)
        return reply
    except Exception as exc:
        traceback.print_exc()
        error_msg = str(exc) or repr(exc)
        logger.error(f"[LLM] Primary model error: {error_msg}")
        return f"⚠️ 调用模型出错: {error_msg[:150]}"
