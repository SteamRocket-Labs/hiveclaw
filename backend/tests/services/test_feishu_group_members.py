"""The official SDK membership reader must not turn a partial response into authority."""

from types import SimpleNamespace as NS

import pytest

from app.services.feishu_service import FeishuService


def page(ids, *, more=False, token="", total=2, limited=False, ok=True):
    return NS(success=lambda: ok, data=NS(items=[NS(member_id_type="open_id", member_id=value) for value in ids],
        has_more=more, page_token=token, member_total=total, trigger_security_conf_limit=limited))


async def test_group_membership_reads_every_sdk_page_and_does_not_cache(monkeypatch):
    responses = [page(["ou_first"], more=True, token="next"), page(["ou_second"]), page(["ou_second"], total=1)]
    calls = []
    async def get(request):
        calls.append(request)
        return responses.pop(0)
    service = FeishuService()
    monkeypatch.setattr(service, "_get_lark_client", lambda *_args, **_kwargs: NS(im=NS(v1=NS(chat_members=NS(aget=get)))))
    assert await service.get_chat_member_open_ids("synthetic-app", "synthetic-secret", "oc_group") == {"ou_first", "ou_second"}
    assert await service.get_chat_member_open_ids("synthetic-app", "synthetic-secret", "oc_group") == {"ou_second"}
    assert [request.page_token for request in calls] == [None, "next", None]
    assert all(request.chat_id == "oc_group" and request.member_id_type == "open_id" and request.page_size == 100 for request in calls)


@pytest.mark.parametrize("responses", [
    [page([], ok=False)], [page(["ou_first"], limited=True)], [page(["ou_first"], more=True)],
    [page(["ou_first"], total=2)], [page(["ou_first"], more=None)], [page([""])],
    [page(["ou_first"], more=True, token="repeat"), page(["ou_second"], more=True, token="repeat")],
])
async def test_incomplete_group_observation_is_unavailable(monkeypatch, responses):
    async def get(_request):
        return responses.pop(0)
    service = FeishuService()
    monkeypatch.setattr(service, "_get_lark_client", lambda *_args, **_kwargs: NS(im=NS(v1=NS(chat_members=NS(aget=get)))))
    with pytest.raises(RuntimeError, match="group_membership_unavailable"):
        await service.get_chat_member_open_ids("synthetic-app", "synthetic-secret", "oc_group")
