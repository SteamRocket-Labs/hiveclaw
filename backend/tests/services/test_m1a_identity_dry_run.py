"""Identity dry-run seams that do not need PostgreSQL."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.company_knowledge_identity import (
    company_knowledge_grant_audiences,
    explicit_subject_matches,
)
from app.services.identity_dry_run import identity_source_revision
from app.services.org_sync_service import DirectoryFetchError, OrgSyncService

SECRET = "m1a-synthetic-app-secret"


def _user(user_id: str, **fields: object) -> SimpleNamespace:
    base = {
        "id": user_id,
        "feishu_open_id": None,
        "feishu_union_id": None,
        "feishu_user_id": None,
        "is_active": True,
        "display_name": user_id,
        "email": f"{user_id}@example.test",
        "username": user_id,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def _member(**fields: object) -> SimpleNamespace:
    base = {
        "id": "member",
        "name": "Directory person",
        "email": None,
        "feishu_open_id": None,
        "open_id": None,
        "unionid": None,
        "feishu_user_id": None,
        "external_id": None,
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_explicit_subject_matches_follow_the_grant_matcher():
    ada = _user("ada", feishu_open_id="ou_ada", email="ada@example.test")
    linked = _member(feishu_open_id="ou_ada", email="ada@example.test", name="Someone else")
    found = explicit_subject_matches([ada], linked)
    assert [user.id for user in found["users"]] == ["ada"]
    assert found["kinds_by_user"]["ada"] == ["provider_open_id"]
    audiences = company_knowledge_grant_audiences([ada], [linked])
    assert audiences["visible_unlinked"] == []

    email_only = _member(id="email", name="Ada", email="ada@example.test")
    assert explicit_subject_matches([ada], email_only)["users"] == []
    email_audiences = company_knowledge_grant_audiences([ada], [email_only])
    assert email_audiences["visible_unlinked"][0]["reason"] == "unregistered"

    other = _user("other", feishu_user_id="fu_other", email="other@example.test")
    ambiguous = _member(feishu_open_id="ou_ada", feishu_user_id="fu_other")
    both = explicit_subject_matches([ada, other], ambiguous)
    assert sorted(user.id for user in both["users"]) == ["ada", "other"]
    ambiguous_audiences = company_knowledge_grant_audiences([ada, other], [ambiguous])
    assert ambiguous_audiences["visible_unlinked"][0]["reason"] == "ambiguous_identity"

    shared = "same-id"
    same_user = _user(shared, display_name="Pat", email="pat@example.test")
    same_member = _member(id=shared, name="Pat", email="pat@example.test", feishu_open_id="ou_other")
    assert explicit_subject_matches([same_user], same_member)["users"] == []


def test_source_revision_is_unknown_when_git_cannot_prove_it(monkeypatch):
    monkeypatch.delenv("HIVE_SOURCE_REVISION", raising=False)

    def fail(*_args, **_kwargs):
        raise OSError("git missing")

    monkeypatch.setattr("app.services.identity_dry_run.subprocess.run", fail)
    assert identity_source_revision() == "unknown"

    def garbage(*_args, **_kwargs):
        return SimpleNamespace(stdout="not-a-revision\n")

    monkeypatch.setattr("app.services.identity_dry_run.subprocess.run", garbage)
    assert identity_source_revision() == "unknown"


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class _Client:
    script: list[dict] = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, _url, json):
        assert json["app_secret"] == SECRET
        return _Response({"code": 0, "msg": "ok", "tenant_access_token": "t-token"})

    async def get(self, _url, params, headers):
        assert headers["Authorization"] == "Bearer t-token"
        assert params["page_size"] == "50"
        return _Response(_Client.script.pop(0))


async def _fetch(monkeypatch, script: list[dict], config: dict | None):
    service = OrgSyncService()

    async def configured(_db, _tenant_id):
        return config

    monkeypatch.setattr(service, "_get_feishu_config", configured)
    _Client.script = list(script)
    monkeypatch.setattr("app.services.org_sync_service.httpx.AsyncClient", _Client)
    return service


async def test_readonly_directory_projects_a_complete_snapshot_without_secrets(monkeypatch):
    from loguru import logger

    department = {
        "code": 0,
        "data": {
            "items": [{
                "open_department_id": "od_research",
                "name": "Research",
                "parent_department_id": "0",
                "member_count": 7,
                "extra": "drop-me",
            }],
            "has_more": False,
        },
    }
    member = {
        "code": 0,
        "data": {
            "items": [{
                "open_id": "ou_1",
                "user_id": "fu_1",
                "union_id": "on_1",
                "email": "ada@example.test",
                "name": "Ada",
                "mobile": "15500001111",
            }],
            "has_more": False,
        },
    }
    root_members = {"code": 0, "data": {"items": [], "has_more": False}}
    service = await _fetch(monkeypatch, [department, root_members, member], {"app_id": "cli_m1a", "app_secret": SECRET})
    messages: list[str] = []
    handle = logger.add(lambda message: messages.append(str(message)))
    try:
        snapshot = await service.fetch_pending_directory(None, uuid.uuid4())
    finally:
        logger.remove(handle)
    logged = "\n".join(messages)
    assert SECRET not in logged
    assert "t-token" not in logged
    assert "15500001111" not in logged
    assert snapshot["complete"] is True
    assert snapshot["departments"] == [{
        "open_department_id": "od_research",
        "name": "Research",
        "parent_department_id": "0",
        "member_count": 7,
    }]
    assert snapshot["members"][0]["open_id"] == "ou_1"
    assert "mobile" not in snapshot["members"][0]


async def test_incomplete_directory_page_is_not_a_clean_snapshot(monkeypatch):
    service = await _fetch(
        monkeypatch,
        [{"code": 99, "msg": "nope", "data": {}}],
        {"app_id": "cli_m1a", "app_secret": SECRET},
    )
    with pytest.raises(DirectoryFetchError) as caught:
        await service.fetch_pending_directory(None, uuid.uuid4())
    assert caught.value.code == "incomplete"


async def test_missing_directory_config_does_not_call_the_provider(monkeypatch):
    service = await _fetch(monkeypatch, [], None)

    def explode(*_args, **_kwargs):
        raise AssertionError("provider called")

    monkeypatch.setattr("app.services.org_sync_service.httpx.AsyncClient", explode)
    with pytest.raises(DirectoryFetchError) as caught:
        await service.fetch_pending_directory(None, uuid.uuid4())
    assert caught.value.code == "missing_config"


@pytest.mark.parametrize(
    "items",
    [["malformed"], [{"name": "Missing department subject"}], None],
)
async def test_provider_page_does_not_drop_unassessed_records(monkeypatch, items):
    page = {"code": 0, "data": {"items": items, "has_more": False}}
    empty = {"code": 0, "data": {"items": [], "has_more": False}}
    service = await _fetch(monkeypatch, [page, empty], {"app_id": "synthetic", "app_secret": SECRET})
    with pytest.raises(DirectoryFetchError) as caught:
        await service.fetch_pending_directory(None, uuid.uuid4())
    assert caught.value.code == "incomplete"


async def test_explicit_empty_directory_is_complete(monkeypatch):
    empty = {"code": 0, "data": {"items": [], "has_more": False}}
    service = await _fetch(monkeypatch, [empty, empty], {"app_id": "synthetic", "app_secret": SECRET})
    assert await service.fetch_pending_directory(None, uuid.uuid4()) == {
        "complete": True,
        "departments": [],
        "members": [],
    }


async def test_root_users_are_included_when_child_departments_exist(monkeypatch):
    service = await _fetch(monkeypatch, [], {"app_id": "synthetic", "app_secret": SECRET})
    calls = []

    async def get(self, url, params, headers):
        if url.endswith("/children"):
            items = [{"open_department_id": "od_child", "name": "Child", "parent_department_id": "0"}]
        else:
            calls.append(params["department_id"])
            items = [{"open_id": "ou_root", "name": "Root person"}] if params["department_id"] == "0" else []
        return _Response({"code": 0, "data": {"items": items, "has_more": False}})

    monkeypatch.setattr(_Client, "get", get)
    snapshot = await service.fetch_pending_directory(None, uuid.uuid4())
    assert calls == ["0", "od_child"]
    assert snapshot["complete"] is True
    assert {row["open_id"] for row in snapshot["members"]} == {"ou_root"}


async def test_token_error_response_does_not_log_the_secret(monkeypatch):
    from loguru import logger

    service = await _fetch(monkeypatch, [], {"app_id": "synthetic", "app_secret": SECRET})

    async def rejected(self, _url, json):
        return _Response({"code": 1, "msg": f"rejected credential {json['app_secret']}"})

    monkeypatch.setattr(_Client, "post", rejected)
    messages: list[str] = []
    handle = logger.add(lambda message: messages.append(str(message)))
    try:
        with pytest.raises(DirectoryFetchError) as caught:
            await service.fetch_pending_directory(None, uuid.uuid4())
        assert caught.value.code == "provider_unavailable"
    finally:
        logger.remove(handle)
    assert SECRET not in "\n".join(messages)


@pytest.mark.parametrize(
    ("phase", "expected"),
    [("token", "provider_unavailable"), ("directory", "incomplete")],
)
async def test_untrusted_provider_code_is_not_logged(monkeypatch, phase, expected):
    from loguru import logger

    service = await _fetch(
        monkeypatch,
        [{"code": SECRET, "data": {}}],
        {"app_id": "synthetic", "app_secret": SECRET},
    )
    if phase == "token":
        async def rejected(self, _url, json):
            return _Response({"code": json["app_secret"], "msg": "rejected"})

        monkeypatch.setattr(_Client, "post", rejected)
    messages: list[str] = []
    handle = logger.add(lambda message: messages.append(str(message)))
    try:
        with pytest.raises(DirectoryFetchError) as caught:
            await service.fetch_pending_directory(None, uuid.uuid4())
        assert caught.value.code == expected
    finally:
        logger.remove(handle)
    assert SECRET not in "\n".join(messages)
