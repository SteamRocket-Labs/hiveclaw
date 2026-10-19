"""Provider resource capabilities stay behind authenticated company file ingress."""

from urllib.parse import urlsplit
import mimetypes

import httpx
from fastapi import HTTPException

from app.services.feishu_uploads import save_channel_upload
from app.services.channel_provider_receipts import ProviderReceiptError, dingtalk_access_token, _wecom_token
from app.services.governed_egress import EgressLimits, GovernedEgressError, request_governed_http

COMPANY_FILE_MAX_BYTES = 20 * 1024 * 1024


async def _dingtalk_json(client, path, **kwargs):
    try:
        response = await client.post('https://api.dingtalk.com' + path, **kwargs)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(503, {'code': 'company_file_provider_unavailable',
            'message': 'The file provider is unavailable. Retry or upload the file in the original Web work.'}) from None
    if not isinstance(payload, dict):
        raise HTTPException(503, {'code': 'company_file_provider_receipt_invalid', 'message': 'The file provider response is incomplete.'})
    return payload


async def materialize_company_attachment(db, *, config, actor, session, attachment, event_id):
    provider = config.channel_type
    allowed = {'file'} if provider == 'dingtalk' else {'image', 'voice', 'video'} if provider == 'wecom' else set()
    if (not isinstance(attachment, dict) or attachment.get('kind') not in allowed
        or not isinstance(attachment.get('resource_key'), str) or not attachment['resource_key']):
        raise HTTPException(422, {'code': 'company_file_invalid', 'message': 'The authenticated file resource is incomplete.'})
    code = attachment['resource_key']
    params = None
    async with httpx.AsyncClient(timeout=20) as client:
        try:
            token = await (dingtalk_access_token(client, config.app_id, config.app_secret) if provider == 'dingtalk'
                else _wecom_token(client, config.app_id, config.app_secret))
        except ProviderReceiptError:
            raise HTTPException(503, {'code': 'company_file_credentials_unavailable', 'message': 'The company file credential is unavailable.'}) from None
        if provider == 'dingtalk':
            download = await _dingtalk_json(client, '/v1.0/robot/messageFiles/download',
                headers={'x-acs-dingtalk-access-token': token}, json={'robotCode': config.app_id, 'downloadCode': code})
            url = download.get('downloadUrl')
        else:
            url = 'https://qyapi.weixin.qq.com/cgi-bin/media/get'
            params = {'access_token': token, 'media_id': code}
    if not isinstance(url, str) or urlsplit(url).scheme != 'https':
        raise HTTPException(503, {'code': 'company_file_download_unavailable', 'message': 'The provider did not issue a secure file download.'})
    try:
        response = await request_governed_http('GET', url, params=params, secret_values=(token, code),
            limits=EgressLimits(max_wire_bytes=COMPANY_FILE_MAX_BYTES, max_decoded_bytes=COMPANY_FILE_MAX_BYTES))
    except (GovernedEgressError, httpx.HTTPError, TimeoutError) as exc:
        code = exc.code if isinstance(exc, GovernedEgressError) else 'company_file_download_unavailable'
        raise HTTPException(503, {'code': code, 'message': 'The file download was refused or exceeded its resource limit. Retry or attach a new file in the original Web work.'}) from None
    content_type = response.headers.get('content-type', 'application/octet-stream').split(';', 1)[0].strip().lower()
    if response.status_code != 200 or provider == 'wecom' and content_type == 'application/json':
        raise HTTPException(409, {'code': 'company_file_expired', 'message': 'The file is unavailable. Upload a new message or attach it in the original Web work.'})
    filename = attachment.get('filename') if provider == 'dingtalk' else attachment['kind'] + (mimetypes.guess_extension(content_type) or '.bin')
    path = await save_channel_upload(db, actor=actor, session=session, provider=config.channel_type,
        message_id=str(event_id), resource_key=attachment['resource_key'], filename=filename, content=response.content)
    return {'path': path, 'name': path.rsplit('/', 1)[-1], 'size': len(response.content),
        'content_type': content_type}
