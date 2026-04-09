"""Messaging domain — Feishu messaging, web messaging, agent-to-agent communication."""

import json
import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select

from app.database import async_session
from app.tools.result_envelope import render_tool_error

logger = logging.getLogger(__name__)

A2A_SYSTEM_PROMPT_SUFFIX = (
    "--- Agent-to-Agent Message ---\n"
    "You are receiving a message from another digital employee.\n"
    "Rules:\n"
    "- Reply concisely and helpfully. Focus on the request and provide a clear answer.\n"
    "- Treat the delegated brief and attached request as authoritative context. If it is incomplete, say what is missing instead of guessing.\n"
    "- Do NOT delegate to another agent — handle the request directly (no nested delegation).\n"
    "- If you are still working, return a short status update with the current step and the specific blocker, if any.\n"
    "- If you completed the request, return a final answer with concrete outputs such as file paths, artifacts, tool results, or evidence.\n"
    "- If you cannot complete the request, explain specifically what is missing or blocked.\n"
    "- Do NOT share private workspace data (memory/*.md, tasks.json) unless explicitly asked."
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


async def _resolve_target_agent_runtime(from_agent_id: uuid.UUID, agent_name: str):
    """Resolve source agent, target agent, and target model for A2A delegation."""
    from app.models.agent import Agent
    from app.models.llm import LLMModel

    async with async_session() as db:
        src_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
        source_agent = src_result.scalar_one_or_none()
        if not source_agent:
            return None, None, None, "❌ Source agent not found"

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
            return source_agent, None, None, (
                f"❌ No agent found matching '{agent_name}'. "
                f"Available: {', '.join(names) if names else 'none'}"
            )

        if target.status in ("expired", "stopped", "archived"):
            return source_agent, target, None, (
                f"⚠️ {target.name} is currently {target.status} and cannot receive messages."
            )

        if getattr(target, "agent_type", "native") == "openclaw":
            return source_agent, target, None, (
                f"⚠️ {target.name} is an OpenClaw agent and does not support async runtime delegation."
            )

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
        from app.services.feishu_service import feishu_service
        from sqlalchemy.orm import selectinload

        async with async_session() as db:
            # ── Resolve agent tenant_id for recipient validation ──
            _agent_r = await db.execute(select(Agent.tenant_id).where(Agent.id == agent_id))
            _agent_tenant_id = _agent_r.scalar_one_or_none()

            # ── Shortcut: if caller provided user_id or open_id directly ──
            if (direct_user_id or direct_open_id) and not member_name:
                # Validate recipient belongs to same tenant (prevent cross-tenant sends)
                if not _agent_tenant_id:
                    return "❌ Agent has no tenant configured, cannot validate recipient. Please contact admin."
                _recipient_ok = False
                if direct_user_id:
                    _check = await db.execute(
                        select(OrgMember.id).where(
                            OrgMember.feishu_user_id == direct_user_id,
                            OrgMember.tenant_id == _agent_tenant_id,
                        )
                    )
                    _recipient_ok = _check.scalar_one_or_none() is not None
                if not _recipient_ok and direct_open_id:
                    _check = await db.execute(
                        select(OrgMember.id).where(
                            OrgMember.feishu_open_id == direct_open_id,
                            OrgMember.tenant_id == _agent_tenant_id,
                        )
                    )
                    _recipient_ok = _check.scalar_one_or_none() is not None
                if not _recipient_ok:
                    return (
                        f"❌ 无法验证收件人身份：user_id={direct_user_id or ''}, open_id={direct_open_id or ''}。"
                        f"该用户不在本组织通讯录中，已阻止发送。"
                    )

                config_result = await db.execute(
                    select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu")
                )
                config = config_result.scalar_one_or_none()
                if not config:
                    return "❌ This agent has no Feishu channel configured"
                import json as _j
                # Prefer user_id over open_id
                if direct_user_id:
                    resp = await feishu_service.send_message(
                        config.app_id, config.app_secret,
                        receive_id=direct_user_id, msg_type="text",
                        content=_j.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type="user_id",
                    )
                    if resp.get("code") == 0:
                        return f"✅ 消息已发送（user_id: {direct_user_id}）"
                    # Fallback to open_id if user_id fails
                    if direct_open_id:
                        resp = await feishu_service.send_message(
                            config.app_id, config.app_secret,
                            receive_id=direct_open_id, msg_type="text",
                            content=_j.dumps({"text": message_text}, ensure_ascii=False),
                            receive_id_type="open_id",
                        )
                        if resp.get("code") == 0:
                            return f"✅ 消息已发送（open_id: {direct_open_id}）"
                    return f"❌ 发送失败：{resp.get('msg')} (code {resp.get('code')})"
                else:
                    resp = await feishu_service.send_message(
                        config.app_id, config.app_secret,
                        receive_id=direct_open_id, msg_type="text",
                        content=_j.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type="open_id",
                    )
                    if resp.get("code") == 0:
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
                _owner_r = await db.execute(
                    select(Agent).where(Agent.id == agent_id)
                )
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
                            target_member = type("_OwnerAsMember", (), {
                                "name": _owner_user.display_name,
                                "feishu_user_id": _owner_feishu_uid,
                                "feishu_open_id": _owner_feishu_oid,
                                "email": _owner_user.email,
                                "phone": None,
                            })()

            if not target_member:
                # ── Fallback: look up via feishu_user_search (contacts cache / OrgMember / User) ──
                _search_result = await _feishu_user_search(agent_id, {"name": member_name})
                # Prefer user_id over open_id
                import re as _re_oid
                _uid_match = _re_oid.search(r'user_id: `([A-Za-z0-9]+)`', _search_result)
                _oid_match = _re_oid.search(r'open_id: `(ou_[A-Za-z0-9]+)`', _search_result)
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
                        select(ChannelConfig).where(ChannelConfig.agent_id == agent_id, ChannelConfig.channel_type == "feishu")
                    )
                    config = config_result.scalar_one_or_none()
                    if not config:
                        return "❌ This agent has no Feishu channel configured"
                    import json as _j2
                    resp = await feishu_service.send_message(
                        config.app_id, config.app_secret,
                        receive_id=_found_id, msg_type="text",
                        content=_j2.dumps({"text": message_text}, ensure_ascii=False),
                        receive_id_type=_found_id_type,
                    )
                    if resp.get("code") == 0:
                        return f"✅ 消息已成功发送给 {member_name}"
                    return f"❌ 找到了 {member_name}（{_found_id_type}: {_found_id}）但发送失败：{resp.get('msg')} (code {resp.get('code')})"
                # Could not find via any path
                names = [r.member.name for r in rels if r.member]
                return (
                    f"❌ 未找到联系人「{member_name}」。\n"
                    f"关系列表中的联系人：{', '.join(names) if names else '（空）'}\n"
                    f"通讯录搜索结果：{_search_result[:200]}"
                )

            if not target_member.feishu_user_id and not target_member.feishu_open_id and not target_member.email and not target_member.phone:
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

            async def _try_send(app_id: str, app_secret: str, receive_id: str, id_type: str = "open_id") -> dict:
                return await feishu_service.send_message(
                    app_id, app_secret,
                    receive_id=receive_id, msg_type="text",
                    content=content, receive_id_type=id_type,
                )

            async def _save_outgoing_to_feishu_session(open_id: str):
                """Save the outgoing message to the Feishu P2P chat session."""
                try:
                    from app.models.audit import ChatMessage
                    from app.models.agent import Agent as AgentModel
                    from app.services.channel_session import find_or_create_channel_session
                    from datetime import datetime as _dt, timezone as _tz

                    agent_r = await db.execute(select(AgentModel).where(AgentModel.id == agent_id))
                    agent_obj = agent_r.scalar_one_or_none()
                    creator_id = agent_obj.creator_id if agent_obj else agent_id

                    # Look up the platform user: prefer feishu_user_id, then feishu_open_id
                    from app.models.user import User as UserModel
                    feishu_user = None
                    if open_id:
                        u_r = await db.execute(
                            select(UserModel).where(UserModel.feishu_open_id == open_id)
                        )
                        feishu_user = u_r.scalar_one_or_none()
                    user_id = feishu_user.id if feishu_user else creator_id

                    ext_conv_id = f"feishu_p2p_{open_id}"
                    sess = await find_or_create_channel_session(
                        db=db,
                        agent_id=agent_id,
                        user_id=user_id,
                        external_conv_id=ext_conv_id,
                        source_channel="feishu",
                        first_message_title=f"[Agent → {member_name}]",
                    )
                    db.add(ChatMessage(
                        agent_id=agent_id,
                        user_id=user_id,
                        role="assistant",
                        content=message_text,
                        conversation_id=str(sess.id),
                    ))
                    sess.last_message_at = _dt.now(_tz.utc)
                    await db.commit()
                    logger.info(f"[Feishu] Saved outgoing message to session {sess.id} ({member_name})")
                except Exception as e:
                    logger.error(f"[Feishu] Failed to save outgoing message to history: {e}")

            # Step 1: Try using feishu_user_id (tenant-stable, works across apps)
            if target_member.feishu_user_id:
                resp = await _try_send(config.app_id, config.app_secret, target_member.feishu_user_id, "user_id")
                if resp.get("code") == 0:
                    await _save_outgoing_to_feishu_session(target_member.feishu_open_id or target_member.feishu_user_id)
                    return f"✅ Successfully sent message to {member_name}"

            # Step 2: Try resolve open_id via email/phone
            if target_member.email or target_member.phone:
                try:
                    resolved = await feishu_service.resolve_open_id(
                        config.app_id, config.app_secret,
                        email=target_member.email,
                        mobile=target_member.phone,
                    )
                    if resolved:
                        resp = await _try_send(config.app_id, config.app_secret, resolved)
                        if resp.get("code") == 0:
                            target_member.feishu_open_id = resolved
                            await db.commit()
                            await _save_outgoing_to_feishu_session(resolved)
                            return f"✅ Successfully sent message to {member_name}"
                except Exception as e:
                    logger.debug("Suppressed: %s", e)
            if target_member.feishu_open_id:
                resp = await _try_send(config.app_id, config.app_secret, target_member.feishu_open_id)
                if resp.get("code") == 0:
                    await _save_outgoing_to_feishu_session(target_member.feishu_open_id)
                    return f"✅ Successfully sent message to {member_name}"

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
                        if target_member.feishu_user_id:
                            resp2 = await _try_send(
                                org_setting.value["app_id"], org_setting.value["app_secret"],
                                target_member.feishu_user_id, "user_id",
                            )
                            if resp2.get("code") == 0:
                                await _save_outgoing_to_feishu_session(target_member.feishu_open_id)
                                return f"✅ Successfully sent message to {member_name}"
                        # Fallback to open_id with org sync app
                        resp2 = await _try_send(
                            org_setting.value["app_id"], org_setting.value["app_secret"],
                            target_member.feishu_open_id,
                        )
                        if resp2.get("code") == 0:
                            await _save_outgoing_to_feishu_session(target_member.feishu_open_id)
                            return f"✅ Successfully sent message to {member_name}"
                        return f"❌ Send failed: {resp2.get('msg', str(resp2))}"

                return f"❌ Send failed: {err_msg}"

            return f"❌ {member_name} has no Feishu user_id or open_id and cannot be resolved via email/phone"
    except Exception as e:
        return f"❌ Message send error: {str(e)[:200]}"


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
        from datetime import datetime as _dt, timezone as _tz

        async with async_session() as db:
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
                select(ChatSession).where(
                    ChatSession.agent_id == agent_id,
                    ChatSession.user_id == target_user.id,
                    ChatSession.source_channel == "web",
                ).order_by(ChatSession.created_at.desc()).limit(1)
            )
            session = sess_r.scalar_one_or_none()

            if not session:
                session = ChatSession(
                    agent_id=agent_id,
                    user_id=target_user.id,
                    title=f"[Agent Message] {_dt.now(_tz.utc).strftime('%m-%d %H:%M')}",
                    source_channel="web",
                    created_at=_dt.now(_tz.utc),
                )
                db.add(session)
                await db.flush()

            # Save the message
            db.add(ChatMessage(
                agent_id=agent_id,
                user_id=target_user.id,
                role="assistant",
                content=message_text,
                conversation_id=str(session.id),
            ))
            session.last_message_at = _dt.now(_tz.utc)
            await db.commit()

            # Push via WebSocket if user has an active connection
            try:
                from app.api.websocket import manager as ws_manager
                agent_id_str = str(agent_id)
                if agent_id_str in ws_manager.active_connections:
                    for ws, sid in list(ws_manager.active_connections[agent_id_str]):
                        try:
                            await ws.send_json({
                                "type": "trigger_notification",
                                "content": message_text,
                                "triggers": ["web_message"],
                            })
                        except Exception as e:
                            logger.debug("Suppressed: %s", e)
            except Exception as e:
                logger.debug("Suppressed: %s", e)

            display = target_user.display_name or target_user.username
            return f"✅ Message sent to {display} on web platform. It has been saved to their chat history."

    except Exception as e:
        return f"❌ Web message send error: {str(e)[:200]}"


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
    from app.models.audit import ChatMessage

    try:
        async with async_session() as db:
            db.add(ChatMessage(
                agent_id=session_agent_id,
                user_id=owner_id,
                role="tool_call",
                content=json.dumps({
                    "name": tool_name,
                    "args": tool_args,
                    "status": "done",
                    "result": str(tool_result)[:500],
                }, ensure_ascii=False),
                conversation_id=session_id,
                participant_id=participant_id,
            ))
            await db.commit()
    except Exception as exc:
        logger.error(f"[A2A] Failed to save tool_call: {exc}")


