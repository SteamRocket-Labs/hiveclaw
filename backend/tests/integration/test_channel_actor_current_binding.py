"""A long-lived runtime session cannot revive a removed channel binding."""

import json
from uuid import UUID

import httpx
import pytest

from app.database import pin_rls_tenant_context
from app.models.external_principal import ExternalPrincipal
from app.services.external_principal_service import (
    ExternalPrincipalRevokedError, load_external_runtime_actor, revoke_external_principals_for_installation,
)
from tests.integration.test_m2y_company_channel_identity_postgres import company_messaging, company_wecom_request  # noqa: F401


@pytest.mark.parametrize("change", ["disconnect", "revoke"])
async def test_cached_runtime_actor_rechecks_the_current_channel_binding(
    company_messaging, owner_sessionmaker, app_user_sessionmaker, tmp_path, change,  # noqa: F811
):
    seed = company_messaging
    scope = {"expected_tenant_id": str(seed["tenant_id"]), "expected_user_id": str(seed["user_id"])}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=seed["app"]), base_url="http://company") as api:
        issued = await api.post("/tenant-channels/wecom/identity/challenge", json=scope)
        assert issued.status_code == 200, issued.text
        config = seed["configs"]["wecom"]
        body, params = company_wecom_request(config, text=issued.json()["command"])
        redeemed = await api.post(f"/channel/wecom/tenant/{seed['tenant_id']}/webhook", content=body, params=params)
        assert redeemed.status_code == 200, redeemed.text
        status = await api.get("/tenant-channels/wecom/identity", params=scope)
        assert status.status_code == 200 and status.json()["state"] == "bound", status.text
        principal_id = UUID(status.json()["principal_id"])
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, seed["tenant_id"])
            cached = await db.get(ExternalPrincipal, principal_id)
            before = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=principal_id,
                expected_user_id=seed["user_id"])
            assert before.authority_bound and before.id == seed["user_id"]
            if change == "disconnect":
                removed = await api.post("/tenant-channels/wecom/identity/disconnect", json={**scope,
                    "principal_id": str(principal_id)})
                assert removed.status_code == 200, removed.text
            else:
                async with owner_sessionmaker() as owner:
                    count = await revoke_external_principals_for_installation(owner, tenant_id=seed["tenant_id"],
                        provider="wecom", installation_ref=cached.installation_ref, actor_user_id=seed["user_id"],
                        reason="Explicitly revoke the test installation")
                    assert count == 1
                    await owner.commit()
            async with owner_sessionmaker() as owner:
                current = await owner.get(ExternalPrincipal, principal_id)
                assert current.linked_user_id is None
                assert current.status == ("revoked" if change == "revoke" else "active")
            revoked = False
            try:
                after = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=principal_id,
                    expected_user_id=seed["user_id"])
                bound_after, user_after = after.authority_bound, after.id
            except ExternalPrincipalRevokedError:
                revoked, bound_after, user_after = True, False, None
            observed = {"change": change, "bound_before": before.authority_bound, "bound_after": bound_after,
                "user_returned_after": user_after is not None, "typed_revocation": revoked,
                "cached_principal_status": cached.status, "cached_binding_present": cached.linked_user_id is not None}
            (tmp_path / "channel-actor-current-binding.json").write_text(json.dumps(observed, indent=2))
            assert not bound_after and user_after is None, observed
            assert revoked is (change == "revoke"), observed


@pytest.mark.parametrize("change", ["pause", "channel_application", "identity_application"])
async def test_cached_feishu_actor_rechecks_the_current_application_namespace(
    company_messaging, owner_sessionmaker, app_user_sessionmaker, tmp_path, change,  # noqa: F811
):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.models.identity import ExternalIdentity, IdentityProvider
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.channel_ingress_inbox import channel_installation_ref

    seed = company_messaging
    async with owner_sessionmaker() as owner:
        config = TenantChannelConfig(id=uuid4(), tenant_id=seed["tenant_id"], channel_type="feishu",
            app_id="synthetic-current-app", app_secret="synthetic-secret", is_active=True)
        provider = IdentityProvider(id=uuid4(), tenant_id=seed["tenant_id"], provider_type="feishu",
            name="Feishu", is_active=True, config={"app_id": config.app_id})
        owner.add_all([config, provider])
        await owner.flush()
        subject = "ou-" + uuid4().hex
        owner.add(ExternalIdentity(provider_id=provider.id, user_id=seed["user_id"], provider_open_id=subject))
        principal = ExternalPrincipal(id=uuid4(), tenant_id=seed["tenant_id"], provider="feishu",
            tenant_channel_config_id=config.id, installation_ref=channel_installation_ref(config, fallback=""),
            subject_id=subject, display_name="Current member", linked_user_id=seed["user_id"],
            binding_method="feishu_tenant_identity", binding_verified_at=datetime.now(UTC),
            linked_at=datetime.now(UTC), status="active")
        owner.add(principal)
        await owner.commit()
        principal_id, config_id, provider_id = principal.id, config.id, provider.id
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, seed["tenant_id"])
        cached_config = await db.get(TenantChannelConfig, config_id)
        cached_provider = await db.get(IdentityProvider, provider_id)
        before = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=principal_id,
            expected_user_id=seed["user_id"])
        assert before.authority_bound and before.id == seed["user_id"]
        async with owner_sessionmaker() as owner:
            if change == "identity_application":
                current = await owner.get(IdentityProvider, provider_id)
                current.config = {"app_id": "synthetic-replaced-identity-app"}
            else:
                current = await owner.get(TenantChannelConfig, config_id)
                if change == "pause":
                    current.is_active = False
                else:
                    current.app_id = "synthetic-replaced-channel-app"
            await owner.commit()
        after = await load_external_runtime_actor(db, tenant_id=seed["tenant_id"], principal_id=principal_id,
            expected_user_id=seed["user_id"])
        observed = {"change": change, "bound_before": before.authority_bound, "bound_after": after.authority_bound,
            "user_returned_after": after.id is not None, "cached_config_active": cached_config.is_active,
            "cached_provider_application_unchanged": cached_provider.config["app_id"] == "synthetic-current-app"}
        (tmp_path / "channel-actor-current-namespace.json").write_text(json.dumps(observed, indent=2))
        assert not after.authority_bound and after.id is None, observed
