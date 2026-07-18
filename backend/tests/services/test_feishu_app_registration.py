from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def ping(self) -> bool:
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None, nx: bool = False) -> bool | None:
        del ex
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)

    async def eval(self, _script: str, numkeys: int, *args: str) -> int:
        keys = args[:numkeys]
        argv = args[numkeys:]
        if numkeys == 2:
            active_key, session_key = keys
            session_id, expected_json, replacement_json, _ttl = argv
            if self.values.get(active_key) != session_id:
                return 0
            if self.values.get(session_key) != expected_json:
                return 0
            self.values[session_key] = replacement_json
            return 1
        if numkeys == 1:
            (active_key,) = keys
            (session_id,) = argv
            if self.values.get(active_key) != session_id:
                return 0
            del self.values[active_key]
            return 1
        raise AssertionError(f"Unexpected eval shape: numkeys={numkeys}")


async def _wait_for_status(manager, session_id, expected: str, *, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        state = await manager.get_session(session_id)
        if state.status == expected:
            return state
        await asyncio.sleep(0)
    raise AssertionError(f"Registration session did not reach {expected}")


@pytest.mark.parametrize(
    ("region", "expected_domain", "expected_lark_domain"),
    [
        ("feishu_cn", "https://accounts.feishu.cn", "https://accounts.larksuite.com"),
        ("lark_global", "https://accounts.larksuite.com", "https://accounts.larksuite.com"),
    ],
)
@pytest.mark.asyncio
async def test_agent_channel_registration_uses_official_qr_flow_for_feishu_and_lark(
    region: str,
    expected_domain: str,
    expected_lark_domain: str,
) -> None:
    from app.services.feishu_app_registration import FeishuAppRegistrationManager

    redis = _FakeRedis()
    captured_runner_kwargs: dict[str, object] = {}
    persisted: list[tuple[object, dict[str, object], str]] = []

    async def fake_runner(**kwargs):
        captured_runner_kwargs.update(kwargs)
        kwargs["on_qr_code"](
            {
                "url": f"{expected_domain}/page/launcher?ticket=registration-ticket",
                "expire_in": 600,
            }
        )
        kwargs["on_status_change"]({"status": "polling"})
        return {
            "client_id": "cli_registered",
            "client_secret": "never-store-in-redis",
            "user_info": {"tenant_brand": "lark" if region == "lark_global" else "feishu"},
        }

    async def fake_persister(context, credentials, resolved_region):
        persisted.append((context, credentials, resolved_region))

    manager = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        registration_runner=fake_runner,
        credential_persister=fake_persister,
    )
    state = await manager.start_registration(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        actor_user_id=uuid4(),
        platform_region=region,
        agent_name="Research Agent",
    )

    final_state = await _wait_for_status(manager, state.session_id, "connecting")

    assert captured_runner_kwargs["domain"] == expected_domain
    assert captured_runner_kwargs["lark_domain"] == expected_lark_domain
    assert captured_runner_kwargs["source"] == "hive-agent-detail"
    assert "im:message:send_as_bot" in captured_runner_kwargs["addons"]["scopes"]["tenant"]
    assert "im.message.receive_v1" in captured_runner_kwargs["addons"]["events"]["items"]["tenant"]
    assert persisted and persisted[0][2] == region
    assert final_state.resolved_platform_region == region
    assert final_state.verification_url is None

    redis_payload = json.dumps(redis.values, ensure_ascii=False)
    assert "never-store-in-redis" not in redis_payload
    assert "client_secret" not in redis_payload


