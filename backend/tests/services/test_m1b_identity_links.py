"""Pure identity-link rules. PostgreSQL checks live in the integration test."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core.permissions import SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE, authorize_loaded_session_access
from app.services.company_knowledge_identity import company_knowledge_grant_audiences
from app.services.identity_links import (
    _Binding,
    _member_reason,
    audiences_with_persistent_links,
    history_reason,
    member_subject_map,
    recheck_stored_link,
)


def _user(user_id: uuid.UUID, **fields: object) -> SimpleNamespace:
    base = {
        "id": user_id,
        "is_active": True,
        "display_name": "Ada",
        "username": "ada",
        "tenant_id": fields.get("tenant_id"),
        "feishu_open_id": None,
        "feishu_union_id": None,
        "feishu_user_id": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _member(member_id: uuid.UUID, **fields: object) -> SimpleNamespace:
    base = {
        "id": member_id,
        "name": "Pat",
        "tenant_id": fields.get("tenant_id"),
        "provider_id": None,
        "feishu_open_id": None,
        "open_id": None,
        "unionid": None,
        "feishu_user_id": None,
        "external_id": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _link(**fields: object) -> SimpleNamespace:
    return SimpleNamespace(
        id=fields.get("id", uuid.uuid4()),
        tenant_id=fields["tenant_id"],
        org_member_id=fields["org_member_id"],
        user_id=fields["user_id"],
        provider_id=fields.get("provider_id"),
        subject_kind=fields.get("subject_kind", "provider_open_id"),
        match_subject=fields.get("match_subject", "ou_stored"),
        external_identity_id=fields.get("external_identity_id"),
        status=fields.get("status", "active"),
    )


def test_subject_map_keeps_one_value_and_refuses_disagreeing_aliases() -> None:
    consistent = member_subject_map(
        _member(uuid.uuid4(), feishu_open_id="ou_1", open_id="ou_1", unionid="on_1", external_id="fu_1")
    )
    assert consistent.ambiguous is False
    assert consistent.values == {
        "provider_open_id": "ou_1",
        "provider_union_id": "on_1",
        "provider_user_id": "fu_1",
    }
    disagree = member_subject_map(_member(uuid.uuid4(), feishu_open_id="ou_a", open_id="ou_b"))
    assert disagree.ambiguous_kinds == ("provider_open_id",)
    assert "provider_open_id" not in disagree.values
    assert member_subject_map(_member(uuid.uuid4())).values == {}


def test_recheck_accepts_only_a_current_active_agreement() -> None:
    tenant_id = uuid.uuid4()
    provider_id = uuid.uuid4()
    user = _user(uuid.uuid4(), tenant_id=tenant_id)
    member = _member(
        uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_id,
        feishu_open_id="ou_1",
        unionid="on_1",
    )
    provider = SimpleNamespace(id=provider_id, tenant_id=tenant_id, is_active=True)
    identity_id = uuid.uuid4()
    bindings = [
        _Binding(identity_id, provider_id, user.id, None, "ou_1", "on_1"),
    ]
    link = _link(
        tenant_id=tenant_id,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider_id,
        match_subject="ou_1",
        external_identity_id=identity_id,
    )
    assert recheck_stored_link(link, member=member, user=user, provider=provider, bindings=bindings) == (True, "active")

    stale = _link(
        tenant_id=tenant_id,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider_id,
        match_subject="ou_old",
        external_identity_id=None,
    )
    assert recheck_stored_link(stale, member=member, user=user, provider=provider, bindings=[])[1] == "stale_subject"
    revoked = _link(
        tenant_id=tenant_id,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider_id,
        status="revoked",
    )
    assert recheck_stored_link(revoked, member=member, user=user, provider=provider, bindings=bindings) == (
        False,
        "revoked",
    )
    inactive = _user(user.id, tenant_id=tenant_id, is_active=False)
    assert recheck_stored_link(link, member=member, user=inactive, provider=provider, bindings=bindings)[1] == (
        "inactive_user"
    )
    foreign = SimpleNamespace(id=provider_id, tenant_id=uuid.uuid4(), is_active=True)
    assert recheck_stored_link(link, member=member, user=user, provider=foreign, bindings=bindings)[1] == (
        "unproven_provider"
    )
    other = _Binding(uuid.uuid4(), provider_id, uuid.uuid4(), None, "ou_1", None)
    assert recheck_stored_link(link, member=member, user=user, provider=provider, bindings=[other])[1] == "conflict"
    missing_pointer = _link(
        tenant_id=tenant_id,
        org_member_id=member.id,
        user_id=user.id,
        provider_id=provider_id,
        external_identity_id=None,
    )
    assert recheck_stored_link(missing_pointer, member=member, user=user, provider=provider, bindings=bindings)[1] == (
        "stale_subject"
    )
    other_member = _member(
        uuid.uuid4(),
        tenant_id=tenant_id,
        provider_id=provider_id,
        feishu_open_id="ou_1",
        unionid="on_1",
    )
    assert recheck_stored_link(link, member=other_member, user=user, provider=provider, bindings=bindings)[1] == (
        "conflict"
    )
    legacy = _member(uuid.uuid4(), tenant_id=tenant_id, feishu_open_id="ou_1", unionid="on_1")
    feishu = SimpleNamespace(id=provider_id, tenant_id=tenant_id, is_active=True, provider_type="feishu")
    oidc = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id, is_active=True, provider_type="oidc")
    legacy_link = _link(
        tenant_id=tenant_id,
        org_member_id=legacy.id,
        user_id=user.id,
        provider_id=feishu.id,
        match_subject="ou_1",
        external_identity_id=None,
    )
    assert recheck_stored_link(legacy_link, member=legacy, user=user, provider=feishu, bindings=[]) == (
        True,
        "active",
    )
    mislabeled = _link(
        tenant_id=tenant_id,
        org_member_id=legacy.id,
        user_id=user.id,
        provider_id=oidc.id,
        match_subject="ou_1",
        external_identity_id=None,
    )
    assert recheck_stored_link(mislabeled, member=legacy, user=user, provider=oidc, bindings=[])[1] == (
        "unproven_provider"
    )
    assigned = _member(uuid.uuid4(), tenant_id=tenant_id, provider_id=oidc.id, feishu_open_id="ou_oidc")
    assigned_link = _link(
        tenant_id=tenant_id,
        org_member_id=assigned.id,
        user_id=user.id,
        provider_id=oidc.id,
        match_subject="ou_oidc",
        external_identity_id=None,
    )
    assert recheck_stored_link(assigned_link, member=assigned, user=user, provider=oidc, bindings=[]) == (
        True,
        "active",
    )
    external_only = _member(uuid.uuid4(), tenant_id=tenant_id, external_id="fu_only")
    unproven_link = _link(
        tenant_id=tenant_id,
        org_member_id=external_only.id,
        user_id=user.id,
        provider_id=feishu.id,
        subject_kind="provider_user_id",
        match_subject="fu_only",
        external_identity_id=None,
    )
    assert recheck_stored_link(unproven_link, member=external_only, user=user, provider=feishu, bindings=[])[1] == (
        "unproven_provider"
    )


def test_history_does_not_fall_back_to_the_legacy_matcher() -> None:
    tenant_id = uuid.uuid4()
    linked_user = _user(uuid.uuid4(), display_name="Ada", feishu_open_id="ou_ada")
    other_user = _user(uuid.uuid4(), display_name="Bea")
    plain = _user(uuid.uuid4(), display_name="Cam", feishu_open_id="ou_cam")
    linked = _member(uuid.uuid4(), name="Ada Person", feishu_open_id="ou_ada")
    untouched = _member(uuid.uuid4(), name="Cam Person", feishu_open_id="ou_cam")
    users = [linked_user, other_user, plain]
    members = [linked, untouched]
    legacy = company_knowledge_grant_audiences(users, members)
    assert linked.id not in {uuid.UUID(item["org_member_id"]) for item in legacy["visible_unlinked"]}

    def recheck(link, _member):
        return link.status == "active", link.status

    active = _link(tenant_id=tenant_id, org_member_id=linked.id, user_id=linked_user.id, status="active")
    resolved = audiences_with_persistent_links(users, members, [active], recheck)
    assert all(item["org_member_id"] != str(linked.id) for item in resolved["visible_unlinked"])
    assert str(untouched.id) not in {item["org_member_id"] for item in resolved["visible_unlinked"]}
    assert {item["user_id"] for item in resolved["grantable_users"]} == {str(user.id) for user in users}

    revoked = _link(tenant_id=tenant_id, org_member_id=linked.id, user_id=linked_user.id, status="revoked")
    hidden = audiences_with_persistent_links(users, members, [revoked], recheck)
    reasons = {item["org_member_id"]: item["reason"] for item in hidden["visible_unlinked"]}
    assert reasons[str(linked.id)] == "revoked"
    assert str(linked_user.id) in {item["user_id"] for item in hidden["grantable_users"]}

    conflict = _link(tenant_id=tenant_id, org_member_id=linked.id, user_id=linked_user.id, status="conflict")
    blocked = audiences_with_persistent_links(users, members, [conflict], recheck)
    assert {item["org_member_id"]: item["reason"] for item in blocked["visible_unlinked"]}[str(linked.id)] == "conflict"

    second = _link(tenant_id=tenant_id, org_member_id=linked.id, user_id=other_user.id, status="active")
    ambiguous = audiences_with_persistent_links(users, members, [active, second], recheck)
    ambiguous_row = next(item for item in ambiguous["visible_unlinked"] if item["org_member_id"] == str(linked.id))
    assert ambiguous_row["reason"] == "ambiguous_identity"
    grantable = {item["user_id"] for item in ambiguous["grantable_users"]}
    assert str(linked_user.id) not in grantable
    assert str(other_user.id) not in grantable
    assert str(plain.id) in grantable


def test_one_account_with_two_subject_kinds_is_not_ambiguous() -> None:
    subjects = member_subject_map(_member(uuid.uuid4(), feishu_open_id="ou_1", unionid="on_1"))
    user_id = uuid.uuid4()
    other_id = uuid.uuid4()
    assert _member_reason(
        subjects,
        [(True, "active", user_id), (True, "active", user_id)],
    ) == (True, "active")
    assert _member_reason(
        subjects,
        [(True, "active", user_id), (True, "active", other_id)],
    ) == (False, "ambiguous_identity")
    assert _member_reason(subjects, [(False, "revoked", user_id)]) == (False, "revoked")


def test_history_reason_priority() -> None:
    assert history_reason(["revoked", "conflict"]) == "conflict"
    assert history_reason(["stale_subject", "inactive_user"]) == "authenticated_user_inactive"
    assert history_reason(["ambiguous_subject"]) == "stale_subject"
    assert history_reason(["provider_namespace"]) == "unproven_provider"
    assert history_reason(["cross_tenant"]) == "cross_tenant"
    assert history_reason(["revoked", "revoked"]) == "revoked"
    assert history_reason([]) == "unregistered"


async def test_nullable_audience_columns_do_not_broaden_session_access() -> None:
    tenant_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant_id)
    member = SimpleNamespace(id=uuid.uuid4(), role="member", tenant_id=tenant_id)
    admin = SimpleNamespace(id=uuid.uuid4(), role="org_admin", tenant_id=tenant_id)
    for audience_kind in (None, "group"):
        session = SimpleNamespace(
            id=uuid.uuid4(),
            user_id=owner_id,
            audience_kind=audience_kind,
            audience_ref=None,
            group_installation_ref=None,
            group_subject_id=None,
        )
        with pytest.raises(HTTPException) as caught:
            await authorize_loaded_session_access(
                None,
                member,
                agent=agent,
                session=session,
                access_level="use",
                action="read",
                audit=False,
            )
        assert caught.value.status_code == 403
        allowed = await authorize_loaded_session_access(
            None,
            admin,
            agent=agent,
            session=session,
            access_level="use",
            action="read",
            audit=False,
        )
        assert allowed == SCOPED_BUSINESS_ADMIN_AUTHORITY_SOURCE
