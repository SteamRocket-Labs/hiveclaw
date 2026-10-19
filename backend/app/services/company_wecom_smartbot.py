"""Company Smart Bot protocol, subject conversion and one-use response capability."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from fastapi import HTTPException
import httpx
from sqlalchemy import select

from app.services.channel_provider_receipts import ProviderReceiptError, _send, _wecom_token


def valid_response_url(value):
    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)
        return (parsed.scheme == 'https' and parsed.hostname == 'qyapi.weixin.qq.com'
            and parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment
            and parsed.path == '/cgi-bin/aibot/response' and set(query) == {'response_code'}
            and len(query['response_code']) == 1 and bool(query['response_code'][0]))
    except (TypeError, ValueError):
        return False


async def _canonical_subject(config, subject):
    if (config.extra_config or {}).get('wecom_bot_userid_mode') == 'plaintext':
        return subject
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            token = await _wecom_token(client, config.app_id, config.app_secret)
            response = await client.post('https://qyapi.weixin.qq.com/cgi-bin/batch/openuserid_to_userid',
                params={'access_token': token}, json={'open_userid_list': [subject]})
            response.raise_for_status()
            payload = response.json()
    except (ProviderReceiptError, httpx.HTTPError, ValueError):
        raise HTTPException(503, {'code': 'company_bot_subject_conversion_unavailable'}) from None
    if not isinstance(payload, dict) or type(payload.get('errcode')) is not int or payload['errcode'] != 0:
        raise HTTPException(503, {'code': 'company_bot_subject_conversion_unavailable'})
    rows = payload.get('userid_list')
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
        or rows[0].get('open_userid') != subject or not isinstance(rows[0].get('userid'), str)
        or not rows[0]['userid'] or payload.get('invalid_open_userid_list')):
        raise HTTPException(403, {'code': 'company_bot_subject_unverified'})
    return rows[0]['userid']


async def accept_smartbot_callback(db, *, config, raw, signature, timestamp, nonce):
    from app.api.wecom import authenticate_wecom_callback
    from app.services.company_channel_runtime import accept_company_message

    try:
        envelope = json.loads(raw)
    except ValueError:
        raise HTTPException(400, 'Invalid Smart Bot callback JSON') from None
    if not isinstance(envelope, dict) or not isinstance(envelope.get('encrypt'), str):
        raise HTTPException(400, 'Invalid Smart Bot encrypted frame')
    plain = authenticate_wecom_callback(config, signature=signature, timestamp=timestamp, nonce=nonce,
        encrypted=envelope['encrypt'], expected_recipient='')
    try:
        message = json.loads(plain)
    except ValueError:
        raise HTTPException(400, 'Invalid Smart Bot message JSON') from None
    if not isinstance(message, dict) or message.get('aibotid') != config.extra_config['wecom_bot_id']:
        raise HTTPException(403, 'Smart Bot installation rejected')
    if message.get('chattype') != 'single' or message.get('compat_info'):
        return {'routed': False, 'state': 'unsupported_conversation'}
    kind = message.get('msgtype')
    if kind not in {'text', 'file', 'image', 'video', 'voice'}:
        return {'routed': False, 'state': 'unsupported_message_type'}
    sender = message.get('from')
    subject = sender.get('userid') if isinstance(sender, dict) else None
    if not isinstance(subject, str) or not subject or not isinstance(message.get('msgid'), str) or not message['msgid']:
        raise HTTPException(422, 'Smart Bot message identity is missing')
    subject = await _canonical_subject(config, subject)
    content = message.get(kind)
    if not isinstance(content, dict):
        raise HTTPException(422, 'Smart Bot message content is missing')
    user_text = content.get('content', '') if kind in {'text', 'voice'} else ''
    if not isinstance(user_text, str):
        raise HTTPException(422, 'Smart Bot text must be a string')
    attachment = None
    if kind in {'file', 'image', 'video'}:
        resource = content.get('url')
        if not isinstance(resource, str) or not resource:
            raise HTTPException(422, 'Smart Bot file resource is missing')
        attachment = {'kind': kind, 'resource_key': resource}
    target = {'wecom_transport': 'smart_bot', 'bot_id': config.extra_config['wecom_bot_id'],
        'response_url': message.get('response_url')}
    if not valid_response_url(target['response_url']):
        raise HTTPException(422, 'Smart Bot response capability is missing or invalid')
    return await accept_company_message(db, config=config, subject_id=subject, text=user_text,
        event_id=message['msgid'], delivery_target=target, attachment=attachment)


async def send_smartbot_text(db, *, config, target, text, outbox_id):
    """Commit a one-use claim before sending; a lost receipt never permits replay."""
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.audit import ChatMessage

    try:
        event_id, delivery_id = UUID(str(target['source_event_id'])), UUID(str(outbox_id))
    except (ValueError, KeyError, TypeError):
        raise ProviderReceiptError('Smart Bot replies require the original durable outbox', status='denied') from None
    url = target.get('response_url')
    if not valid_response_url(url):
        raise ProviderReceiptError('Smart Bot response capability is invalid', status='denied')
    if len(text.encode('utf-8')) > 20480:
        raise ProviderReceiptError('Smart Bot reply exceeds 20480 UTF-8 bytes; revise the reply in the original work', status='unavailable')
    event = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == event_id,
        ChannelIngressEvent.tenant_id == config.tenant_id).with_for_update().execution_options(populate_existing=True))
    delivery = await db.get(ChannelDeliveryOutbox, delivery_id, populate_existing=True)
    message = await db.scalar(select(ChatMessage).where(ChatMessage.source_ingress_event_id == event_id,
        ChatMessage.tenant_id == config.tenant_id, ChatMessage.role == 'user'))
    original = ((event.payload_json or {}).get('body') or {}).get('delivery_target') or {} if event else {}
    if (event is None or delivery is None or delivery.status != 'processing' or delivery.tenant_id != config.tenant_id
        or delivery.agent_id != event.agent_id or message is None or message.conversation_id != str(delivery.session_id)
        or message.user_id != delivery.user_id or message.external_principal_id != delivery.external_principal_id
        or delivery.delivery_target_json != target
        or event.installation_ref != target.get('installation_ref') or event.provider != 'wecom'
        or original.get('bot_id') != target.get('bot_id') or not isinstance(original.get('response_url'), str)
        or not hmac.compare_digest(original['response_url'], url)):
        raise ProviderReceiptError('Smart Bot response is not bound to the original work', status='denied')
    if event.created_at + timedelta(hours=1) <= datetime.now(timezone.utc):
        raise ProviderReceiptError('Smart Bot response expired; continue in the original Web work or send a new Bot message', status='unavailable')
    claim = (event.metadata_json or {}).get('smartbot_response_claim') or {}
    if claim and not (claim.get('state') == 'unavailable' and claim.get('retryable') is True):
        status = 'unknown' if claim.get('state') in {'sending', 'unknown'} else 'unavailable'
        raise ProviderReceiptError('Smart Bot response was already used; reconcile or continue in the original Web work', status=status)
    claim = {'delivery_id': str(delivery_id), 'capability_sha256': hashlib.sha256(url.encode()).hexdigest(), 'state': 'sending'}
    event.metadata_json = {**(event.metadata_json or {}), 'smartbot_response_claim': claim}
    await db.commit()
    failure = None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await _send(client, url, json={'msgtype': 'markdown', 'markdown': {'content': text}})
    except ProviderReceiptError as exc:
        failure = exc
    event = await db.scalar(select(ChannelIngressEvent).where(ChannelIngressEvent.id == event_id,
        ChannelIngressEvent.tenant_id == config.tenant_id).with_for_update().execution_options(populate_existing=True))
    event.metadata_json = {**event.metadata_json, 'smartbot_response_claim': {**claim,
        'state': failure.status if failure else 'delivered', 'retryable': failure.retryable if failure else False}}
    await db.commit()
    if failure:
        raise failure
    return {'artifact_delivery': 'authenticated_work_link'}