def _build_agent_message_tool_executor(
    target_agent_id: uuid.UUID,
    owner_id: uuid.UUID,
    session_agent_id: uuid.UUID,
    session_id: str,
    participant_id: uuid.UUID | None,
):
    """Wrap A2A tool execution with chat-history persistence."""

    async def _executor(tool_name: str, tool_args: dict) -> str:
        from app.services.agent_tools import execute_tool
        tool_result = await execute_tool(tool_name, tool_args, target_agent_id, owner_id)
        await _persist_agent_tool_call(
            session_agent_id=session_agent_id,
            owner_id=owner_id,
            session_id=session_id,
            participant_id=participant_id,
            tool_name=tool_name,
            tool_args=tool_args,
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
) -> str:
    """Run the target agent reply through the shared runtime kernel."""
    from app.agents.orchestrator import delegate_to_agent

    return await delegate_to_agent(
        target=target,
        target_model=target_model,
        conversation_messages=conversation_messages,
        owner_id=owner_id,
        session_id=session_id,
        parent_agent_id=from_agent_id,
        parent_session_id=session_id,
        trace_id=f"a2a:{session_id}:{from_agent_id}:{target.id}",
        tool_executor=_build_agent_message_tool_executor(
            target_agent_id=target.id,
            owner_id=owner_id,
            session_agent_id=session_agent_id,
            session_id=session_id,
            participant_id=participant_id,
        ),
        system_prompt_suffix=A2A_SYSTEM_PROMPT_SUFFIX,
        max_tool_rounds=getattr(target, "max_tool_rounds", None) or 200,
    )


