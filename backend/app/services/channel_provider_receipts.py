"""Exact provider receipts without exporting URLs, credentials or message bodies."""

from urllib.parse import urlsplit
import mimetypes

import httpx


class ProviderReceiptError(ValueError):
    def __init__(self, message, *, status='failed', retryable=False):
        super().__init__(message)
        self.status, self.retryable = status, retryable


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
