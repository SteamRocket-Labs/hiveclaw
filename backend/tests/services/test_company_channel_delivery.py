"""Provider acknowledgements must distinguish refusal from an unknown send."""

import httpx
import pytest

from app.api.wecom import _send_wecom_text_message


@pytest.mark.parametrize('payload', [{}, {'errcode': 0, 'invaliduser': 'member-one'}, {'errcode': 0, 'invalidparty': '4'}])
async def test_wecom_does_not_claim_success_without_complete_recipient_receipt(monkeypatch, payload):
    factory = httpx.AsyncClient
    def respond(request):
        return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-token'}
            if request.url.path.endswith('/gettoken') else payload)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError):
        await _send_wecom_text_message(corp_id='synthetic-corp', corp_secret='synthetic-secret',
            agent_id='100001', to_user='member-one', text='Completed')


@pytest.mark.parametrize('url', ['http://oapi.dingtalk.com/robot/send', 'https://oapi.dingtalk.com.attacker.test/robot/send',
    'https://oapi.dingtalk.com@attacker.test/robot/send', 'https://user:password@oapi.dingtalk.com/robot/send',
    'https://oapi.dingtalk.com:444/robot/send', 'https://oapi.dingtalk.com/unrelated'])
async def test_dingtalk_rejects_a_foreign_reply_sink_before_network(monkeypatch, url):
    from app.services.channel_provider_receipts import ProviderReceiptError, send_dingtalk_text
    def forbidden(**_kwargs):
        pytest.fail('An untrusted endpoint must not create an HTTP client')
    monkeypatch.setattr(httpx, 'AsyncClient', forbidden)
    with pytest.raises(ProviderReceiptError) as error:
        await send_dingtalk_text(url, 'Private company result')
    assert error.value.status == 'denied' and not error.value.retryable


@pytest.mark.parametrize('code,payload,status,retryable', [(200, {}, 'unknown', False), (500, {}, 'unknown', False),
    (429, {}, 'failed', True), (200, {'errcode': 310000, 'errmsg': 'SYNTHETIC_PRIVATE_BODY'}, 'failed', False),
    (200, {'errcode': False}, 'unknown', False)])
async def test_dingtalk_receipts_preserve_explicit_failure_and_unknown(monkeypatch, code, payload, status, retryable):
    from app.services.channel_provider_receipts import ProviderReceiptError, send_dingtalk_text
    factory = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(
        transport=httpx.MockTransport(lambda _: httpx.Response(code, json=payload)), **kwargs))
    with pytest.raises(ProviderReceiptError) as error:
        await send_dingtalk_text('https://oapi.dingtalk.com/robot/sendBySession?session=SYNTHETIC_PRIVATE_TOKEN', 'Work result')
    assert error.value.status == status and error.value.retryable == retryable
    assert 'SYNTHETIC_PRIVATE' not in str(error.value)


@pytest.mark.parametrize('outcome,status,retryable', [('confirmed', None, False), ('no_key', 'unknown', False),
    ('invalid', 'failed', False), ('filtered', 'failed', False), ('flow', 'failed', True),
    ('pending', 'unknown', False), ('query_failed', 'unknown', False), ('send_lost', 'unknown', False)])
async def test_dingtalk_file_requires_the_actual_recipient_and_delivery_receipt(monkeypatch, tmp_path, outcome, status, retryable):
    import json
    from app.services.channel_provider_receipts import ProviderReceiptError, send_dingtalk_file

    path = tmp_path / 'result.xlsx'
    path.write_bytes(b'synthetic-transport-bytes')
    calls, factory = [], httpx.AsyncClient
    def respond(request):
        calls.append(request)
        if request.url.path == '/gettoken':
            return httpx.Response(200, json={'errcode': 0, 'access_token': 'synthetic-oapi-token'})
        if request.url.path == '/media/upload':
            assert request.url.params['access_token'] == 'synthetic-oapi-token'
            return httpx.Response(200, json={'errcode': 0, 'media_id': '@synthetic-raw-media'})
        if request.url.path == '/v1.0/oauth2/accessToken':
            return httpx.Response(200, json={'accessToken': 'synthetic-v1-token'})
        assert request.headers['x-acs-dingtalk-access-token'] == 'synthetic-v1-token'
        if request.url.path.endswith('/batchSend'):
            data = json.loads(request.content)
            assert data['userIds'] == ['member-one'] and data['robotCode'] == 'synthetic-app'
            assert json.loads(data['msgParam'])['mediaId'] == '@synthetic-raw-media'
            if outcome == 'send_lost':
                raise httpx.ReadTimeout('synthetic-private-diagnostic', request=request)
            if outcome == 'no_key':
                return httpx.Response(200, json={})
            refused = {'invalid': 'invalidStaffIdList', 'filtered': 'filteredStaffIdList', 'flow': 'flowControlledStaffIdList'}
            return httpx.Response(200, json={'processQueryKey': 'synthetic-file-receipt',
                **({refused[outcome]: ['member-one']} if outcome in refused else {})})
        assert request.url.path.endswith('/readStatus') and request.url.params['processQueryKey'] == 'synthetic-file-receipt'
        if outcome == 'query_failed':
            return httpx.Response(403, json={'code': 'synthetic-private-diagnostic'})
        return httpx.Response(200, json={'sendStatus': 'SUCESS' if outcome == 'confirmed' else 'SENDING'})
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: factory(**{**kwargs, 'transport': httpx.MockTransport(respond)}))
    if status is None:
        assert await send_dingtalk_file(app_key='synthetic-app', app_secret='synthetic-secret', to_user='member-one', path=path) == {
            'provider_reference': 'synthetic-file-receipt'}
    else:
        with pytest.raises(ProviderReceiptError) as error:
            await send_dingtalk_file(app_key='synthetic-app', app_secret='synthetic-secret', to_user='member-one', path=path)
        assert error.value.status == status and error.value.retryable == retryable
        assert 'synthetic-private-diagnostic' not in str(error.value)
        if outcome in {'pending', 'query_failed'}:
            assert error.value.detail == {'provider_reference': 'synthetic-file-receipt'}
    assert sum(request.url.path.endswith('/batchSend') for request in calls) == 1
    assert sum(request.url.path.endswith('/readStatus') for request in calls) == (outcome in {'confirmed', 'pending', 'query_failed'})
