"""Exact provider receipts without exporting URLs, credentials or message bodies."""

from urllib.parse import urlsplit
import hashlib
import json
import mimetypes

import httpx


def feishu_message_scope(config):
    """Bind provider message IDs to the credential's actual app and API namespace."""
    from app.services.feishu_platform import resolve_feishu_platform

    identity = [str(getattr(config, 'id', None)), str(config.app_id),
        resolve_feishu_platform(getattr(config, 'extra_config', None)).open_api_domain]
    return hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()


class ProviderReceiptError(ValueError):
    def __init__(self, message, *, status='failed', retryable=False, detail=None):
        super().__init__(message)
        self.status, self.retryable = status, retryable
        self.detail = dict(detail or {})


async def _send(client, url, **kwargs):
    try:
        response = await client.post(url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise ProviderReceiptError('Provider connection unavailable', status='unavailable', retryable=True) from None
    except httpx.HTTPError:
        raise ProviderReceiptError('Provider send outcome is unknown', status='unknown') from None
    if 400 <= response.status_code < 500:
        raise ProviderReceiptError(f'Provider refused the send (HTTP {response.status_code})',
            retryable=response.status_code == 429)
    if response.status_code != 200:
        raise ProviderReceiptError('Provider send has no authoritative receipt', status='unknown')
    try:
        payload = response.json()
    except ValueError:
        raise ProviderReceiptError('Provider send receipt is unreadable', status='unknown') from None
    if not isinstance(payload, dict) or type(payload.get('errcode')) is not int:
        raise ProviderReceiptError('Provider send receipt is incomplete', status='unknown')
    if payload['errcode'] != 0:
        raise ProviderReceiptError(f'Provider rejected the send (code {payload["errcode"]})')
    return payload


async def send_dingtalk_text(session_webhook, text):
    try:
        target = urlsplit(session_webhook)
        valid = (target.scheme == 'https' and target.hostname == 'oapi.dingtalk.com' and target.port in (None, 443)
            and not target.username and not target.password and not target.fragment
            and target.path in {'/robot/sendBySession', '/robot/send'})
    except ValueError:
        valid = False
    if not valid:
        raise ProviderReceiptError('DingTalk reply endpoint is invalid', status='denied')
    async with httpx.AsyncClient(timeout=10) as client:
        return await _send(client, session_webhook,
            json={'msgtype': 'markdown', 'markdown': {'title': 'AI Reply', 'text': text}})


async def _dingtalk_response(client, method, path, **kwargs):
    try:
        response = await client.request(method, 'https://api.dingtalk.com' + path, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise ProviderReceiptError('DingTalk connection unavailable', status='unavailable', retryable=True) from None
    except httpx.HTTPError:
        raise ProviderReceiptError('DingTalk request outcome is unknown', status='unknown') from None
    if 400 <= response.status_code < 500:
        raise ProviderReceiptError(f'DingTalk refused the request (HTTP {response.status_code})', retryable=response.status_code == 429)
    if response.status_code != 200:
        raise ProviderReceiptError('DingTalk request has no authoritative receipt', status='unknown')
    try:
        payload = response.json()
    except ValueError:
        raise ProviderReceiptError('DingTalk response is unreadable', status='unknown') from None
    if not isinstance(payload, dict):
        raise ProviderReceiptError('DingTalk response is incomplete', status='unknown')
    return payload


async def dingtalk_access_token(client, app_key, app_secret):
    try:
        payload = await _dingtalk_response(client, 'POST', '/v1.0/oauth2/accessToken',
            json={'appKey': app_key, 'appSecret': app_secret})
    except ProviderReceiptError as exc:
        if exc.status == 'unknown':
            raise ProviderReceiptError('DingTalk credentials are unavailable', status='unavailable', retryable=True) from None
        raise
    token = payload.get('accessToken')
    if not isinstance(token, str) or not token:
        raise ProviderReceiptError('DingTalk token receipt is incomplete', status='unavailable', retryable=True)
    return token


async def send_dingtalk_file(*, app_key, app_secret, to_user, path):
    if not to_user:
        raise ProviderReceiptError('DingTalk file recipient is unavailable', status='denied')
    if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
        raise ProviderReceiptError('DingTalk file must be nonempty and at most 20 MiB', status='denied')
    async with httpx.AsyncClient(timeout=30) as client:
        # OAPI upload and v1 robot APIs have separate credential contracts.
        try:
            response = await client.get('https://oapi.dingtalk.com/gettoken',
                params={'appkey': app_key, 'appsecret': app_secret})
            response.raise_for_status()
            credentials = response.json()
        except (httpx.HTTPError, ValueError):
            raise ProviderReceiptError('DingTalk upload credentials are unavailable', status='unavailable', retryable=True) from None
        if (not isinstance(credentials, dict) or type(credentials.get('errcode')) is not int or credentials['errcode'] != 0
            or not isinstance(credentials.get('access_token'), str) or not credentials['access_token']):
            raise ProviderReceiptError('DingTalk upload token was rejected')
        with path.open('rb') as source:
            media = await _send(client, 'https://oapi.dingtalk.com/media/upload',
                params={'access_token': credentials['access_token'], 'type': 'file'},
                files={'media': (path.name, source, 'application/octet-stream')})
        media_id = media.get('media_id')
        if not isinstance(media_id, str) or not media_id:
            raise ProviderReceiptError('DingTalk upload receipt is incomplete', status='unavailable', retryable=True)
        token = await dingtalk_access_token(client, app_key, app_secret)
        headers = {'x-acs-dingtalk-access-token': token}
        sent = await _dingtalk_response(client, 'POST', '/v1.0/robot/oToMessages/batchSend', headers=headers,
            json={'robotCode': app_key, 'userIds': [to_user], 'msgKey': 'sampleFile',
                'msgParam': json.dumps({'mediaId': media_id, 'fileName': path.name, 'fileType': path.suffix.lstrip('.')})})
        for key in ('invalidStaffIdList', 'flowControlledStaffIdList', 'filteredStaffIdList'):
            if key in sent and not isinstance(sent[key], list):
                raise ProviderReceiptError('DingTalk recipient receipt is incomplete', status='unknown')
            if sent.get(key):
                raise ProviderReceiptError('DingTalk refused a file recipient', retryable=key == 'flowControlledStaffIdList')
        reference = sent.get('processQueryKey')
        if not isinstance(reference, str) or not reference:
            raise ProviderReceiptError('DingTalk file acceptance receipt is incomplete', status='unknown')
        detail = {'provider_reference': reference}
        try:
            confirmed = await _dingtalk_response(client, 'GET', '/v1.0/robot/oToMessages/readStatus',
                headers=headers, params={'robotCode': app_key, 'processQueryKey': reference})
        except ProviderReceiptError:
            raise ProviderReceiptError('DingTalk file delivery requires reconciliation', status='unknown', detail=detail) from None
        if confirmed.get('sendStatus') != 'SUCESS':
            raise ProviderReceiptError('DingTalk file delivery is not confirmed', status='unknown', detail=detail)
        return detail


async def _wecom_token(client, corp_id, corp_secret):
    try:
        response = await client.get('https://qyapi.weixin.qq.com/cgi-bin/gettoken',
            params={'corpid': corp_id, 'corpsecret': corp_secret})
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise ProviderReceiptError('WeCom credentials could not be verified', status='unavailable', retryable=True) from None
    if not isinstance(payload, dict) or type(payload.get('errcode')) is not int or payload['errcode'] != 0:
        raise ProviderReceiptError('WeCom token request was rejected')
    token = payload.get('access_token')
    if not isinstance(token, str) or not token:
        raise ProviderReceiptError('WeCom token receipt is incomplete', status='unavailable', retryable=True)
    return token


def _wecom_recipient(payload):
    if any(payload.get(key) for key in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser')):
        raise ProviderReceiptError('WeCom rejected a message recipient')
    return payload


async def send_wecom_text(*, corp_id, corp_secret, agent_id, to_user, text):
    async with httpx.AsyncClient(timeout=10) as client:
        token = await _wecom_token(client, corp_id, corp_secret)
        payload = await _send(client, 'https://qyapi.weixin.qq.com/cgi-bin/message/send', params={'access_token': token},
            json={'touser': to_user, 'msgtype': 'text', 'agentid': int(agent_id), 'text': {'content': text}})
        return _wecom_recipient(payload)


async def send_wecom_file(*, corp_id, corp_secret, agent_id, to_user, path):
    if not to_user or not agent_id:
        raise ProviderReceiptError('WeCom file recipient is unavailable', status='denied')
    if not 0 < path.stat().st_size <= 20 * 1024 * 1024:
        raise ProviderReceiptError('WeCom file must be nonempty and at most 20 MiB', status='denied')
    async with httpx.AsyncClient(timeout=30) as client:
        token = await _wecom_token(client, corp_id, corp_secret)
        with path.open('rb') as source:
            media = await _send(client, 'https://qyapi.weixin.qq.com/cgi-bin/media/upload',
                params={'access_token': token, 'type': 'file'},
                files={'media': (path.name, source, mimetypes.guess_type(path.name)[0] or 'application/octet-stream')})
        media_id = media.get('media_id')
        if not isinstance(media_id, str) or not media_id:
            raise ProviderReceiptError('WeCom upload receipt is incomplete', status='unavailable', retryable=True)
        return _wecom_recipient(await _send(client, 'https://qyapi.weixin.qq.com/cgi-bin/message/send',
            params={'access_token': token}, json={'touser': to_user, 'msgtype': 'file',
                'agentid': int(agent_id), 'file': {'media_id': media_id}}))