async def _send_message_to_agent(from_agent_id: uuid.UUID, args: dict) -> str:
    """Send a message to another digital employee. Uses a single request-response pattern:
    the source agent sends a message, the target agent replies once, and the result is returned.
    If the source agent needs to continue the conversation, it can call this tool again.
    """
    agent_name = args.get("agent_name", "").strip()
    message_text = args.get("message", "").strip()

    if not agent_name or not message_text:
        return "❌ Please provide target agent name and message content"

    try:
        from app.models.agent import Agent
        from app.models.audit import ChatMessage
        from app.models.chat_session import ChatSession
        from app.models.participant import Participant

        async with async_session() as db:
            # Look up source agent
            src_result = await db.execute(select(Agent).where(Agent.id == from_agent_id))
            source_agent = src_result.scalar_one_or_none()
            source_name = source_agent.name if source_agent else "Unknown agent"

            # Find target agent by name (scoped to same tenant)
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
                return f"❌ No agent found matching '{agent_name}'. Available: {', '.join(names) if names else 'none'}"

            if target.status in ("expired", "stopped", "archived"):
                return f"⚠️ {target.name} is currently {target.status} and cannot receive messages."

            # ── OpenClaw target: queue message for gateway poll ──
            if getattr(target, "agent_type", "native") == "openclaw":
                from app.models.gateway_message import GatewayMessage as GMsg
                gw_msg = GMsg(
                    agent_id=target.id,
                    sender_agent_id=from_agent_id,
                    sender_user_id=source_agent.creator_id if source_agent else None,
                    content=f"[From {source_name}] {message_text}",
                    status="pending",
                )
                db.add(gw_msg)
                await db.commit()
                online = target.openclaw_last_seen and (datetime.now(timezone.utc) - target.openclaw_last_seen).total_seconds() < 300
                status_hint = "online" if online else "offline (message will be delivered on next heartbeat)"
                return f"✅ Message sent to {target.name} (OpenClaw agent, currently {status_hint}). The message has been queued and will be delivered when the agent polls for updates."
            src_part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == from_agent_id))
            src_participant = src_part_r.scalar_one_or_none()
            tgt_part_r = await db.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id))
            tgt_participant = tgt_part_r.scalar_one_or_none()

            # Find or create ChatSession for this agent pair (ordered consistently)
            session_agent_id = min(from_agent_id, target.id, key=str)
            session_peer_id = max(from_agent_id, target.id, key=str)
            sess_r = await db.execute(
                select(ChatSession).where(
                    ChatSession.agent_id == session_agent_id,
                    ChatSession.peer_agent_id == session_peer_id,
                    ChatSession.source_channel == "agent",
                )
            )
            chat_session = sess_r.scalar_one_or_none()
            if not chat_session:
                owner_id = source_agent.creator_id if source_agent else from_agent_id
                src_part_id = src_participant.id if src_participant else None
                chat_session = ChatSession(
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    title=f"{source_name} ↔ {target.name}",
                    source_channel="agent",
                    participant_id=src_part_id,
                    peer_agent_id=session_peer_id,
                )
                db.add(chat_session)
                await db.flush()

            session_id = str(chat_session.id)

            # Prepare target LLM
            from app.models.llm import LLMModel

            # Load primary model (with fallback support)
            target_model = None
            if target.primary_model_id:
                model_r = await db.execute(
                    select(LLMModel).where(LLMModel.id == target.primary_model_id, LLMModel.tenant_id == target.tenant_id)
                )
                target_model = model_r.scalar_one_or_none()

            # Config-level fallback: primary missing -> use fallback
            if not target_model and target.fallback_model_id:
                fb_r = await db.execute(
                    select(LLMModel).where(LLMModel.id == target.fallback_model_id, LLMModel.tenant_id == target.tenant_id)
                )
                target_model = fb_r.scalar_one_or_none()
                if target_model:
                    logger.warning(f"[A2A] Primary model unavailable for {target.name}, using fallback: {target_model.model}")

            if not target_model:
                return f"⚠️ {target.name} has no LLM model configured"

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
                if m.participant_id and src_participant and m.participant_id == src_participant.id:
                    role = "user"
                else:
                    role = "assistant"
                conversation_messages.append({"role": role, "content": m.content})

            # Add the new message from source
            conversation_messages.append({"role": "user", "content": f"[From {source_name}] {message_text}"})

            # Save source message
            owner_id = source_agent.creator_id if source_agent else from_agent_id
            db.add(ChatMessage(
                agent_id=session_agent_id,
                user_id=owner_id,
                role="user",
                content=message_text,
                conversation_id=session_id,
                participant_id=src_participant.id if src_participant else None,
            ))
            chat_session.last_message_at = datetime.now(timezone.utc)
            await db.commit()

            target_reply = await _invoke_agent_message_runtime(
                target=target,
                target_model=target_model,
                conversation_messages=conversation_messages,
                from_agent_id=from_agent_id,
                owner_id=owner_id,
                session_id=session_id,
                session_agent_id=session_agent_id,
                participant_id=tgt_participant.id if tgt_participant else None,
            )

            if not target_reply:
                return f"⚠️ {target.name} did not respond (LLM returned empty)"

            # Save target reply
            async with async_session() as db2:
                part_r = await db2.execute(select(Participant).where(Participant.type == "agent", Participant.ref_id == target.id))
                tgt_part = part_r.scalar_one_or_none()
                db2.add(ChatMessage(
                    agent_id=session_agent_id,
                    user_id=owner_id,
                    role="assistant",
                    content=target_reply,
                    conversation_id=session_id,
                    participant_id=tgt_part.id if tgt_part else None,
                ))
                await db2.commit()

            # Log activity
            from app.services.activity_logger import log_activity
            await log_activity(
                target.id, "agent_msg_sent",
                f"Replied to message from {source_name}",
                detail={"partner": source_name, "message": message_text[:200], "reply": target_reply[:200]},
            )
            await log_activity(
                from_agent_id, "agent_msg_sent",
                f"Sent message to {target.name} and received reply",
                detail={"partner": target.name, "message": message_text[:200], "reply": target_reply[:200]},
            )

            return f"💬 {target.name} replied:\n{target_reply}"

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"❌ Message send error: {str(e)[:200]}"


