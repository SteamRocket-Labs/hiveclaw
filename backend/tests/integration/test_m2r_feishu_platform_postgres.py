"""The selected connection owns token, action and source receipt namespaces."""

import pytest
from sqlalchemy import select

from app.models.invocation_span import InvocationSpan
from app.models.tenant_channel_config import TenantChannelConfig
from tests.integration.test_m2p_feishu_discovery_postgres import (  # noqa: F401
    ordinary_work, _exercise_ordinary_office_work,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("extra_config,origin", [
    ({"platform_region": "lark_global"}, "https://open.larksuite.com"),
    ({"platform_region": "custom", "open_api_domain": "https://office.fixture.test"}, "https://office.fixture.test"),
])
async def test_selected_platform_reaches_original_work_and_verified_artifact(
    ordinary_work, owner_sessionmaker, app_user_sessionmaker, monkeypatch, extra_config, origin,  # noqa: F811
):
    seed = ordinary_work
    async with owner_sessionmaker() as db:
        config = await db.get(TenantChannelConfig, seed["config_id"])
        config.extra_config = extra_config
        await db.commit()

    async def inspect_receipts(*, app, client, report, requests, **_kwargs):
        async with owner_sessionmaker() as db:
            spans = (await db.scalars(select(InvocationSpan).where(
                InvocationSpan.runtime_task_id == seed["run_id"], InvocationSpan.span_type == "feishu_read"))).all()
            assert len(spans) == 2
            for span in spans:
                connection = span.metadata_json["feishu_read"]["connection"]
                assert connection["platform_region"] == extra_config["platform_region"]
                assert connection["open_api_domain"] == origin
            config = await db.get(TenantChannelConfig, seed["config_id"])
            config.extra_config = {"platform_region": "feishu_cn"}
            await db.commit()
        import httpx

        before = list(requests)
        async with client(transport=httpx.ASGITransport(app=app), base_url="http://platform-work") as api:
            denied = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
            assert denied.status_code == 403, denied.text
            async with owner_sessionmaker() as db:
                config = await db.get(TenantChannelConfig, seed["config_id"])
                config.extra_config = extra_config
                await db.commit()
            restored = await api.get(f"/agents/{seed['agent_id']}/files/artifacts/{report.id}/content")
            assert restored.status_code == 200, restored.text
        assert requests == before

    await _exercise_ordinary_office_work(seed, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
        provider_origin=origin, follow_up=inspect_receipts)
