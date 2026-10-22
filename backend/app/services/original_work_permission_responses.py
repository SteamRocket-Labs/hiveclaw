"""Durable original-channel acknowledgements of native permission receipts."""

import uuid

from app.services.content_input_receipts import ContentInputReceiptError

SOURCE = 'original_work_permission_response'
SCHEMA = 'hive.original_work_permission_response_notice.v1'
STATUS = 'permission_response'


def response_prompt_id(receipt):
    if receipt['status'] not in {'resolved', 'waiting_for_sibling_permissions', 'awaiting_tool_result', 'needs_reconciliation'}:
        raise ValueError('unsupported_native_permission_receipt')
    return uuid.uuid5(uuid.UUID(receipt['control_id']), f'original-work-permission-response:{receipt["status"]}')


def permission_response_text(run, record, receipt, decision):
    from app.config import get_settings

    if decision not in {'allow_once', 'allow_session', 'deny'}:
        raise ValueError('unsupported_native_permission_decision')
    choice = {'allow_once': '批准', 'allow_session': '会话授权', 'deny': '拒绝'}[decision]
    text = f'已记录本次{choice}。此提示不代表任务完成。'
    if receipt['status'] == 'needs_reconciliation':
        text += '\n这项操作的执行结果尚未确认；不会自动重试。请在原工作中核对。'
    elif receipt['status'] == 'awaiting_tool_result':
        text += '\n本次处理正在等待工具执行回执，请在原工作中查看最新状态。'
    elif receipt['status'] == 'waiting_for_sibling_permissions':
        text += '\n本次处理后仍有其他权限待处理，请打开原工作查看最新状态。'
    else:
        text += '\n请在原工作中查看后续进度和结果。'
    base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
    if base:
        anchor = f'session_id={record.session_id}#chat' if run.task_type == 'delegation' else f'task_id={record.id}#tasks'
        text += f'\n{base}/agents/{run.parent_agent_id}?{anchor}'
    return text


async def enqueue_original_work_permission_response(db, *, notice, run, record, receipt, decision, audit_event_id):
    from app.services.channel_delivery_outbox import ChannelDeliveryIntent, enqueue_channel_delivery

    return await enqueue_channel_delivery(db, ChannelDeliveryIntent(
        tenant_id=notice.tenant_id, runtime_task_id=run.id, agent_id=notice.agent_id, session_id=notice.session_id,
        user_id=notice.user_id, external_principal_id=notice.external_principal_id,
        channel_config_id=notice.channel_config_id, delivery_target=notice.delivery_target_json,
        text=permission_response_text(run, record, receipt, decision), terminal_status=STATUS,
        delivery_kind='interactive_prompt', prompt_item_id=response_prompt_id(receipt),
        metadata={'source': SOURCE, 'schema': SCHEMA, 'original_notice_id': str(notice.id),
            'audit_event_id': str(audit_event_id), 'permission_receipt': receipt}))


