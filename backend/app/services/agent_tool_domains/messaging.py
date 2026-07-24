"""Messaging domain — Feishu messaging, web messaging, agent-to-agent communication."""

import json
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.core.execution_context import A2AToolAuthorityFrame, ExecutionPrincipal
from app.database import tenant_scoped_session
from app.services.a2a_collaboration_policy import resolve_a2a_collaboration_policy
from app.services.a2a_outcome import A2AOutcome, A2AOperation
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

A2A_SYSTEM_PROMPT_SUFFIX = (
    "<role>\n"
    "You are receiving a message from another digital employee (agent-to-agent\n"
    "communication, 'A2A'). The sender is a peer agent, not a human user. Your\n"
    "reply goes back to that agent, which will parse it and act on it.\n"
    "</role>\n\n"
    "<reply_format>\n"
    "- Be concise and direct. Peer agents consume structured answers, not chit-chat.\n"
    "- If the request is clear: provide a concrete answer with evidence\n"
    "  (file paths, tool-result IDs, URLs, stdout snippets — whatever applies).\n"
    "- If the request is incomplete or ambiguous: name the specific missing piece\n"
    "  and stop. Do not guess at intent — the sender can resolve the gap faster\n"
    "  than you can work around it.\n"
    "- If you are still working on the request: return a short status update\n"
    "  with (a) the current step, (b) the specific blocker or next action.\n"
    "- If you cannot complete the request: explain specifically what is missing,\n"
    "  blocked, or outside your scope. Don't fabricate a partial answer.\n"
    "</reply_format>\n\n"
    "<workspace_delivery_contract>\n"
    "- If the peer request names existing workspace artifacts or says to update\n"
    "  current/existing work, modify and deliver those same workspace paths. This\n"
    "  applies to documents, presentations, spreadsheets, code files, images, and\n"
    "  other artifacts.\n"
    "- Do not create a replacement artifact unless the peer explicitly asks for a\n"
    "  separate deliverable.\n"
    "- If you need supporting notes, write them only as secondary artifacts and\n"
    "  still update the requested primary artifact path(s).\n"
    "</workspace_delivery_contract>\n\n"
    "<privacy_boundary>\n"
    "- Treat the delegated brief and attached request as the authoritative context.\n"
    "- Do NOT share private workspace data (memory/*.md, tasks.json, soul.md,\n"
    "  logs/) unless the sender explicitly asks for that file.\n"
    "- Do NOT leak information about other senders, other conversations, or\n"
    "  your user's identity unless relevant and authorized.\n"
    "</privacy_boundary>\n\n"
    "<anti_patterns>\n"
    "- **Bounded nested delegation**: you MAY call `delegate_to_agent` or another\n"
    "  peer when specialization materially helps. The runtime enforces inherited\n"
    "  authority, maximum depth, cycle detection, and budget. If a boundary is\n"
    "  reached, return the typed blocker instead of bypassing it.\n"
    "- ❌ **Pleasantries and filler**: don't write 'Sure, happy to help!' or\n"
    "  'Let me know if you need more'. The peer parses content; filler wastes\n"
    "  tokens on both sides.\n"
    "- ❌ **Guessing at ambiguous intent**: if the brief is unclear, ask ONCE\n"
    "  with a specific clarifying question. Do not silently pick an interpretation.\n"
    "- ❌ **Claiming completion without evidence**: 'Done.' with no file path,\n"
    "  tool ID, or artifact is unparseable. Provide the verifiable detail.\n"
    "</anti_patterns>\n\n"
    "<good_reply_example>\n"
    "Peer request: 'Summarize the current CI pipeline.'\n"
    "Good reply:\n"
    "'CI pipeline (from .github/workflows/ci.yml):\n"
    " - lint (ruff + pyright) on push\n"
    " - unit tests on PR\n"
    " - integration tests gated on main\n"
    " - railway deploy on merge to main\n"
    "Evidence: .github/workflows/ci.yml:15-87. Deploy job wired to railway.json.'\n"
    "</good_reply_example>\n\n"
    "<bad_reply_example>\n"
    "Peer request: 'Summarize the current CI pipeline.'\n"
    "Bad reply:\n"
    "'Hi! Sure thing, happy to help. The CI pipeline is pretty standard — it\n"
    "does all the usual stuff like linting and tests and then deploys. Let me\n"
    "know if you'd like me to dive deeper into any particular area!'\n"
    "(No file reference, no concrete stages, no evidence — useless to a peer agent.)\n"
    "</bad_reply_example>"
)


def _normalize_messaging_result(tool_name: str, result: str) -> str:
    if not result or "<tool_error>" in result:
        return result

    message = result.strip()
    if not message.startswith(("❌", "⚠️")):
        return result

    normalized = message.lstrip("❌⚠️ ").strip()
    error_class = "provider_error"
    retryable = False

    lowered = normalized.lower()
    if "please provide" in lowered:
        error_class = "bad_arguments"
    elif "not found" in lowered or "no user named" in lowered or "no agent found" in lowered:
        error_class = "not_found"
    elif "does not belong to the current agent" in lowered or "access denied" in lowered:
        error_class = "auth_or_permission"
    elif "has no llm model configured" in lowered or "no feishu channel configured" in lowered:
        error_class = "not_configured"
    elif "did not respond" in lowered or "cannot receive messages" in lowered:
        error_class = "provider_unavailable"
        retryable = True
    elif "error " in lowered or "failed" in lowered:
        error_class = "provider_error"
        retryable = True

    return render_tool_error(
        tool_name=tool_name,
        error_class=error_class,
        message=normalized,
        provider="messaging",
        retryable=retryable,
        actionable_hint="Check recipient identity, agent availability, and channel/runtime configuration before retrying.",
    )


def _a2a_failure(
    operation: A2AOperation,
    *,
    error_code: str,
    message: str,
    retryable: bool = False,
    status: str = "failed",
) -> A2AOutcome:
    return A2AOutcome.failure(
        operation=operation,
        error_code=error_code,
        message=str(message).lstrip("❌⚠️ ").strip(),
        retryable=retryable,
        status=status,
    )


def _delegation_runtime_failure_outcome(
    operation: A2AOperation,
    result: Any,
) -> A2AOutcome | None:
    """Preserve a child runtime's typed failure instead of treating its prose as a reply."""
    terminal_reason = getattr(result, "terminal_reason", None)
    terminal_reason_value = str(getattr(terminal_reason, "value", terminal_reason) or "")
    runtime_failed = bool(getattr(result, "failed", False)) or terminal_reason_value not in {"", "turn_stop"}
    if not runtime_failed:
        return None
    parts = getattr(result, "parts", ()) or ()
    runtime_status = next(
        (part for part in parts if isinstance(part, dict) and str(part.get("type") or "") == "runtime_status"),
        {},
    )
    error_code = str(runtime_status.get("error_code") or terminal_reason_value or "a2a_child_runtime_failed")
    return _a2a_failure(
        operation,
        error_code=error_code,
        message=str(getattr(result, "content", "") or "The delegated Agent runtime failed."),
        retryable=bool(runtime_status.get("retryable", False)),
        status=str(runtime_status.get("status") or "failed"),
    )


def _principal_from_args(
    args: dict,
    explicit: ExecutionPrincipal | None,
    *,
    operation: A2AOperation,
) -> tuple[ExecutionPrincipal | None, A2AOutcome | None]:
    if explicit is not None:
        return explicit, None
    try:
        return ExecutionPrincipal.from_evidence(args.get("_execution_principal")), None
    except (KeyError, TypeError, ValueError) as exc:
        return None, _a2a_failure(
            operation,
            error_code="invalid_execution_principal",
            message=f"Invalid execution principal: {exc}",
        )


def _effective_a2a_requester(source_agent: Any, principal: ExecutionPrincipal | None) -> uuid.UUID:
    requester = principal.requester_user_id if principal is not None else None
    fallback = getattr(source_agent, "owner_user_id", None) or getattr(source_agent, "creator_id", None)
    owner_id = requester or fallback
    if owner_id is None:
        raise ValueError("source Agent has no effective requester")
    return owner_id


