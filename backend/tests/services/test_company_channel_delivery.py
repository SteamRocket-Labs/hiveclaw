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