async def _delegate_to_agent_async(from_agent_id: uuid.UUID, args: dict) -> str:
    """Spawn an async subagent task and return a runtime handle."""
    agent_name = args.get("agent_name", "").strip()
    message_text = args.get("message", "").strip()
    tool_profile = str(args.get("tool_profile") or "worker_safe").strip() or "worker_safe"

    if not agent_name or not message_text:
        return "❌ Please provide target agent name and message content"

    try:
        from app.agents.orchestrator import OrchestrationPolicy, delegate_async

        source_agent, target, target_model, error = await _resolve_target_agent_runtime(from_agent_id, agent_name)
        if error:
            return error
        assert source_agent is not None
        assert target is not None
        assert target_model is not None

        handle = await delegate_async(
            target=target,
            target_model=target_model,
            conversation_messages=[{
                "role": "user",
                "content": f"[Delegated by {source_agent.name}] {message_text}",
            }],
            owner_id=source_agent.creator_id,
            session_id=uuid.uuid4().hex,
            parent_agent_id=from_agent_id,
            parent_session_id=args.get("parent_session_id"),
            max_tool_rounds=args.get("max_tool_rounds"),
            policy=OrchestrationPolicy(timeout_seconds=120.0, tool_profile=tool_profile),
        )
        return json.dumps({
            "task_id": handle.task_id,
            "status": "running",
            "target_agent": handle.target_name,
            "trace_id": handle.trace_id,
            "next_action": "Use check_async_task with this task_id to inspect progress.",
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("delegate_to_agent failed: %s", e, exc_info=True)
        return f"❌ Error delegating to agent: {e}"


async def _check_async_task(from_agent_id: uuid.UUID, args: dict) -> str:
    """Check a previously spawned async task."""
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "❌ Please provide task_id"

    try:
        from app.agents.orchestrator import check_async_delegation
        from app.services.runtime_task_service import get_runtime_task_record

        try:
            record = await get_runtime_task_record(task_id)
        except Exception:
            record = None
        if record and record.get("parent_agent_id") not in {None, str(from_agent_id)}:
            return "❌ This task does not belong to the current agent"

        status = await check_async_delegation(task_id, parent_agent_id=from_agent_id)
        if status.get("status") == "forbidden":
            return "❌ This task does not belong to the current agent"
        return json.dumps(status, ensure_ascii=False)
    except Exception as e:
        logger.error("check_async_task failed: %s", e, exc_info=True)
        return f"❌ Error checking async task: {e}"


async def _cancel_async_task(from_agent_id: uuid.UUID, args: dict) -> str:
    """Cancel a previously spawned async task if it belongs to the current agent."""
    task_id = (args.get("task_id") or "").strip()
    if not task_id:
        return "❌ Please provide task_id"

    try:
        from app.agents.orchestrator import cancel_async_delegation
        from app.services.runtime_task_service import get_runtime_task_record

        try:
            record = await get_runtime_task_record(task_id)
        except Exception:
            record = None
        if record and record.get("parent_agent_id") not in {None, str(from_agent_id)}:
            return "❌ This task does not belong to the current agent"

        status = await cancel_async_delegation(task_id, parent_agent_id=from_agent_id)
        if status.get("status") == "forbidden":
            return "❌ This task does not belong to the current agent"
        return json.dumps(status, ensure_ascii=False)
    except Exception as e:
        logger.error("cancel_async_task failed: %s", e, exc_info=True)
        return f"❌ Error cancelling async task: {e}"


async def _list_async_tasks(from_agent_id: uuid.UUID) -> str:
    """List recent async runtime tasks created by the current agent."""
    try:
        from app.agents.orchestrator import list_async_delegations
        from app.services.runtime_task_service import list_runtime_task_records

        try:
            tasks = await list_runtime_task_records(parent_agent_id=from_agent_id, limit=20)
        except Exception:
            tasks = []
        if not tasks:
            tasks = list_async_delegations(parent_agent_id=from_agent_id)
        return json.dumps(tasks, ensure_ascii=False)
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
        return json.dumps({
            "timezone": timezone_name,
            "local_time": now.isoformat(),
            "utc_time": now.astimezone(timezone.utc).isoformat(),
            "weekday": now.strftime("%A"),
        }, ensure_ascii=False)
    except Exception as e:
        logger.error("get_current_time failed: %s", e, exc_info=True)
        return f"❌ Error getting current time: {e}"


async def _feishu_user_search(agent_id: uuid.UUID, arguments: dict) -> str:
    """Proxy to feishu_users domain module (lazy import to avoid circular deps)."""
    from app.services.agent_tool_domains.feishu_users import _feishu_user_search as _real_search
    return await _real_search(agent_id, arguments)