def _parse_bool_arg(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _parse_timeout_seconds_arg(
    value,
    *,
    default_seconds: float,
    max_seconds: float,
    field_name: str = "timeout_seconds",
) -> tuple[float | None, str | None]:
    if value in (None, ""):
        return default_seconds, None
    try:
        timeout_seconds = float(value)
    except (TypeError, ValueError):
        return None, f"❌ {field_name} must be a number"
    if timeout_seconds <= 0:
        return None, f"❌ {field_name} must be greater than zero"
    if timeout_seconds > max_seconds:
        return None, f"❌ {field_name} must be at most {max_seconds:.0f} seconds"
    return timeout_seconds, None


def _channel_extra_config(config) -> dict | None:
    return getattr(config, "extra_config", None)


async def _resolve_target_agent_runtime(
    from_agent_id: uuid.UUID,
    agent_name: str,
    *,
    target_agent_id: uuid.UUID | None = None,
):
    """Resolve source agent, target agent, and target model for A2A delegation."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    # RLS 阶段1: agents/llm_models are policy-bearing — scope to the source
    # agent's tenant (resolved via audited single-row bypass). Cross-tenant A2A
    # is already forbidden by the explicit tenant filters below.
    tid = await resolve_tenant_for_agent(from_agent_id)
    async with tenant_scoped_session(tid) as db:
        src_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        source_agent = src_result.scalar_one_or_none()
        if not source_agent:
            return None, None, None, "❌ Source agent not found"

        if target_agent_id is not None:
            target_result = await db.execute(
                select(Agent).where(
                    Agent.id == target_agent_id,
                    Agent.id != from_agent_id,
                    Agent.tenant_id == source_agent.tenant_id,
                )
            )
            target = target_result.scalar_one_or_none()
        else:
            target_result = await db.execute(
                select(Agent).where(
                    Agent.name.ilike(f"%{agent_name}%"),
                    Agent.id != from_agent_id,
                    Agent.tenant_id == source_agent.tenant_id,
                )
            )
            target = target_result.scalars().first()
        if not target:
            all_r = await db.execute(
                select(Agent).where(Agent.id != from_agent_id, Agent.tenant_id == source_agent.tenant_id)
            )
            names = [a.name for a in all_r.scalars().all()]
            return (
                source_agent,
                None,
                None,
                (f"❌ No agent found matching '{agent_name}'. Available: {', '.join(names) if names else 'none'}"),
            )

        if target.status in ("expired", "stopped", "archived"):
            return (
                source_agent,
                target,
                None,
                (f"⚠️ {target.name} is currently {target.status} and cannot receive messages."),
            )

        policy = await resolve_a2a_collaboration_policy(db, source_agent, target, action="delegate")
        if not policy.allowed:
            return source_agent, target, None, f"❌ {policy.message}"

        target_model = None
        if target.primary_model_id:
            model_r = await db.execute(
                select(LLMModel).where(LLMModel.id == target.primary_model_id, LLMModel.tenant_id == target.tenant_id)
            )
            target_model = model_r.scalar_one_or_none()

        if not target_model and target.fallback_model_id:
            fb_r = await db.execute(
                select(LLMModel).where(LLMModel.id == target.fallback_model_id, LLMModel.tenant_id == target.tenant_id)
            )
            target_model = fb_r.scalar_one_or_none()
            if target_model:
                logger.warning(
                    "[A2A] Primary model unavailable for %s, using fallback: %s",
                    target.name,
                    target_model.model,
                )

        if not target_model:
            return source_agent, target, None, f"⚠️ {target.name} has no LLM model configured"

        return source_agent, target, target_model, None


async def _send_feishu_message(agent_id: uuid.UUID, args: dict) -> str:
    """Send a Feishu message to a person in the agent's relationship list."""
    member_name = (args.get("member_name") or "").strip()
    direct_open_id = (args.get("open_id") or "").strip()
    direct_user_id = (args.get("user_id") or "").strip()
    message_text = (args.get("message") or "").strip()

    if not message_text:
        return "❌ Please provide message content"
    if not member_name and not direct_open_id and not direct_user_id:
        return "❌ Please provide member_name, user_id, or open_id"

    try:
        from app.models.agent import Agent
        from app.models.org import AgentRelationship, OrgMember
        from app.models.channel_config import ChannelConfig
        from app.services.channel_user_service import channel_user_service
        from app.services.feishu_service import feishu_service
        from sqlalchemy.orm import selectinload

        # RLS 阶段1: agents/users/org_members are policy-bearing — scope to the
        # agent's tenant (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:

            async def _safe_send_text_message(
                app_id: str,
                app_secret: str,
                *,
                receive_id: str,
                content: str,
                receive_id_type: str = "open_id",
                extra_config: dict | None = None,
            ) -> dict:
                try:
                    return await feishu_service.send_message(
                        app_id,
                        app_secret,
                        receive_id=receive_id,
                        msg_type="text",
                        content=content,
                        receive_id_type=receive_id_type,
                        extra_config=extra_config,
                    )
                except Exception as exc:
                    logger.warning(
                        "[Feishu Send] app_id=%s receive_id_type=%s receive_id=%s failed: %s",
                        app_id,
                        receive_id_type,
                        receive_id,
                        exc,
                    )
                    return {"code": -1, "msg": str(exc)}

            async def _save_outgoing_to_feishu_session(stable_user_id: str | None, stable_open_id: str | None):
                """Save the outgoing message to the Feishu P2P chat session."""
                try:
                    from app.models.audit import ChatMessage
                    from app.models.agent import Agent as AgentModel
                    from app.services.feishu_identity_maintenance import find_or_create_feishu_chat_session
                    from datetime import datetime as _dt, timezone as _tz

                    agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                    agent_obj = agent_r.scalar_one_or_none()
                    creator_id = agent_obj.creator_id if agent_obj else agent_id

                    feishu_user = await channel_user_service.resolve_feishu_user(
                        db,
                        tenant_id=agent_obj.tenant_id if agent_obj else None,
                        provider_user_id=stable_user_id or None,
                        provider_open_id=stable_open_id or None,
                    )
                    user_id = feishu_user.id if feishu_user else creator_id

                    sess = await find_or_create_feishu_chat_session(
                        db=db,
                        agent_id=agent_id,
                        tenant_id=agent_obj.tenant_id if agent_obj else None,
                        user_id=user_id,
                        provider_user_id=stable_user_id,
                        provider_open_id=stable_open_id,
                        first_message_title=f"[Agent → {member_name}]",
                    )
                    db.add(
                        ChatMessage(
                            agent_id=agent_id,
                            tenant_id=agent_obj.tenant_id if agent_obj else None,
                            user_id=user_id,
                            role="assistant",
                            content=message_text,
                            conversation_id=str(sess.id),
                        )
                    )
                    sess.last_message_at = _dt.now(_tz.utc)
                    await db.commit()
                    logger.info(f"[Feishu] Saved outgoing message to session {sess.id} ({member_name})")
                except Exception as e:
                    logger.error(f"[Feishu] Failed to save outgoing message to history: {e}")

            # ── Resolve agent tenant_id for recipient validation ──
            _agent_r = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
            _agent_tenant_id = _agent_r.scalar_one_or_none()

            # ── Shortcut: if caller provided user_id or open_id directly ──
            if (direct_user_id or direct_open_id) and not member_name:
                # Validate recipient belongs to same tenant (prevent cross-tenant sends)
                if not _agent_tenant_id:
                    return "❌ Agent has no tenant configured, cannot validate recipient. Please contact admin."
                _recipient_ok = False
                _validated_member = None
                if direct_user_id:
                    _check = await db.execute(
                        select(OrgMember).where(
                            OrgMember.feishu_user_id == direct_user_id,
                            OrgMember.tenant_id == _agent_tenant_id,
                        )
                    )
                    _validated_member = _check.scalar_one_or_none()
                    _recipient_ok = _validated_member is not None
                if not _recipient_ok and direct_open_id:
                    _check = await db.execute(
                        select(OrgMember).where(
                            OrgMember.feishu_open_id == direct_open_id,
                            OrgMember.tenant_id == _agent_tenant_id,
                        )
                    )
                    _validated_member = _check.scalar_one_or_none()
                    _recipient_ok = _validated_member is not None
                if not _recipient_ok:
                    return (
                        f"❌ 无法验证收件人身份：user_id={direct_user_id or ''}, open_id={direct_open_id or ''}。"
                        f"该用户不在本组织通讯录中，已阻止发送。"
                    )
                canonical_user_id = (
                    direct_user_id
                    or getattr(_validated_member, "feishu_user_id", None)
                    or getattr(
                        _validated_member,
                        "external_id",
                        None,
                    )
                )
                canonical_open_id = (
                    direct_open_id
                    or getattr(_validated_member, "feishu_open_id", None)
                    or getattr(
                        _validated_member,
                        "open_id",
                        None,
                    )
                )

                config_result = await db.execute(
                    select(ChannelConfig).where(
                        ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu"
                    )
                )
                config = config_result.scalar_one_or_none()
                if not config:
                    return "❌ This agent has no Feishu channel configured"
                import json as _j

                # Prefer user_id over open_id
                if direct_user_id:
                    resp = await _safe_send_text_message(
                        config.app_id,
                        config.app_secret,
                        receive_id=direct_user_id,
                        content=_j.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type="user_id",
                        extra_config=_channel_extra_config(config),
                    )
                    if resp.get("code") == 0:
                        args["user_id"] = direct_user_id
                        if canonical_open_id:
                            args["open_id"] = canonical_open_id
                        await _save_outgoing_to_feishu_session(direct_user_id, canonical_open_id)
                        return f"✅ 消息已发送（user_id: {direct_user_id}）"
                    # Fallback to open_id if user_id fails
                    if direct_open_id:
                        resp = await _safe_send_text_message(
                            config.app_id,
                            config.app_secret,
                            receive_id=direct_open_id,
                            content=_j.dumps({"text": message_text}, ensure_ascii=False),
                            receive_id_type="open_id",
                            extra_config=_channel_extra_config(config),
                        )
                        if resp.get("code") == 0:
                            args["open_id"] = direct_open_id
                            if canonical_user_id:
                                args["user_id"] = canonical_user_id
                            await _save_outgoing_to_feishu_session(canonical_user_id, direct_open_id)
                            return f"✅ 消息已发送（open_id: {direct_open_id}）"
                    return f"❌ 发送失败：{resp.get('msg')} (code {resp.get('code')})"
                else:
                    resp = await _safe_send_text_message(
                        config.app_id,
                        config.app_secret,
                        receive_id=direct_open_id,
                        content=_j.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type="open_id",
                        extra_config=_channel_extra_config(config),
                    )
                    if resp.get("code") == 0:
                        args["open_id"] = direct_open_id
                        if canonical_user_id:
                            args["user_id"] = canonical_user_id
                        await _save_outgoing_to_feishu_session(canonical_user_id, direct_open_id)
                        return f"✅ 消息已发送（open_id: {direct_open_id}）"
                    return f"❌ 发送失败：{resp.get('msg')} (code {resp.get('code')})"

            # Find the relationship member by name
            result = await db.execute(
                select(AgentRelationship)
                .where(AgentRelationship.agent_id == agent_id)
                .options(selectinload(AgentRelationship.member))
            )
            rels = result.scalars().all()

            target_member = None
            for r in rels:
                if r.member and r.member.name == member_name:
                    target_member = r.member
                    break

            # ── Fallback: check if recipient matches agent owner/creator ──
            if not target_member:
                from app.models.user import User as _UserModel

                _owner_r = await db.execute(select(Agent).where(Agent.id == agent_id))
                _agent_obj = _owner_r.scalar_one_or_none()
                _owner_id = _agent_obj.owner_user_id or _agent_obj.creator_id if _agent_obj else None
                if _owner_id:
                    _owner_r2 = await db.execute(select(_UserModel).where(_UserModel.id == _owner_id))
                    _owner_user = _owner_r2.scalar_one_or_none()
                    if _owner_user and member_name.lower() in (_owner_user.display_name or "").lower():
                        # Owner matched by name — resolve feishu credentials
                        _owner_feishu_uid = _owner_user.feishu_user_id
                        _owner_feishu_oid = _owner_user.feishu_open_id
                        # If owner has no feishu binding, try matching via email in OrgMember
                        if not _owner_feishu_uid and not _owner_feishu_oid and _owner_user.email and _agent_tenant_id:
                            _om_r = await db.execute(
                                select(OrgMember).where(
                                    OrgMember.tenant_id == _agent_tenant_id,
                                    OrgMember.email == _owner_user.email,
                                )
                            )
                            _om = _om_r.scalar_one_or_none()
                            if _om:
                                _owner_feishu_uid = _om.feishu_user_id
                                _owner_feishu_oid = _om.feishu_open_id
                        if _owner_feishu_uid or _owner_feishu_oid:
                            target_member = type(
                                "_OwnerAsMember",
                                (),
                                {
                                    "name": _owner_user.display_name,
                                    "feishu_user_id": _owner_feishu_uid,
                                    "feishu_open_id": _owner_feishu_oid,
                                    "email": _owner_user.email,
                                    "phone": None,
                                },
                            )()

            if not target_member:
                resolved_target = await channel_user_service.resolve_feishu_delivery_target_by_name(
                    db,
                    agent_id=agent_id,
                    tenant_id=_agent_tenant_id,
                    member_name=member_name,
                )
                if resolved_target:
                    resolved_id, resolved_id_type = resolved_target
                    config_result = await db.execute(
                        select(ChannelConfig).where(
                            ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu"
                        )
                    )
                    config = config_result.scalar_one_or_none()
                    if not config:
                        return "❌ This agent has no Feishu channel configured"
                    import json as _j_prior

                    resp = await _safe_send_text_message(
                        config.app_id,
                        config.app_secret,
                        receive_id=resolved_id,
                        content=_j_prior.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type=resolved_id_type,
                        extra_config=_channel_extra_config(config),
                    )
                    if resp.get("code") == 0:
                        args[resolved_id_type] = resolved_id
                        await _save_outgoing_to_feishu_session(
                            resolved_id if resolved_id_type == "user_id" else None,
                            resolved_id if resolved_id_type == "open_id" else None,
                        )
                        return f"✅ 消息已成功发送给 {member_name}"

                # ── Fallback: look up via feishu_user_search (contacts cache / OrgMember / User) ──
                _search_result = await _feishu_user_search(agent_id, {"name": member_name})
                # Prefer user_id over open_id
                import re as _re_oid

                _uid_match = _re_oid.search(r"user_id: `([A-Za-z0-9]+)`", _search_result)
                _oid_match = _re_oid.search(r"open_id: `(ou_[A-Za-z0-9]+)`", _search_result)
                _found_id = None
                _found_id_type = None
                if _uid_match:
                    _found_id = _uid_match.group(1)
                    _found_id_type = "user_id"
                elif _oid_match:
                    _found_id = _oid_match.group(1)
                    _found_id_type = "open_id"
                if _found_id:
                    config_result = await db.execute(
                        select(ChannelConfig).where(
                            ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu"
                        )
                    )
                    config = config_result.scalar_one_or_none()
                    if not config:
                        return "❌ This agent has no Feishu channel configured"
                    import json as _j2

                    resp = await _safe_send_text_message(
                        config.app_id,
                        config.app_secret,
                        receive_id=_found_id,
                        content=_j2.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type=_found_id_type,
                        extra_config=_channel_extra_config(config),
                    )
                    if resp.get("code") == 0:
                        args[_found_id_type] = _found_id
                        await _save_outgoing_to_feishu_session(
                            _found_id if _found_id_type == "user_id" else None,
                            _found_id if _found_id_type == "open_id" else None,
                        )
                        return f"✅ 消息已成功发送给 {member_name}"
                    return f"❌ 找到了 {member_name}（{_found_id_type}: {_found_id}）但发送失败：{resp.get('msg')} (code {resp.get('code')})"
                # Could not find via any path
                names = [r.member.name for r in rels if r.member]
                return (
                    f"❌ 未找到联系人「{member_name}」。\n"
                    f"关系列表中的联系人：{', '.join(names) if names else '（空）'}\n"
                    f"通讯录搜索结果：{_search_result}"
                )

            if (
                not target_member.feishu_user_id
                and not target_member.feishu_open_id
                and not target_member.email
                and not target_member.phone
            ):
                return f"❌ {member_name} has no linked Feishu account (no user_id, open_id, email, or phone)"

            # Get the agent's Feishu bot credentials
            config_result = await db.execute(
                select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu")
            )
            config = config_result.scalar_one_or_none()
            if not config:
                return "❌ This agent has no Feishu channel configured"

            import json as _json

            content = _json.dumps({"text": message_text}, ensure_ascii=False)

            async def _try_send(
                app_id: str,
                app_secret: str,
                receive_id: str,
                id_type: str = "open_id",
                *,
                extra_config: dict | None = None,
            ) -> dict:
                return await _safe_send_text_message(
                    app_id,
                    app_secret,
                    receive_id=receive_id,
                    content=content,
                    receive_id_type=id_type,
                    extra_config=extra_config if extra_config is not None else _channel_extra_config(config),
                )

            stable_user_id = target_member.external_id or target_member.feishu_user_id
            stable_open_id = target_member.open_id or target_member.feishu_open_id

            # Step 1: Try using provider user_id (tenant-stable, works across apps)
            if stable_user_id:
                resp = await _try_send(config.app_id, config.app_secret, stable_user_id, "user_id")
                if resp.get("code") == 0:
                    args["user_id"] = stable_user_id
                    await _save_outgoing_to_feishu_session(stable_user_id, stable_open_id)
                    return f"✅ Successfully sent message to {member_name}"
                logger.warning("[Feishu Send] Step1 user_id=%s failed: %s", stable_user_id, resp)

            # Step 2: Try resolve open_id via email/phone
            if target_member.email or target_member.phone:
                try:
                    resolved = await feishu_service.resolve_open_id(
                        config.app_id,
                        config.app_secret,
                        email=target_member.email,
                        mobile=target_member.phone,
                        extra_config=_channel_extra_config(config),
                    )
                    if resolved:
                        resp = await _try_send(config.app_id, config.app_secret, resolved)
                        if resp.get("code") == 0:
                            args["open_id"] = resolved
                            target_member.open_id = resolved
                            target_member.feishu_open_id = resolved
                            await db.commit()
                            await _save_outgoing_to_feishu_session(stable_user_id, resolved)
                            return f"✅ Successfully sent message to {member_name}"
                except Exception as e:
                    logger.debug("Suppressed: %s", e)
            if stable_open_id:
                resp = await _try_send(config.app_id, config.app_secret, stable_open_id)
                if resp.get("code") == 0:
                    args["open_id"] = stable_open_id
                    if stable_user_id:
                        args["user_id"] = stable_user_id
                    await _save_outgoing_to_feishu_session(stable_user_id, stable_open_id)
                    return f"✅ Successfully sent message to {member_name}"
                logger.warning("[Feishu Send] Step3 open_id=%s failed: %s", stable_open_id, resp)

                # Step 4: If cross-app error, try org sync app as fallback
                err_msg = resp.get("msg", "")
                if "cross" in err_msg.lower():
                    from app.models.tenant_setting import TenantSetting

                    org_r = await db.execute(
                        select(TenantSetting).where(
                            TenantSetting.tenant_id == target_member.tenant_id,
                            TenantSetting.key == "feishu_org_sync",
                        )
                    )
                    org_setting = org_r.scalar_one_or_none()
                    if org_setting and org_setting.value.get("app_id"):
                        # Try user_id with org sync app first
                        if stable_user_id:
                            resp2 = await _try_send(
                                org_setting.value["app_id"],
                                org_setting.value["app_secret"],
                                stable_user_id,
                                "user_id",
                                extra_config=org_setting.value,
                            )
                            if resp2.get("code") == 0:
                                args["user_id"] = stable_user_id
                                await _save_outgoing_to_feishu_session(stable_user_id, stable_open_id)
                                return f"✅ Successfully sent message to {member_name}"
                        # Fallback to open_id with org sync app
                        resp2 = await _try_send(
                            org_setting.value["app_id"],
                            org_setting.value["app_secret"],
                            stable_open_id,
                            extra_config=org_setting.value,
                        )
                        if resp2.get("code") == 0:
                            args["open_id"] = stable_open_id
                            await _save_outgoing_to_feishu_session(stable_user_id, stable_open_id)
                            return f"✅ Successfully sent message to {member_name}"
                        return f"❌ Send failed: {resp2.get('msg', str(resp2))}"

                return f"❌ Send failed: {err_msg}"

            return f"❌ {member_name} has no Feishu user_id/open_id and cannot be resolved via email/phone"
    except Exception as e:
        return f"❌ Message send error: {str(e)}"


async def _send_web_message(agent_id: uuid.UUID, args: dict) -> str:
    """Send a proactive message to a web platform user."""
    username = args.get("username", "").strip()
    message_text = args.get("message", "").strip()

    if not username or not message_text:
        return "❌ Please provide recipient username and message content"

    try:
        from app.models.user import User as UserModel
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.services.web_session_contract import apply_web_session_contract
        from datetime import datetime as _dt, timezone as _tz

        # RLS 阶段1: users (policy-bearing) read — scope to the agent's tenant
        # (resolved via audited single-row bypass).
        tid = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tid) as db:
            # Resolve agent tenant for scoped query
            from app.models.agent import Agent as _AgentModel

            _ag_r = await db.execute(select(_AgentModel.tenant_id).where(_AgentModel.id == agent_id))
            _agent_tenant = _ag_r.scalar_one_or_none()

            # Look up target user by username or display_name (scoped to same tenant)
            from sqlalchemy import or_

            _user_query = select(UserModel).where(
                or_(
                    UserModel.username == username,
                    UserModel.display_name == username,
                )
            )
            if _agent_tenant:
                _user_query = _user_query.where(UserModel.tenant_id == _agent_tenant)
            u_result = await db.execute(_user_query)
            target_user = u_result.scalar_one_or_none()
            if not target_user:
                _avail_query = select(UserModel.username, UserModel.display_name).limit(20)
                if _agent_tenant:
                    _avail_query = _avail_query.where(UserModel.tenant_id == _agent_tenant)
                all_r = await db.execute(_avail_query)
                names = [f"{r.display_name or r.username}" for r in all_r.all()]
                return f"❌ No user named '{username}' found. Available users: {', '.join(names) if names else 'none'}"

            # Find or create a web session between the agent and this user
            sess_r = await db.execute(
                select(ChatSession)
                .where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == target_user.id,
                    ChatSession.source_channel == "web",
                )
                .order_by(ChatSession.created_at.desc())
                .limit(1)
            )
            session = sess_r.scalar_one_or_none()

            if not session:
                session = ChatSession(
                    agent_id=agent_id,
                    tenant_id=_agent_tenant,
                    user_id=target_user.id,
                    title=f"[Agent Message] {_dt.now(_tz.utc).strftime('%m-%d %H:%M')}",
                    source_channel="web",
                    created_at=_dt.now(_tz.utc),
                )
                db.add(session)
                await db.flush()
            await apply_web_session_contract(db, session=session, agent_id=agent_id, user=target_user)

            # Save the message. RLS 阶段2b: chat_messages is USING-only — stamp
            # tenant_id so the row isn't globally visible (chat_sessions too).
            db.add(
                ChatMessage(
                    agent_id=agent_id,
                    tenant_id=_agent_tenant,
                    user_id=target_user.id,
                    role="assistant",
                    content=message_text,
                    conversation_id=str(session.id),
                )
            )
            session.last_message_at = _dt.now(_tz.utc)
            await db.commit()

            # Push via WebSocket if user has an active connection
            try:
                from app.api.websocket import manager as ws_manager

                agent_id_str = str(agent_id)
                if agent_id_str in ws_manager.active_connections:
                    for ws, sid in list(ws_manager.active_connections[agent_id_str]):
                        try:
                            await ws.send_json(
                                {
                                    "type": "trigger_notification",
                                    "content": message_text,
                                    "triggers": ["web_message"],
                                }
                            )
                        except Exception as e:
                            logger.debug("Suppressed: %s", e)
            except Exception as e:
                logger.debug("Suppressed: %s", e)

            display = target_user.display_name or target_user.username
            return f"✅ Message sent to {display} on web platform. It has been saved to their chat history."

    except Exception as e:
        return f"❌ Web message send error: {str(e)}"