@pytest.mark.asyncio
async def test_cancelled_registration_cannot_persist_when_original_worker_finishes_late() -> None:
    from app.services.feishu_app_registration import FeishuAppRegistrationManager

    redis = _FakeRedis()
    release_runner = asyncio.Event()
    persisted: list[object] = []

    async def slow_runner(**kwargs):
        kwargs["on_qr_code"](
            {
                "url": "https://accounts.feishu.cn/page/launcher?ticket=cancel-me",
                "expire_in": 600,
            }
        )
        await release_runner.wait()
        return {
            "client_id": "cli_stale",
            "client_secret": "stale-secret",
            "user_info": {"tenant_brand": "feishu"},
        }

    async def fake_persister(*args):
        persisted.append(args)

    first_worker = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        registration_runner=slow_runner,
        credential_persister=fake_persister,
    )
    cancelling_worker = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        registration_runner=slow_runner,
        credential_persister=fake_persister,
    )
    tenant_id = uuid4()
    agent_id = uuid4()
    actor_user_id = uuid4()
    started = await first_worker.start_registration(
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor_user_id=actor_user_id,
        platform_region="feishu_cn",
        agent_name="Agent",
    )
    await _wait_for_status(first_worker, started.session_id, "scanning")

    cancelled = await cancelling_worker.cancel_registration(
        started.session_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        actor_user_id=actor_user_id,
    )
    release_runner.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert cancelled.status == "cancelled"
    assert persisted == []
    assert (await first_worker.get_session(started.session_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_registration_rejects_qr_urls_outside_official_accounts_domains() -> None:
    from app.services.feishu_app_registration import FeishuAppRegistrationManager

    redis = _FakeRedis()
    persisted: list[object] = []

    async def malicious_runner(**kwargs):
        kwargs["on_qr_code"]({"url": "https://attacker.example/steal", "expire_in": 600})
        await asyncio.sleep(0)
        return {"client_id": "cli_x", "client_secret": "secret"}

    async def fake_persister(*args):
        persisted.append(args)

    manager = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        registration_runner=malicious_runner,
        credential_persister=fake_persister,
    )
    started = await manager.start_registration(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        actor_user_id=uuid4(),
        platform_region="feishu_cn",
        agent_name="Agent",
    )

    failed = await _wait_for_status(manager, started.session_id, "failed")

    assert failed.error_code == "invalid_verification_url"
    assert failed.verification_url is None
    assert persisted == []


@pytest.mark.asyncio
async def test_credential_persistence_rechecks_manage_access_audits_and_starts_ws_after_commit(
    monkeypatch,
) -> None:
    from app.services.feishu_app_registration import (
        FeishuRegistrationContext,
        _persist_registered_credentials,
    )

    tenant_id = uuid4()
    agent_id = uuid4()
    actor_user_id = uuid4()
    actor = SimpleNamespace(id=actor_user_id, tenant_id=tenant_id, role="member")
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, name="Agent")
    existing_config = SimpleNamespace(
        app_id="old-app",
        app_secret="old-secret",
        encrypt_key="old-encrypt-key",
        verification_token="old-token",
        extra_config={"connection_mode": "webhook", "last_error": "old"},
        is_configured=True,
        is_connected=True,
    )
    committed = False
    audit_calls: list[dict[str, object]] = []
    ws_calls: list[dict[str, object]] = []

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self):
            self.results = iter((Result(actor), Result(existing_config)))

        async def execute(self, _statement):
            return next(self.results)

        async def flush(self):
            return None

        def add(self, _value):
            raise AssertionError("the existing ChannelConfig should be updated")

    db = DB()

    @asynccontextmanager
    async def fake_tenant_session(*args, **kwargs):
        nonlocal committed
        assert args == (tenant_id,)
        assert kwargs["require_tenant"] is True
        yield db
        committed = True

    async def fake_require_manage(received_db, received_actor, received_agent_id):
        assert received_db is db
        assert received_actor is actor
        assert received_agent_id == agent_id
        return agent

    async def fake_write_audit_event(_db, **kwargs):
        audit_calls.append(kwargs)

    async def fake_start_client(received_agent_id, app_id, app_secret, **kwargs):
        assert committed is True
        ws_calls.append(
            {
                "agent_id": received_agent_id,
                "app_id": app_id,
                "app_secret": app_secret,
                **kwargs,
            }
        )

    monkeypatch.setattr("app.database.tenant_scoped_session", fake_tenant_session)
    monkeypatch.setattr("app.core.permissions.require_agent_manage_access", fake_require_manage)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)
    monkeypatch.setattr("app.services.feishu_ws.feishu_ws_manager.start_client", fake_start_client)

    context = FeishuRegistrationContext(
        session_id="registration-session",
        tenant_id=str(tenant_id),
        agent_id=str(agent_id),
        actor_user_id=str(actor_user_id),
        requested_platform_region="lark_global",
        agent_name="Agent",
    )
    await _persist_registered_credentials(
        context,
        {"client_id": "cli_new", "client_secret": "new-secret"},
        "lark_global",
    )

    assert existing_config.app_id == "cli_new"
    assert existing_config.app_secret == "new-secret"
    assert existing_config.encrypt_key is None
    assert existing_config.verification_token is None
    assert existing_config.is_configured is True
    assert existing_config.is_connected is False
    assert existing_config.extra_config["connection_mode"] == "websocket"
    assert existing_config.extra_config["platform_region"] == "lark_global"
    assert existing_config.extra_config["registration_session_id"] == "registration-session"
    assert "new-secret" not in json.dumps(audit_calls, default=str)
    assert audit_calls[0]["event_type"] == "channel.feishu_qr_registered"
    assert ws_calls == [
        {
            "agent_id": agent_id,
            "app_id": "cli_new",
            "app_secret": "new-secret",
            "extra_config": existing_config.extra_config,
        }
    ]


