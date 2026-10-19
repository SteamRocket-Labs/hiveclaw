"""Exact provider receipts without exporting URLs, credentials or message bodies."""

from urllib.parse import urlsplit

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


async def send_wecom_text(*, corp_id, corp_secret, agent_id, to_user, text):
    async with httpx.AsyncClient(timeout=10) as client:
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
        payload = await _send(client, 'https://qyapi.weixin.qq.com/cgi-bin/message/send', params={'access_token': token},
            json={'touser': to_user, 'msgtype': 'text', 'agentid': int(agent_id), 'text': {'content': text}})
        if any(payload.get(key) for key in ('invaliduser', 'invalidparty', 'invalidtag', 'unlicenseduser')):
            raise ProviderReceiptError('WeCom rejected a message recipient')
        return payload