async def _persist_agent_tool_call(
    session_agent_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: str,
    participant_id: uuid.UUID | None,
    tool_name: str,
    tool_args: dict,
    tool_result: str,
) -> None:
    """Persist A2A tool execution so it remains visible in the shared chat session."""
    from app.services.chat_transcript import append_session_event

    try:
        # RLS 阶段2b: chat_messages now bears a USING-only policy. Scope to the
        # session agent's tenant and stamp tenant_id — a bare session would
        # write a NULL (globally visible) row and fail closed under the
        # non-owner role.
        tid = await resolve_tenant_for_agent(session_agent_id)
        async with tenant_scoped_session(tid) as db:
            await append_session_event(
                db=db,
                agent_id=session_agent_id,
                tenant_id=tid,
                session_id=session_id,
                actor_type="tool",
                event_type="tool_result",
                role="tool_call",
                t0_role="tool",
                user_id=owner_id,
                participant_id=participant_id,
                content=json.dumps(
                    {
                        "name": tool_name,
                        "args": tool_args,
                        "status": "done",
                        "result": str(tool_result),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ),
                source="agent_message",
                visibility_scope="agent_owner",
                listed_surface="chat",
                metadata={
                    "source": "agent_message",
                    "interaction_type": "agent_message",
                    "tool_name": tool_name,
                    "status": "done",
                    "semantic_memory_eligible": True,
                },
            )
            await db.commit()
    except Exception as exc:
        logger.error(f"[A2A] Failed to save tool_call: {exc}")


def _build_agent_message_tool_executor(
    target_agent_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_agent_id: uuid.UUID,
    session_id: str,
    participant_id: uuid.UUID | None,
    delegation_trace_id: str | None = None,
    delegation_depth: int = 1,
    delegation_max_depth: int = 2,
):
    """Wrap A2A tool execution with chat-history persistence."""
    message_session_id = session_id

    async def _executor(
        tool_name: str,
        tool_args: dict,
        *,
        authority_frame: A2AToolAuthorityFrame | None = None,
        event_callback: Any | None = None,
        tool_call_id: str | None = None,
        trace_metadata_sink: dict[str, Any] | None = None,
        turn_id: str | None = None,
        origin_channel: str | None = None,
        round_state: dict[str, Any] | None = None,
        t0_refs: tuple[str, ...] = (),
        plan_mode_interactive_available: bool = False,
        plan_mode_unattended_available: bool = False,
        emit_runtime_hooks: bool = True,
    ) -> str:
        from app.services.agent_tools import execute_tool

        effective_authority_frame = authority_frame or A2AToolAuthorityFrame(
            schema=None,
            principal=None,
            capability_snapshot_hash=None,
            policy_snapshot_hash=None,
            required=True,
        )
        effective_args = dict(tool_args)
        if tool_name in {"send_message_to_agent", "delegate_to_agent", "spawn_subagent"}:
            effective_args.update(
                {
                    "_a2a_trace_id": delegation_trace_id,
                    "_a2a_depth": delegation_depth + 1,
                    "_a2a_max_depth": delegation_max_depth,
                }
            )
        tool_result = await execute_tool(
            tool_name,
            effective_args,
            target_agent_id,
            owner_id,
            execution_identity=effective_authority_frame.execution_identity,
            authority_frame=effective_authority_frame,
            delegation_token=effective_authority_frame.delegation_token,
            event_callback=event_callback,
            permission_profile=effective_authority_frame.permission_profile,
            tool_call_id=tool_call_id,
            trace_metadata_sink=trace_metadata_sink,
            session_id=effective_authority_frame.session_id or message_session_id,
            turn_id=turn_id,
            runtime_task_id=effective_authority_frame.runtime_task_id,
            budget_run_id=effective_authority_frame.budget_run_id,
            origin_channel=origin_channel,
            round_state=round_state,
            t0_refs=t0_refs,
            plan_mode_interactive_available=plan_mode_interactive_available,
            plan_mode_unattended_available=plan_mode_unattended_available,
            emit_runtime_hooks=emit_runtime_hooks,
        )
        await _persist_agent_tool_call(
            session_agent_id=session_agent_id,
            owner_id=owner_id,
            session_id=message_session_id,
            participant_id=participant_id,
            tool_name=tool_name,
            tool_args=effective_args,
            tool_result=tool_result,
        )
        return tool_result

    return _executor


async def _invoke_agent_message_runtime(
    *,
    target,
    target_model,
    conversation_messages: list[dict],
    from_agent_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_id: str,
    session_agent_id: uuid.UUID,
    participant_id: uuid.UUID | None,
    permission_profile: Any | None = None,
    parent_session_id: str | None = None,
    execution_principal: dict[str, Any] | None = None,
    root_runtime_task_id: str | None = None,
    delegation_trace_id: str | None = None,
    delegation_depth: int = 1,
    delegation_max_depth: int = 2,
) -> Any:
    """Run the target agent reply through the shared runtime kernel."""
    from app.agents.orchestrator import AGENT_MESSAGE_TIMEOUT_SECONDS, OrchestrationPolicy, delegate_to_agent

    return await delegate_to_agent(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        owner_id=owner_id,
        session_id=session_id,
        parent_agent_id=from_agent_id,
        parent_session_id=parent_session_id or session_id,
        trace_id=delegation_trace_id or f"a2a:{session_id}:{from_agent_id}:{target.id}",
        depth=delegation_depth,
        tool_executor=_build_agent_message_tool_executor(
            target_agent_id=target.id,
            owner_id=owner_id,
            session_agent_id=session_agent_id,
            session_id=session_id,
            participant_id=participant_id,
            delegation_trace_id=delegation_trace_id or f"a2a:{session_id}:{from_agent_id}:{target.id}",
            delegation_depth=delegation_depth,
            delegation_max_depth=delegation_max_depth,
        ),
        system_prompt_suffix=A2A_SYSTEM_PROMPT_SUFFIX,
        max_tool_rounds=getattr(target, "max_tool_rounds", None) or 200,
        interaction_type="agent_message",
        # A2A is a real multi-tool turn (the target may call feishu_wiki_list,
        # read documents, and synthesize a final answer). Keep the inner budget
        # below the tool wrapper cap, but well above short consult latency.
        policy=OrchestrationPolicy(
            max_depth=delegation_max_depth,
            timeout_seconds=AGENT_MESSAGE_TIMEOUT_SECONDS,
            tool_profile="agent_message",
        ),
        permission_profile=permission_profile,
        execution_principal=execution_principal,
        root_runtime_task_id=root_runtime_task_id,
        return_result=True,
    )


def _normalize_delegate_tool_profile(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw or raw == "peer_agent":
        return "agent_message"
    return raw


async def _send_message_to_agent_outcome(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    principal: ExecutionPrincipal | None = None,
) -> A2AOutcome:
    """Send a message to another digital employee. Uses a single request-response pattern:
    the source agent sends a message, the target agent replies once, and the result is returned.
    If the source agent needs to continue the conversation, it can call this tool again.
    """
    agent_name = args.get("agent_name", "").strip()
    message_text = args.get("message", "").strip()
    target_agent_id = None
    target_agent_id_raw = args.get("target_agent_id")
    principal, principal_error = _principal_from_args(args, principal, operation="consult")
    if principal_error is not None:
        return principal_error
    if target_agent_id_raw:
        try:
            target_agent_id = uuid.UUID(str(target_agent_id_raw))
        except (TypeError, ValueError, AttributeError):
            return _a2a_failure("consult", error_code="invalid_arguments", message="target_agent_id is invalid")

    if (not agent_name and target_agent_id is None) or not message_text:
        return _a2a_failure(
            "consult",
            error_code="invalid_arguments",
            message="Please provide target agent name and message content",
        )

    try:
        delegation_depth = max(1, int(args.get("_a2a_depth") or 1))
        delegation_max_depth = max(1, int(args.get("_a2a_max_depth") or 2))
    except (TypeError, ValueError):
        return _a2a_failure("consult", error_code="invalid_delegation_context", message="Invalid A2A depth context")
    if delegation_depth > delegation_max_depth:
        return _a2a_failure(
            "consult",
            error_code="delegation_depth_limited",
            message=f"Delegation depth limit reached ({delegation_depth}/{delegation_max_depth})",
            status="blocked",
        )
    delegated_trace_id = str(args.get("_a2a_trace_id") or "").strip() or None

    try:
        from app.models.agent import Agent
        from app.models.audit import ChatMessage
        from app.services.chat_transcript import append_session_event
        from app.services.agent_pair_session import (
            find_or_create_agent_pair_session,
            get_or_create_agent_participant_id,
        )

        # RLS 阶段1: agents/llm_models are policy-bearing — scope to the source
        # agent's tenant (resolved via audited single-row bypass). Target lookup
        # is already tenant-filtered below.
        tid = await resolve_tenant_for_agent(from_agent_id)
        async with tenant_scoped_session(tid) as db:
            # Look up source agent
            src_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
            source_agent = src_result.scalar_one_or_none()
            source_name = source_agent.name if source_agent else "Unknown agent"
            if source_agent is None:
                return _a2a_failure("consult", error_code="source_agent_not_found", message="Source agent not found")
            if principal is not None:
                try:
                    principal.assert_scope(tenant_id=source_agent.tenant_id, source_agent_id=from_agent_id)
                except ValueError as exc:
                    return _a2a_failure(
                        "consult",
                        error_code="principal_scope_mismatch",
                        message=str(exc),
                    )

            # Find target agent by id or name (scoped to same tenant)
            if target_agent_id is not None:
                _tenant_filter = [Agent.id == target_agent_id, Agent.id != from_agent_id]
                if source_agent and source_agent.tenant_id:
                    _tenant_filter.append(Agent.tenant_id == source_agent.tenant_id)
                result = await db.execute(select(Agent).where(*_tenant_filter))
                target = result.scalar_one_or_none()
            else:
                _tenant_filter = [Agent.name.ilike(f"%{agent_name}%"), Agent.id != from_agent_id]
                if source_agent and source_agent.tenant_id:
                    _tenant_filter.append(Agent.tenant_id == source_agent.tenant_id)
                result = await db.execute(select(Agent).where(*_tenant_filter))
                target = result.scalars().first()
            if not target:
                _avail_filter = [Agent.id != from_agent_id]
                if source_agent and source_agent.tenant_id:
                    _avail_filter.append(Agent.tenant_id == source_agent.tenant_id)
                all_r = await db.execute(select(Agent).where(*_avail_filter))
                names = [a.name for a in all_r.scalars().all()]
                return _a2a_failure(
                    "consult",
                    error_code="target_agent_not_found",
                    message=f"No agent found matching '{agent_name}'. Available: {', '.join(names) if names else 'none'}",
                )

            if target.status in ("expired", "stopped", "archived"):
                return _a2a_failure(
                    "consult",
                    error_code="target_agent_unavailable",
                    message=f"{target.name} is currently {target.status} and cannot receive messages.",
                    retryable=True,
                )

            policy = await resolve_a2a_collaboration_policy(db, source_agent, target, action="message")
            if not policy.allowed:
                return _a2a_failure("consult", error_code="a2a_policy_denied", message=policy.message)

            owner_id = _effective_a2a_requester(source_agent, principal)
            src_participant_id = await get_or_create_agent_participant_id(
                db,
                agent_id=from_agent_id,
                display_name=source_name,
                avatar_url=getattr(source_agent, "avatar_url", None),
            )
            tgt_participant_id = await get_or_create_agent_participant_id(
                db,
                agent_id=target.id,
                display_name=target.name,
                avatar_url=getattr(target, "avatar_url", None),
            )
            chat_session = await find_or_create_agent_pair_session(
                db,
                source_agent_id=from_agent_id,
                target_agent_id=target.id,
                owner_user_id=owner_id,
                source_agent_name=source_name,
                target_agent_name=target.name,
                source_participant_id=src_participant_id,
                root_session_id=principal.root_session_id if principal else None,
            )
            session_id = str(chat_session.id)
            session_agent_id = chat_session.agent_id

            # Prepare target LLM
            from app.models.llm import LLMModel

            # Load primary model (with fallback support)
            target_model = None
            if target.primary_model_id:
                model_r = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == target.primary_model_id, LLMModel.tenant_id == target.tenant_id
                    )
                )
                target_model = model_r.scalar_one_or_none()

            # Config-level fallback: primary missing -> use fallback
            if not target_model and target.fallback_model_id:
                fb_r = await db.execute(
                    select(LLMModel).where(
                        LLMModel.id == target.fallback_model_id, LLMModel.tenant_id == target.tenant_id
                    )
                )
                target_model = fb_r.scalar_one_or_none()
                if target_model:
                    logger.warning(
                        f"[A2A] Primary model unavailable for {target.name}, using fallback: {target_model.model}"
                    )

            if not target_model:
                return _a2a_failure(
                    "consult",
                    error_code="target_model_not_configured",
                    message=f"{target.name} has no LLM model configured",
                )

            # Load recent history for context
            conversation_messages: list[dict] = []
            hist_result = await db.execute(
                select(ChatMessage)
                .where(
                    ChatMessage.conversation_id == session_id,
                    ChatMessage.agent_id == session_agent_id,
                )
                .order_by(ChatMessage.created_at.desc())
                .limit(20)
            )
            for m in reversed(hist_result.scalars().all()):
                if m.participant_id and src_participant_id and m.participant_id == src_participant_id:
                    role = "user"
                else:
                    role = "assistant"
                conversation_messages.append({"role": role, "content": m.content})

            # Add the new message from source
            conversation_messages.append({"role": "user", "content": f"[From {source_name}] {message_text}"})

            # Save source message. RLS 阶段2b: chat_messages is USING-only —
            # stamp tenant_id so the row isn't globally visible.
            await append_session_event(
                db=db,
                agent_id=session_agent_id,
                tenant_id=tid,
                session_id=session_id,
                actor_type="agent",
                event_type="user_message",
                role="user",
                user_id=owner_id,
                participant_id=src_participant_id,
                content=message_text,
                source="agent_message",
                visibility_scope="agent_owner",
                listed_surface="chat",
                metadata={
                    "source": "agent_message",
                    "interaction_type": "agent_message",
                    "a2a_policy_reason": policy.reason,
                    "a2a_collaboration_group_id": str(policy.group_id) if policy.group_id else None,
                    "a2a_collaboration_group_name": policy.group_name,
                    "from_agent": str(from_agent_id),
                    "from_agent_name": source_name,
                    "to_agent": str(target.id),
                    "to_agent_name": target.name,
                    "semantic_memory_eligible": True,
                    "execution_principal": principal.to_evidence() if principal else None,
                },
            )
            chat_session.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            target_result = await _invoke_agent_message_runtime(
                target=target,
                target_model=target_model,
                conversation_messages=conversation_messages,
                from_agent_id=from_agent_id,
                owner_id=owner_id,
                session_id=session_id,
                session_agent_id=session_agent_id,
                participant_id=tgt_participant_id,
                permission_profile=args.get("_permission_profile"),
                parent_session_id=principal.root_session_id if principal else None,
                execution_principal=principal.to_evidence() if principal else None,
                root_runtime_task_id=principal.root_runtime_task_id if principal else None,
                delegation_trace_id=delegated_trace_id,
                delegation_depth=delegation_depth,
                delegation_max_depth=delegation_max_depth,
            )

            runtime_failure = _delegation_runtime_failure_outcome("consult", target_result)
            if runtime_failure is not None:
                return runtime_failure

            target_reply = str(getattr(target_result, "content", target_result) or "")
            child_invocation = (
                target_result.to_dict().get("child_invocation")
                if hasattr(target_result, "to_dict")
                else {
                    "trace_id": delegated_trace_id or f"a2a:{session_id}:{from_agent_id}:{target.id}",
                    "session_id": session_id,
                    "parts": [],
                    "artifact_refs": [],
                    "terminal_reason": None,
                    "legacy_evidence_unavailable": True,
                }
            )

            if not target_reply:
                return _a2a_failure(
                    "consult",
                    error_code="empty_target_response",
                    message=f"{target.name} did not respond (LLM returned empty)",
                    retryable=True,
                )

            # Save target reply. Re-open a fresh tenant-scoped session (`db`
            # above may be detached after the long runtime invoke) and stamp
            # tenant_id — RLS 阶段2b chat_messages is USING-only, so a NULL
            # tenant_id would be globally visible.
            async with tenant_scoped_session(tid) as db2:
                await append_session_event(
                    db=db2,
                    agent_id=session_agent_id,
                    tenant_id=tid,
                    session_id=session_id,
                    actor_type="assistant",
                    event_type="assistant_message",
                    role="assistant",
                    user_id=owner_id,
                    participant_id=tgt_participant_id,
                    content=target_reply,
                    source="agent_message",
                    visibility_scope="agent_owner",
                    listed_surface="chat",
                    metadata={
                        "source": "agent_message",
                        "interaction_type": "agent_message",
                        "a2a_policy_reason": policy.reason,
                        "a2a_collaboration_group_id": str(policy.group_id) if policy.group_id else None,
                        "a2a_collaboration_group_name": policy.group_name,
                        "from_agent": str(from_agent_id),
                        "from_agent_name": source_name,
                        "to_agent": str(target.id),
                        "to_agent_name": target.name,
                        "semantic_memory_eligible": True,
                        "execution_principal": principal.to_evidence() if principal else None,
                        "child_invocation": child_invocation,
                    },
                )
                await db2.commit()

            # Log activity
            from app.services.activity_logger import log_activity

            await log_activity(
                target.id,
                "agent_msg_sent",
                f"Replied to message from {source_name}",
                detail={"partner": source_name, "message": message_text, "reply": target_reply},
            )
            await log_activity(
                from_agent_id,
                "agent_msg_sent",
                f"Sent message to {target.name} and received reply",
                detail={"partner": target.name, "message": message_text, "reply": target_reply},
            )

            return A2AOutcome.success(
                operation="consult",
                payload={
                    "ok": True,
                    "status": "completed",
                    "session_id": session_id,
                    "child_session_id": session_id,
                    "target_agent": target.name,
                    "target_agent_id": str(target.id),
                    "source_agent": source_name,
                    "source_agent_id": str(from_agent_id),
                    "reply": target_reply,
                    "child_invocation": child_invocation,
                    "message": f"{target.name} replied.",
                    "continuation_tool": "send_message_to_agent",
                },
            )

    except Exception as e:
        import traceback

        traceback.print_exc()
        return _a2a_failure(
            "consult",
            error_code="message_runtime_error",
            message=f"Message send error: {str(e)}",
            retryable=True,
        )