@pytest.mark.asyncio
async def test_pinned_lark_sdk_accepts_hive_registration_preset_without_network(monkeypatch) -> None:
    from lark_oapi.scene import registration as sdk_registration

    from app.services.feishu_app_registration import FeishuAppRegistrationManager

    redis = _FakeRedis()
    persisted: list[tuple[dict[str, object], str]] = []
    requests: list[tuple[str, dict[str, object]]] = []

    async def fake_post(flow, data):
        requests.append((flow._base_url, data))
        if data["action"] == "init":
            return {"supported_auth_methods": ["client_secret"]}
        if data["action"] == "begin":
            return {
                "device_code": "device-code",
                "verification_uri_complete": "https://accounts.feishu.cn/page/launcher?ticket=sdk",
                "interval": 1,
                "expires_in": 600,
            }
        return {
            "client_id": "cli_from_sdk",
            "client_secret": "sdk-secret",
            "user_info": {"tenant_brand": "feishu"},
        }

    async def fake_persister(_context, credentials, resolved_region):
        persisted.append((dict(credentials), resolved_region))

    monkeypatch.setattr(sdk_registration._AsyncFlow, "_post", fake_post)
    manager = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        credential_persister=fake_persister,
    )
    started = await manager.start_registration(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        actor_user_id=uuid4(),
        platform_region="feishu_cn",
        agent_name="SDK Agent",
    )

    completed = await _wait_for_status(manager, started.session_id, "connecting")

    assert [request[1]["action"] for request in requests] == ["init", "begin", "poll"]
    assert all(request[0] == "https://accounts.feishu.cn" for request in requests)
    assert completed.verification_url is None
    assert persisted == [
        (
            {
                "client_id": "cli_from_sdk",
                "client_secret": "sdk-secret",
                "user_info": {"tenant_brand": "feishu"},
            },
            "feishu_cn",
        )
    ]


@pytest.mark.parametrize(
    ("provider_error", "expected_status", "expected_code"),
    [
        ("denied", "denied", "registration_denied"),
        ("expired", "expired", "registration_expired"),
    ],
)
@pytest.mark.asyncio
async def test_official_provider_denial_and_expiry_are_typed_terminal_states(
    provider_error: str,
    expected_status: str,
    expected_code: str,
) -> None:
    from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError

    from app.services.feishu_app_registration import FeishuAppRegistrationManager

    redis = _FakeRedis()
    persisted: list[object] = []

    async def failing_runner(**_kwargs):
        if provider_error == "denied":
            raise AppAccessDeniedError("access_denied", "cancelled")
        raise AppExpiredError("expired_token", "expired")

    async def fake_persister(*args):
        persisted.append(args)

    manager = FeishuAppRegistrationManager(
        redis_getter=lambda: redis,
        registration_runner=failing_runner,
        credential_persister=fake_persister,
    )
    started = await manager.start_registration(
        tenant_id=uuid4(),
        agent_id=uuid4(),
        actor_user_id=uuid4(),
        platform_region="feishu_cn",
        agent_name="Agent",
    )

    terminal = await _wait_for_status(manager, started.session_id, expected_status)

    assert terminal.error_code == expected_code
    assert persisted == []