async def authorize_original_work_permission_response(db, *, item, session, user):
    from app.core.permissions import authorize_session_action
    from app.core.policy import compute_audit_event_hash
    from app.core.resource_authority import authorize_resource_action
    from app.core.source_read_purpose import source_recipient_read
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.external_principal import ExternalPrincipal
    from app.models.runtime_task import RuntimeTask
    from app.models.security_audit import SecurityAuditEvent
    from app.models.session_v2 import SessionCommand, SessionControlInput, SessionToolInvocation
    from app.services.channel_delivery_service import ChannelDeliveryService
    from app.services.channel_ingress_inbox import channel_installation_ref
    from app.services.channel_provider_receipts import feishu_message_scope
    from app.services.original_work_permission_notices import _original_work
    from app.services.session_control_input import _sha256

    def invalid():
        return ContentInputReceiptError('permission_response_notice_changed', 'Check the original permission response.', 403)

    try:
        receipt = item.metadata['permission_receipt']
        notice = await db.get(ChannelDeliveryOutbox, uuid.UUID(item.metadata['original_notice_id']), populate_existing=True)
        audit = await db.get(SecurityAuditEvent, uuid.UUID(item.metadata['audit_event_id']), populate_existing=True)
        run = await db.get(RuntimeTask, item.runtime_task_id, populate_existing=True)
        control = await db.get(SessionControlInput, uuid.UUID(receipt['control_id']), populate_existing=True)
        command = await db.get(SessionCommand, control.command_id) if control else None
        invocation = await db.get(SessionToolInvocation, uuid.UUID(receipt['invocation_id']))
        group = session is not None and session.audience_kind == 'group'
        principal_id = (audit.details or {}).get('external_principal_id') if group and audit is not None else item.external_principal_id
        principal = await db.get(ExternalPrincipal, uuid.UUID(str(principal_id))) if principal_id else None
        if (item.metadata.get('source') != SOURCE or item.metadata.get('schema') != SCHEMA
            or item.terminal_status != STATUS or item.delivery_kind != 'interactive_prompt' or item.artifact_ids
            or notice is None or audit is None or run is None or control is None or command is None or invocation is None
            or session is None or user is None or user.id != item.user_id or run.root_user_id != user.id
            or run.tenant_id != item.tenant_id or run.parent_agent_id != item.agent_id
            or run.parent_session_id != str(item.session_id) or session.id != item.session_id
            or session.tenant_id != item.tenant_id or session.agent_id != item.agent_id
            or notice.tenant_id != item.tenant_id or notice.runtime_task_id != run.id
            or notice.session_id != item.session_id or notice.agent_id != item.agent_id or notice.user_id != user.id
            or notice.channel_config_id != item.channel_config_id or notice.external_principal_id != item.external_principal_id
            or notice.channel != item.channel or notice.delivery_target_json != item.delivery_target
            or notice.metadata_json.get('source') != 'original_work_permission'
            or notice.metadata_json.get('schema') != 'hive.original_work_permission_notice.v1'
            or receipt.get('schema') != 'hive.session_permission_resolution.v2' or receipt.get('run_id') != str(run.id)
            or receipt.get('permission_request_id') != notice.prompt_key or str(response_prompt_id(receipt)) != item.prompt_key
            or invocation.tenant_id != item.tenant_id or invocation.run_id != run.id
            or invocation.session_id != uuid.UUID(run.child_session_id) or str(invocation.permission_item_id) != notice.prompt_key
            or notice.metadata_json.get('invocation_id') != str(invocation.id)
            or control.tenant_id != item.tenant_id or control.session_id != invocation.session_id
            or control.kind != 'permission_response' or control.status != 'applied'
            or control.expected_run_id != run.id or control.request_item_id != invocation.permission_item_id
            or command.tenant_id != item.tenant_id or command.session_id != invocation.session_id
            or command.principal_type != 'user' or command.principal_id != user.id
            or command.command_kind != 'permission_response' or command.target_json.get('invocation_id') != str(invocation.id)
            or control.response_schema != 'hive.tool_permission_response.v1'
            or control.response_payload_hash != _sha256(control.response_payload_json)
            or audit.tenant_id != item.tenant_id or audit.actor_type != 'user' or audit.actor_id != user.id
            or audit.event_type != 'original_work.permission_response_via_channel'
            or audit.action != 'respond_original_work_permission' or audit.resource_type != 'runtime_task' or audit.resource_id != run.id
            or audit.details.get('original_notice_id') != str(notice.id) or audit.details.get('permission_receipt') != receipt
            or audit.details.get('external_principal_id') != str(principal.id if principal else None)
            or audit.details.get('channel_ingress_event_id') != str(audit.request_id)
            or principal is None or principal.tenant_id != item.tenant_id or principal.linked_user_id != user.id
            or item.channel != 'feishu'):
            raise invalid()
        if group:
            if (item.external_principal_id is not None or item.delivery_target.get('receive_id_type') != 'chat_id'
                or item.delivery_target.get('receive_id') != session.group_subject_id
                or item.delivery_target.get('chat_id') != session.group_subject_id):
                raise invalid()
        elif (item.delivery_target.get('receive_id_type') != 'open_id'
            or item.delivery_target.get('receive_id') != principal.subject_id):
            raise invalid()
        config = await ChannelDeliveryService._load_target_config(db, item.agent_id, item.channel, item.delivery_target)
        scope = ((notice.delivery_receipts_json or {}).get('text', {}).get('detail') or {}).get('provider_message_scope')
        if (config is None or config.id != item.channel_config_id or principal.installation_ref != channel_installation_ref(config, fallback='')
            or scope is None or scope != feishu_message_scope(config)):
            raise invalid()
        audit_fields = {key: getattr(audit, key) for key in ('event_type', 'severity', 'actor_type', 'actor_id', 'tenant_id',
            'action', 'resource_type', 'resource_id', 'details', 'ip_address', 'request_id', 'execution_identity_type',
            'execution_identity_id', 'execution_identity_label', 'prev_hash')}
        if compute_audit_event_hash(**audit_fields) != audit.event_hash:
            raise invalid()
        work = await _original_work(db, run)
        if work is None:
            raise invalid()
        _original_session, record, _binding, _target = work
        if item.text != permission_response_text(run, record, receipt, control.response_payload_json['decision']):
            raise invalid()
        if group:
            from app.services.external_principal_service import load_external_runtime_actor
            from app.services.shared_group_history import authorize_group_history_reader

            actor = await load_external_runtime_actor(db, tenant_id=item.tenant_id,
                principal_id=principal.id, expected_user_id=user.id)
            if not actor.authority_bound:
                raise invalid()
            await authorize_group_history_reader(db, session=session, user=user, audit=False)
        else:
            await authorize_session_action(db, user, agent_id=item.agent_id, session_id=item.session_id,
                action='read_content_input_source', audit=False)
        if run.task_type == 'business_task':
            with source_recipient_read(session_id=session.id, user_id=user.id):
                await authorize_resource_action(db, user, agent_id=record.agent_id, resource_kind='task', resource_id=record.id,
                    action='read', owner_user_id=record.created_by, root_session_id=record.root_session_id,
                    authority_state=record.authority_state)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise invalid() from exc