async def _send_message_to_agent(from_agent_id: uuid.UUID, args: dict) -> str:
    """Compatibility renderer for the LLM tool surface."""
    outcome = await _send_message_to_agent_outcome(from_agent_id, args)
    return outcome.to_tool_result()


async def _delegate_to_agent_async_outcome(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    principal: ExecutionPrincipal | None = None,
) -> A2AOutcome:
    """Spawn an async subagent task and return a runtime handle."""
    agent_name = args.get("agent_name", "").strip()
    message_text = args.get("message", "").strip()
    tool_profile = _normalize_delegate_tool_profile(args.get("tool_profile"))
    target_artifacts_arg = args.get("target_artifacts") if isinstance(args.get("target_artifacts"), list) else []
    target_agent_id_raw = args.get("target_agent_id")
    target_agent_id = None
    principal, principal_error = _principal_from_args(args, principal, operation="delegate")
    if principal_error is not None:
        return principal_error
    if target_agent_id_raw:
        try:
            target_agent_id = uuid.UUID(str(target_agent_id_raw))
        except (TypeError, ValueError, AttributeError):
            return _a2a_failure("delegate", error_code="invalid_arguments", message="target_agent_id is invalid")

    if (not agent_name and target_agent_id is None) or not message_text:
        return _a2a_failure(
            "delegate",
            error_code="invalid_arguments",
            message="Please provide target agent name and message content",
        )

    try:
        from app.agents.orchestrator import (
            ASYNC_DELEGATION_TIMEOUT_SECONDS,
            MAX_DELEGATION_TIMEOUT_SECONDS,
            OrchestrationPolicy,
            delegate_async,
        )

        source_agent, target, target_model, error = await _resolve_target_agent_runtime(
            from_agent_id,
            agent_name,
            target_agent_id=target_agent_id,
        )
        if error:
            return _a2a_failure(
                "delegate",
                error_code="target_runtime_unavailable",
                message=error,
                retryable=str(error).startswith("⚠️"),
            )
        assert source_agent is not None
        assert target is not None
        if principal is not None:
            try:
                principal.assert_scope(tenant_id=source_agent.tenant_id, source_agent_id=from_agent_id)
            except ValueError as exc:
                return _a2a_failure(
                    "delegate",
                    error_code="principal_scope_mismatch",
                    message=str(exc),
                )
        requester_user_id = _effective_a2a_requester(source_agent, principal)
        budget_run_id = None
        if args.get("_budget_run_id"):
            try:
                budget_run_id = uuid.UUID(str(args.get("_budget_run_id")))
            except (TypeError, ValueError, AttributeError):
                return _a2a_failure(
                    "delegate", error_code="invalid_runtime_budget", message="runtime budget id is invalid"
                )

        if str(args.get("execution_target") or "cloud_agent").strip() == "local_agent":
            local_args = dict(args)
            if principal is not None:
                local_args["_execution_principal"] = principal.to_evidence()
            local_delegate_kwargs = {
                "source_agent": source_agent,
                "target_agent": target,
                "message_text": message_text,
                "args": local_args,
            }
            if budget_run_id is not None:
                local_delegate_kwargs["budget_run_id"] = budget_run_id
            queued = await _delegate_to_local_agent_channel(**local_delegate_kwargs)
            return A2AOutcome.success(operation="delegate", payload=queued)

        assert target_model is not None
        timeout_seconds, timeout_error = _parse_timeout_seconds_arg(
            args.get("timeout_seconds"),
            default_seconds=ASYNC_DELEGATION_TIMEOUT_SECONDS,
            max_seconds=MAX_DELEGATION_TIMEOUT_SECONDS,
        )
        if timeout_error:
            return _a2a_failure("delegate", error_code="invalid_timeout", message=timeout_error)
        assert timeout_seconds is not None
        child_session_id = uuid.uuid4().hex
        try:
            delegation_depth = max(1, int(args.get("_a2a_depth") or 1))
            delegation_max_depth = max(1, int(args.get("_a2a_max_depth") or 2))
        except (TypeError, ValueError):
            return _a2a_failure(
                "delegate",
                error_code="invalid_delegation_context",
                message="Invalid A2A depth context",
            )
        if delegation_depth > delegation_max_depth:
            return _a2a_failure(
                "delegate",
                error_code="delegation_depth_limited",
                message=f"Delegation depth limit reached ({delegation_depth}/{delegation_max_depth})",
                status="blocked",
            )
        handle = await delegate_async(
            target=target,
            target_model=target_model,
            conversation_messages=[
                {
                    "role": "user",
                    "content": message_text,
                }
            ],
            owner_id=requester_user_id,
            session_id=child_session_id,
            parent_agent_id=from_agent_id,
            parent_agent_name=source_agent.name,
            parent_session_id=(principal.root_session_id if principal else None) or args.get("parent_session_id"),
            trace_id=str(args.get("_a2a_trace_id") or "").strip() or None,
            depth=delegation_depth,
            max_tool_rounds=args.get("max_tool_rounds"),
            policy=OrchestrationPolicy(
                max_depth=delegation_max_depth,
                timeout_seconds=timeout_seconds,
                tool_profile=tool_profile,
            ),
            tenant_id=getattr(source_agent, "tenant_id", None),
            confirmed_plan_id=args.get("confirmed_plan_id"),
            confirmed_plan_version=args.get("confirmed_plan_version"),
            confirmed_plan_hash=args.get("confirmed_plan_hash"),
            confirmed_plan_session_id=args.get("confirmed_plan_session_id"),
            plan_authorization=(dict(args.get("plan_authorization") or args.get("_plan_authorization") or {}) or None),
            ledger_todo_id=str(args.get("ledger_todo_id") or "").strip() or None,
            permission_profile=args.get("_permission_profile"),
            target_artifact_path=str(args.get("target_artifact_path") or "").strip() or None,
            target_artifacts=target_artifacts_arg,
            edit_mode=str(args.get("edit_mode") or "").strip() or None,
            budget_run_id=budget_run_id,
            execution_principal=principal.to_evidence() if principal else None,
            root_runtime_task_id=principal.root_runtime_task_id if principal else args.get("_runtime_task_id"),
        )
        if str(getattr(handle, "status", "running")).startswith("plan_required"):
            return _a2a_failure(
                "delegate",
                error_code="plan_required",
                message=str(getattr(handle, "status", "plan_required")),
                status="blocked",
            )
        if str(getattr(handle, "status", "running")) == "blocked_by_lease":
            return _a2a_failure(
                "delegate",
                error_code="coordination_lease_blocked",
                message="Delegation is blocked by an active coordination lease",
                status="blocked",
                retryable=True,
            )
        handle_status = str(getattr(handle, "status", "running"))
        if handle_status.startswith("authority_unavailable"):
            _, _, reason = handle_status.partition(":")
            return _a2a_failure(
                "delegate",
                error_code=reason or "a2a_authority_unavailable",
                message="The delegated invocation was not queued because its authority frame is unavailable.",
                status="unavailable",
            )
        return A2AOutcome.success(
            operation="delegate",
            payload={
                "task_id": handle.task_id,
                "runtime_task_id": handle.task_id,
                "session_id": child_session_id,
                "child_session_id": child_session_id,
                "status": getattr(handle, "status", "running"),
                "target_agent": handle.target_name,
                "target_agent_id": str(target.id),
                "target_artifact_path": str(args.get("target_artifact_path") or "").strip() or None,
                "target_artifacts": target_artifacts_arg,
                "edit_mode": str(args.get("edit_mode") or "").strip() or None,
                "trace_id": handle.trace_id,
                "receipt": getattr(handle, "receipt", None),
                "continuation_tool": "send_agent_session_message",
                "next_action": (
                    "Use send_agent_session_message with child_session_id to continue this delegated "
                    "agent session; use check_async_task only for runtime status."
                ),
            },
        )
    except Exception as e:
        logger.error("delegate_to_agent failed: %s", e, exc_info=True)
        return _a2a_failure(
            "delegate",
            error_code="delegation_runtime_error",
            message=f"Error delegating to agent: {e}",
            retryable=True,
        )


