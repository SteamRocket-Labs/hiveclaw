"""Concurrent first desktop callbacks preserve one provider and account."""

import asyncio
import json
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import select

from app.models.identity import ExternalIdentity, IdentityProvider
from app.models.user import User
from tests.integration.conftest import app_user_engine, migrated_pg_url as _migrated_pg_url, pg_container as _pg_container  # noqa: F401
from tests.integration.test_m1c_repair_postgres import company  # noqa: F401
from tests.integration.test_oauth_subject_boundary import application, provider_transport

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
def pg_container():
    yield from _pg_container.__wrapped__()


@pytest.fixture
def migrated_pg_url(pg_container):
    return _migrated_pg_url.__wrapped__(pg_container)


async def test_first_concurrent_desktop_callbacks_share_the_union_only_account(company, monkeypatch, tmp_path):  # noqa: F811
    from app.api.desktop_auth import _store_oauth_state

    subject = "union-" + uuid.uuid4().hex
    first_profile = asyncio.Event()
    second_profile = asyncio.Event()
    release = asyncio.Event()

    async def at_profile():
        if not first_profile.is_set():
            first_profile.set()
            await asyncio.wait_for(release.wait(), 15)
        else:
            second_profile.set()

    calls = provider_transport(monkeypatch, {"union_id": subject, "name": "Concurrent desktop account"}, on_profile=at_profile)
    async with company.owner() as db:
        assert not (await db.scalars(select(IdentityProvider).where(
            IdentityProvider.provider_type == "feishu", IdentityProvider.tenant_id.is_(None)))).all()

    devices = ["cold-desktop-one", "cold-desktop-two"]
    nonces = [uuid.uuid4().hex, uuid.uuid4().hex]
    for nonce, device in zip(nonces, devices):
        await _store_oauth_state(nonce, device)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(company)), base_url="http://oauth.fixture") as client:
        async def callback(nonce):
            return await client.get("/auth/feishu/callback-desktop", params={"code": "synthetic-cold-code", "state": nonce})

        tasks = [asyncio.create_task(callback(nonces[0]))]
        try:
            await asyncio.wait_for(first_profile.wait(), 10)
            tasks.append(asyncio.create_task(callback(nonces[1])))
            # A correct cold-namespace lock may hold the second exchange until
            # the first transaction commits. Both schedules must converge.
            try:
                await asyncio.wait_for(second_profile.wait(), 3)
            except TimeoutError:
                pass
            release.set()
            responses = await asyncio.wait_for(asyncio.gather(*tasks), 20)
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        redirects = [urlparse(response.headers["location"]) for response in responses]
        queries = [parse_qs(redirect.query) for redirect in redirects]
        async with company.owner() as db:
            providers = list((await db.scalars(select(IdentityProvider).where(
                IdentityProvider.provider_type == "feishu", IdentityProvider.tenant_id.is_(None)))).all())
            users = list((await db.scalars(select(User).where(User.tenant_id.is_(None), User.feishu_union_id == subject))).all())
            identities = list((await db.scalars(select(ExternalIdentity).where(ExternalIdentity.provider_union_id == subject))).all())
            observed = {"accepted": [r.path == "/callback" for r in redirects],
                "provider_count": len(providers), "account_count": len(users), "identity_count": len(identities),
                "same_returned_user": queries[0].get("user_id") == queries[1].get("user_id"), "provider_calls": calls}
            (tmp_path / "oauth-cold-namespace-observation.json").write_text(json.dumps(observed, indent=2))
            assert observed["accepted"] == [True, True], observed
            assert len(providers) == len(users) == len(identities) == 1, observed
            assert identities[0].provider_id == providers[0].id and identities[0].user_id == users[0].id
            assert queries[0]["user_id"] == queries[1]["user_id"] == [str(users[0].id)]
            assert users[0].role == "member" and users[0].is_active is True
        for query, device in zip(queries, devices):
            refreshed = await client.post("/auth/desktop/exchange", json={"refresh_token": query["refresh_token"][0], "device_id": device})
            assert refreshed.status_code == 200 and refreshed.json()["access_token"]