async def _delegate_to_agent_async(from_agent_id: uuid.UUID, args: dict) -> str:
    """Compatibility renderer for the LLM tool surface."""
    outcome = await _delegate_to_agent_async_outcome(from_agent_id, args)
    return outcome.to_tool_result()


async def _delegate_to_local_agent_channel(
    *,
    source_agent,
    target_agent,
    message_text: str,
    args: dict,
    budget_run_id: uuid.UUID | None = None,
) -> dict:
    """Queue delegated work onto the target agent's Local Agent Channel."""

    from app.services import local_agent_channel_service

    tenant_id = getattr(source_agent, "tenant_id", None) or getattr(target_agent, "tenant_id", None)
    if tenant_id is None:
        raise ValueError("source/target agent has no tenant for local-agent delegation")
    if getattr(source_agent, "tenant_id", tenant_id) != getattr(target_agent, "tenant_id", tenant_id):
        raise ValueError("cross-tenant local-agent delegation is forbidden")
    source_owner_id = getattr(source_agent, "owner_user_id", None) or getattr(source_agent, "creator_id", None)
    target_owner_id = getattr(target_agent, "owner_user_id", None) or getattr(target_agent, "creator_id", None)
    if source_owner_id is None or target_owner_id is None:
        raise ValueError("source/target agent owner is required for local-agent delegation")
    principal = ExecutionPrincipal.from_evidence(args.get("_execution_principal"))
    if principal is not None:
        principal.assert_scope(tenant_id=tenant_id, source_agent_id=source_agent.id)
    source_requester_id = principal.requester_user_id if principal and principal.requester_user_id else source_owner_id
    target_artifacts_arg = args.get("target_artifacts") if isinstance(args.get("target_artifacts"), list) else []
    idempotency_anchor = str(
        args.get("_runtime_task_id")
        or args.get("_turn_id")
        or args.get("_budget_run_id")
        or args.get("parent_session_id")
        or f"{source_agent.id}:{target_agent.id}"
    )[:80]
    request_digest = hashlib.sha256(
        json.dumps(
            {
                "source_agent_id": str(source_agent.id),
                "target_agent_id": str(target_agent.id),
                "message": message_text,
                "attachments": list(args.get("attachments") or []),
                "expected_output": args.get("expected_output"),
                "target_artifacts": target_artifacts_arg,
                "edit_mode": args.get("edit_mode"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"a2a-local:{idempotency_anchor}:{request_digest[:40]}"[:200]
    budget_reservation_key = None
    budget_service = None
    budget_admission = None
    budget_decision = None
    if budget_run_id is not None:
        from app.services.execution_admission import ExecutionAdmission
        from app.services.runtime_budget_service import (
            RuntimeBudgetReservation,
            RuntimeBudgetService,
            estimate_reservation_tokens,
        )

        budget_service = RuntimeBudgetService()
        budget_admission = ExecutionAdmission(budget_service)
        budget_reservation_key = f"delegation:local:{request_digest[:40]}:start"
        estimated_tokens = estimate_reservation_tokens(
            default_tokens=50_000,
            prompt_tokens=max(1, (len(message_text) + 3) // 4) if message_text else 0,
        )
        budget_decision = await budget_admission.admit(
            RuntimeBudgetReservation(
                budget_run_id=budget_run_id,
                reservation_key=budget_reservation_key,
                tokens=estimated_tokens,
                cache_miss_tokens=estimated_tokens,
                delegations=1,
                background_tasks=1,
                reason="local_agent_delegation_start",
                metadata={
                    "work_type": "local_agent_delegation",
                    "source_agent_id": str(source_agent.id),
                    "target_agent_id": str(target_agent.id),
                },
            )
        )
        if budget_decision.waiting:
            return {
                "status": "waiting_budget_approval",
                "error_code": "runtime_budget_approval_required",
                "message": budget_decision.user_message,
                "denied_dimensions": list(budget_decision.denied_dimensions),
                "execution_target": "local_agent",
                "target_agent": getattr(target_agent, "name", str(target_agent.id)),
            }

    try:
        async with tenant_scoped_session(tenant_id) as db:
            session = await local_agent_channel_service.create_channel_session(
                db,
                tenant_id=tenant_id,
                owner_user_id=target_owner_id,
                actor_user_id=source_requester_id,
                source_agent_id=target_agent.id,
                source="a2a",
                title=f"A2A from {getattr(source_agent, 'name', 'agent')}",
                commit=False,
                reuse_existing=True,
            )
            message = await local_agent_channel_service.enqueue_channel_message(
                db,
                session_id=session["id"],
                owner_user_id=target_owner_id,
                sender_user_id=source_requester_id,
                sender_agent_id=source_agent.id,
                content=message_text,
                attachments=list(args.get("attachments") or []),
                metadata={
                    "source": "a2a",
                    "execution_target": "local_agent",
                    "sender_agent_id": str(source_agent.id),
                    "sender_agent_name": getattr(source_agent, "name", None),
                    "target_agent_id": str(target_agent.id),
                    "target_agent_name": getattr(target_agent, "name", None),
                    "target_owner_user_id": str(target_owner_id),
                    "expected_output": str(args.get("expected_output") or "").strip() or None,
                    "parent_session_id": (principal.root_session_id if principal else None)
                    or args.get("parent_session_id"),
                    "ledger_todo_id": str(args.get("ledger_todo_id") or "").strip() or None,
                    "target_artifact_path": str(args.get("target_artifact_path") or "").strip() or None,
                    "target_artifacts": target_artifacts_arg,
                    "edit_mode": str(args.get("edit_mode") or "").strip() or None,
                    "budget_run_id": str(budget_run_id) if budget_run_id else None,
                    "budget_reservation_key": budget_reservation_key,
                    "root_runtime_task_id": principal.root_runtime_task_id if principal else None,
                    "execution_principal": principal.to_evidence() if principal else None,
                },
                idempotency_key=idempotency_key,
            )
            message_status = str(message.get("status") or "pending")
            if message_status in {"pending", "delivered"}:
                try:
                    from app.api.local_agent_channel import channel_ws_manager

                    await channel_ws_manager.send_to_user(target_owner_id, {"type": "message", "message": message})
                except Exception as exc:
                    logger.debug("Suppressed local-agent channel WS fanout failure: %s", exc)
            result = {
                "status": "waiting_approval" if message_status == "waiting_approval" else "queued",
                "execution_target": "local_agent",
                "target_agent": getattr(target_agent, "name", str(target_agent.id)),
                "channel_session_id": str(session["id"]),
                "chat_session_id": str(session["chat_session_id"]) if session.get("chat_session_id") else None,
                "message_id": str(message["id"]),
                "receipt": message.get("receipt"),
                "approval_id": message.get("approval_id"),
                "next_action": (
                    "The owner must approve this exact action before Hive Connect can receive it."
                    if message_status == "waiting_approval"
                    else (
                        "The result will be delivered automatically back into this source Agent session. "
                        "Use check_async_task with message_id only as fallback status inspection. "
                        "If the computer is offline, the request remains queued until Hive Connect reconnects."
                    )
                ),
            }
            if budget_admission is not None and budget_decision is not None:
                await budget_admission.settle(
                    budget_decision,
                    actual_delegations=1,
                    actual_background_tasks=1,
                    reason="local_agent_delegation_queued",
                )
            return result
    except Exception:
        if budget_admission is not None and budget_decision is not None:
            try:
                await budget_admission.settle(
                    budget_decision,
                    reason="local_agent_delegation_enqueue_failed",
                )
            except Exception as settle_exc:
                logger.warning("Failed to release local-agent delegation budget reservation: %s", settle_exc)
        raise


async def _check_async_task(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    principal: ExecutionPrincipal | None = None,
) -> str:
    """Check a cloud RuntimeTask or a Local Agent delegation message."""
    task_id = (args.get("task_id") or "").strip()
    message_id = (args.get("message_id") or "").strip()
    if bool(task_id) == bool(message_id):
        return "❌ Please provide exactly one of task_id or message_id"

    if message_id:
        if principal is None:
            return "❌ Local Agent delegation root authority is required"
        try:
            local_message_id = uuid.UUID(message_id)
        except (TypeError, ValueError, AttributeError):
            return "❌ Local Agent delegation message_id is invalid"
        try:
            principal.assert_scope(tenant_id=principal.tenant_id, source_agent_id=from_agent_id)
            from app.services import local_agent_channel_service

            async with tenant_scoped_session(principal.tenant_id) as db:
                payload = await local_agent_channel_service.get_a2a_channel_message_status(
                    db,
                    message_id=local_message_id,
                    principal=principal,
                    source_agent_id=from_agent_id,
                )
            if payload is None:
                return "❌ Local Agent delegation not found or access denied"
            return json.dumps(payload, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("check_async_task local message failed: %s", exc, exc_info=True)
            return f"❌ Local Agent delegation authority evidence is unavailable: {exc}"

    try:
        from app.agents.orchestrator import check_async_delegation
        from app.services.runtime_task_authority import authorize_runtime_task_record
        from app.services.runtime_task_service import get_runtime_task_record

        try:
            record = await get_runtime_task_record(task_id)
        except Exception as exc:
            return f"❌ RuntimeTask authority evidence is unavailable: {exc}"
        if record is None:
            return "❌ RuntimeTask not found"
        if principal is None:
            return "❌ RuntimeTask root authority is required"
        decision = authorize_runtime_task_record(record, principal=principal, action="read")
        if not decision.allowed:
            return f"❌ RuntimeTask access denied: {decision.reason}"

        status = await check_async_delegation(task_id, parent_agent_id=from_agent_id)
        if status.get("status") == "forbidden":
            return "❌ This task does not belong to the current agent"
        return json.dumps(status, ensure_ascii=False)
    except Exception as e:
        logger.error("check_async_task failed: %s", e, exc_info=True)
        return f"❌ Error checking async task: {e}"


async def _cancel_async_task(
    from_agent_id: uuid.UUID,
    args: dict,
    *,
    principal: ExecutionPrincipal | None = None,
) -> str:
    """Cancel a previously spawned async task if it belongs to the current agent."""
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "❌ Please provide task_id"

    try:
        from app.agents.orchestrator import ASYNC_DELEGATION_CANCEL_GRACE_SECONDS, cancel_async_delegation
        from app.services.runtime_task_authority import authorize_runtime_task_record
        from app.services.runtime_task_service import get_runtime_task_record

        try:
            record = await get_runtime_task_record(task_id)
        except Exception as exc:
            return f"❌ RuntimeTask authority evidence is unavailable: {exc}"
        if record is None:
            return "❌ RuntimeTask not found"
        if principal is None:
            return "❌ RuntimeTask root authority is required"
        decision = authorize_runtime_task_record(record, principal=principal, action="cancel")
        if not decision.allowed:
            return f"❌ RuntimeTask access denied: {decision.reason}"

        min_runtime_seconds, timeout_error = _parse_timeout_seconds_arg(
            args.get("min_runtime_seconds"),
            default_seconds=ASYNC_DELEGATION_CANCEL_GRACE_SECONDS,
            max_seconds=3600.0,
            field_name="min_runtime_seconds",
        )
        if timeout_error:
            return timeout_error
        assert min_runtime_seconds is not None
        status = await cancel_async_delegation(
            task_id,
            parent_agent_id=from_agent_id,
            force=_parse_bool_arg(args.get("force")),
            min_runtime_seconds=min_runtime_seconds,
        )
        if status.get("status") == "forbidden":
            return "❌ This task does not belong to the current agent"
        return json.dumps(status, ensure_ascii=False)
    except Exception as e:
        logger.error("cancel_async_task failed: %s", e, exc_info=True)
        return f"❌ Error cancelling async task: {e}"


async def _list_async_tasks(
    from_agent_id: uuid.UUID,
    *,
    principal: ExecutionPrincipal | None = None,
) -> str:
    """List recent async runtime tasks created by the current agent."""
    try:
        from app.services.runtime_task_authority import authorize_runtime_task_record
        from app.services.runtime_task_service import list_runtime_task_records

        if principal is None or principal.requester_user_id is None or principal.root_session_id is None:
            return "❌ RuntimeTask root authority is required"
        try:
            tasks = await list_runtime_task_records(
                parent_agent_id=from_agent_id,
                root_user_id=principal.requester_user_id,
                root_session_id=principal.root_session_id,
                limit=20,
            )
        except Exception as exc:
            return f"❌ RuntimeTask authority evidence is unavailable: {exc}"
        authorized = [
            task for task in tasks if authorize_runtime_task_record(task, principal=principal, action="list").allowed
        ]
        return json.dumps(authorized, ensure_ascii=False)
    except Exception as e:
        logger.error("list_async_tasks failed: %s", e, exc_info=True)
        return f"❌ Error listing async tasks: {e}"


async def _get_current_time(agent_id: uuid.UUID, args: dict | None = None) -> str:
    """Return the current time in the agent's effective timezone."""
    try:
        from app.services.timezone_utils import get_agent_timezone, now_in_timezone

        requested_tz = (args or {}).get("timezone")
        timezone_name = requested_tz or await get_agent_timezone(agent_id)
        now = now_in_timezone(timezone_name)
        return json.dumps(
            {
                "timezone": timezone_name,
                "local_time": now.isoformat(),
                "utc_time": now.astimezone(timezone.utc).isoformat(),
                "weekday": now.strftime("%A"),
            },
            ensure_ascii=False,
        )
    except Exception as e:
        logger.error("get_current_time failed: %s", e, exc_info=True)
        return f"❌ Error getting current time: {e}"


async def _feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Proxy to feishu_users domain module (lazy import to avoid circular deps)."""
    from app.services.agent_tool_domains.feishu_users import _feishu_user_search as _real_search

    return await _real_search(agent_id, arguments)
