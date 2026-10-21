"""Private install profiles and native equivalents of the shipped service commands."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time

from dotenv import dotenv_values
import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
import uuid
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("configure_self_hosted", ROOT / "scripts/configure_self_hosted.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture(scope="module")
def pg_container():
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as container:
        yield container


def test_private_profile_is_unique_and_cannot_replace_existing_keys(tmp_path):
    first = installer.configure(tmp_path / "one", public_origin="https://EXAMPLE.test:443/", port=3008)
    other = installer.configure(tmp_path / "two", public_origin="http://localhost:3008", port=3008)
    before = first.read_bytes()
    values, second = dotenv_values(first), dotenv_values(other)
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert json.loads(values["HIVE_INSTALL_CORS_ORIGINS"]) == ["https://example.test"]
    keys = [key for key in values if key.endswith(("PASSWORD", "SECRET_KEY", "MASTER_KEY"))]
    assert len(keys) == 6 and all(len(values[key]) == 64 and values[key] != second[key] for key in keys)
    assert values["HIVE_INSTALL_ID"] != second["HIVE_INSTALL_ID"]
    with pytest.raises(FileExistsError):
        installer.configure(first.parent, public_origin="http://localhost:3008", port=9999)
    assert first.read_bytes() == before


@pytest.mark.parametrize("origin", ["https://user:secret@example.test", "http://example.test/path", "https://example.test?key=x", "http://exa mple.test"])
def test_invalid_origin_writes_no_profile(tmp_path, origin):
    with pytest.raises(ValueError):
        installer.configure(tmp_path / "invalid", public_origin=origin, port=3008)
    assert not (tmp_path / "invalid").exists()


def test_actual_compose_parser_preserves_private_paths_and_separates_owner_credentials(tmp_path, monkeypatch):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI required, no daemon/container is used")
    source = tmp_path / "source's $literal"
    (source / "frontend").mkdir(parents=True)
    (source / "Dockerfile").write_text("FROM scratch\n")
    (source / "frontend/Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr(installer, "ROOT", source)
    profile = installer.configure(tmp_path / "owner's $profile", public_origin="http://localhost:3008", port=3008)
    values = dotenv_values(profile)
    compose = [shutil.which("docker-compose")] if shutil.which("docker-compose") else ["docker", "compose"]
    compose_env = {**os.environ, "SECRET_KEY": "AMBIENT_SECRET_MUST_NOT_OVERRIDE_PROFILE"}
    plugin_directory = Path(compose[0]).resolve().parent
    if (plugin_directory / "docker-buildx").is_file():
        config = tmp_path / "docker-config"
        config.mkdir()
        (config / "config.json").write_text(json.dumps({"cliPluginsExtraDirs": [str(plugin_directory)]}))
        compose_env["DOCKER_CONFIG"] = str(config)
    result = subprocess.run([*compose, "--env-file", str(profile), "-f", str(ROOT / "deploy/self-hosted/compose.yml"), "config", "--format", "json"],
        env=compose_env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    # Compose config re-escapes dollars for reloading; Bake consumes the actual build context.
    bake = subprocess.run([*compose, "--env-file", str(profile), "-f", str(ROOT / "deploy/self-hosted/compose.yml"), "build", "--print"],
        env=compose_env, capture_output=True, text=True, timeout=30)
    assert bake.returncode == 0, bake.stderr
    assert json.loads(bake.stdout)["target"]["backend"]["context"] == str(source)
    for name in ("backend", "backend-api"):
        env = services[name]["environment"]
        assert "SCHEMA_DATABASE_URL" not in env and "RLS_APP_PASSWORD" not in env
        assert env["RLS_RUNTIME_ROLE_ENFORCEMENT"] == "strict"
        assert env["SECRET_KEY"] == values["HIVE_INSTALL_SECRET_KEY"]
        assert make_url(env["DATABASE_URL"]).username == "app_rls"
        assert values["HIVE_INSTALL_SCHEMA_PASSWORD"] not in json.dumps(services[name])
        assert services[name]["depends_on"]["prepare"]["condition"] == "service_completed_successfully"
    assert services["backend"]["image"] == services["backend-api"]["image"] == services["prepare"]["image"]
    assert "recovery" not in services
    maintenance = subprocess.run([*compose, "--env-file", str(profile), "-f", str(ROOT / "deploy/self-hosted/compose.yml"),
        "--profile", "maintenance", "config", "--format", "json"], env=compose_env, capture_output=True, text=True, timeout=30)
    assert maintenance.returncode == 0, maintenance.stderr
    recovery = json.loads(maintenance.stdout)["services"]["recovery"]
    assert recovery["profiles"] == ["maintenance"]
    assert recovery["environment"]["PGPASSWORD"] == values["HIVE_INSTALL_SCHEMA_PASSWORD"]
    assert "SECRETS_MASTER_KEY" not in recovery["environment"]
    assert (ROOT / "deploy/self-hosted/recovery.Dockerfile").read_text().splitlines()[1] == "FROM " + services["postgres"]["image"]
    assert services["backend"]["environment"]["HIVE_PROCESS_ROLE"] == "runtime"
    assert services["backend-api"]["environment"]["HIVE_PROCESS_ROLE"] == "api"
    assert all(not services[name].get("ports") for name in ("postgres", "redis", "backend", "backend-api"))
    assert services["frontend"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert all("docker.sock" not in json.dumps(value) for value in services.values())


@pytest.mark.parametrize("schema_url", ["not-a-url-SYNTHETIC_SECRET", "postgresql+asyncpg://owner:secret@127.0.0.1:1/other"])
def test_prepare_rejects_invalid_configuration_before_any_database_effect(tmp_path, schema_url):
    env = {**os.environ, "SCHEMA_DATABASE_URL": schema_url,
        "DATABASE_URL": "postgresql+asyncpg://app_rls:synthetic@127.0.0.1:1/target",
        "RLS_APP_PASSWORD": "synthetic", "AGENT_DATA_DIR": str(tmp_path / "workspace")}
    result = subprocess.run([sys.executable, "-B", "-m", "app.scripts.prepare_self_hosted"], cwd=ROOT / "backend",
        env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert json.loads(result.stdout)["code"] == "installation_configuration_invalid"
    assert "SYNTHETIC_SECRET" not in result.stdout + result.stderr
    assert not (tmp_path / "workspace").exists()


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def wait_ready(origin, process, log):
    async with httpx.AsyncClient(trust_env=False) as client:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            assert process.returncode is None, log.read_text()[-5000:]
            try:
                result = await client.get(origin + "/api/health", timeout=5)
                if result.status_code == 200:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.1)
    pytest.fail(log.read_text()[-5000:])


async def stop(process, log_path=None):
    was_running = process.returncode is None
    if was_running:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 15)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    if was_running:
        if log_path is None:
            assert process.returncode == 0
        else:
            assert process.returncode in (0, -signal.SIGTERM)
            assert "[shutdown] application lifespan cleanup completed" in log_path.read_text()


async def test_native_prepare_real_authentication_and_worker_without_owner_credentials(tmp_path, pg_container):
    from redis import Redis
    from redis.exceptions import RedisError, AuthenticationError

    redis_binary = shutil.which("redis-server")
    if not redis_binary:
        pytest.skip("Native redis-server required for this process journey")
    profile = installer.configure(tmp_path / "installation", public_origin="http://127.0.0.1:3008", port=3008)
    values = dotenv_values(profile)
    owner = make_url(pg_container.get_connection_url()).set(drivername="postgresql+asyncpg")
    runtime = owner.set(username="app_rls", password=values["HIVE_INSTALL_RUNTIME_PASSWORD"])
    inherited = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE") if key in os.environ}
    redis_port = unused_port()
    common = {**inherited, "DATABASE_URL": runtime.render_as_string(hide_password=False),
        "REDIS_URL": f"redis://:{values['HIVE_INSTALL_REDIS_PASSWORD']}@127.0.0.1:{redis_port}/0",
        "SECRET_KEY": values["HIVE_INSTALL_SECRET_KEY"], "JWT_SECRET_KEY": values["HIVE_INSTALL_JWT_SECRET_KEY"],
        "SECRETS_MASTER_KEY": values["HIVE_INSTALL_SECRETS_MASTER_KEY"], "DEBUG": "false", "RLS_RUNTIME_ROLE_ENFORCEMENT": "strict",
        "CORS_ORIGINS": values["HIVE_INSTALL_CORS_ORIGINS"], "AGENT_DATA_DIR": str(tmp_path / "agents"),
        "AGENT_TEMPLATE_DIR": str(ROOT / "backend/agent_template"), "PYTHONUNBUFFERED": "1"}
    preparation = subprocess.run([sys.executable, "-B", "-m", "app.scripts.prepare_self_hosted"], cwd=ROOT / "backend",
        env={**common, "SCHEMA_DATABASE_URL": owner.render_as_string(hide_password=False), "RLS_APP_PASSWORD": runtime.password},
        capture_output=True, text=True, timeout=180)
    (tmp_path / "prepare.log").write_text(preparation.stdout + preparation.stderr)
    assert preparation.returncode == 0, preparation.stdout + preparation.stderr
    assert json.loads(preparation.stdout.splitlines()[-1]) == {"status": "prepared", "runtime_verified": False}
    engine = create_async_engine(owner)
    try:
        async with engine.connect() as db:
            role = (await db.execute(text("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname='app_rls'"))).one()
            assert role == (False, False)
    finally:
        await engine.dispose()
    redis_conf = tmp_path / "redis.conf"
    redis_conf.write_text(f"bind 127.0.0.1\nport {redis_port}\nrequirepass {values['HIVE_INSTALL_REDIS_PASSWORD']}\nsave \"\"\ndir {tmp_path}\n")
    redis_conf.chmod(0o600)
    processes = []
    try:
        with (tmp_path / "redis.log").open("w") as log:
            redis_process = await asyncio.create_subprocess_exec(redis_binary, str(redis_conf), stdout=log, stderr=log)
        processes.append((redis_process, None))
        redis_ready = False
        for _ in range(100):
            try:
                with Redis(host="127.0.0.1", port=redis_port, password=values["HIVE_INSTALL_REDIS_PASSWORD"]) as redis:
                    if redis.ping():
                        redis_ready = True
                        break
            except RedisError:
                await asyncio.sleep(0.05)
        assert redis_ready
        with Redis(host="127.0.0.1", port=redis_port, password="incorrect") as client:
            with pytest.raises(AuthenticationError):
                client.ping()
        assert {"SCHEMA_DATABASE_URL", "RLS_APP_PASSWORD", "M1E_PG_PASSWORD"}.isdisjoint(common)
        origins = {}
        for role in ("runtime", "api"):
            port = unused_port()
            path = tmp_path / f"{role}.log"
            with path.open("w") as log:
                process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--no-access-log",
                    cwd=ROOT / "backend", env={**common, "HIVE_PROCESS_ROLE": role}, stdout=log, stderr=log)
            processes.append((process, path))
            origins[role] = f"http://127.0.0.1:{port}"
            await wait_ready(origins[role], process, path)
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            engine = create_async_engine(owner)
            try:
                async with engine.begin() as blocker:
                    await blocker.execute(text("LOCK TABLE users IN SHARE MODE"))
                    requests = [asyncio.create_task(client.post(origins["api"] + "/api/auth/register",
                        json={"username": name, "email": f"{name}@installation.example.com", "password": "SyntheticInstallPass123!"}))
                        for name in ("install-owner", "install-colleague")]
                    for _ in range(100):
                        async with engine.connect() as control:
                            waiting = await control.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND usename='app_rls' AND wait_event_type='Lock'"))
                        if waiting >= 2:
                            break
                        await asyncio.sleep(0.05)
                    assert waiting >= 2
                replies = await asyncio.gather(*requests)
                assert all(reply.status_code == 201 for reply in replies), [reply.text for reply in replies]
                registered_users = [reply.json() for reply in replies]
                assert sorted(row["user"]["role"] for row in registered_users) == ["member", "platform_admin"]
                registered = next(row for row in registered_users if row["user"]["role"] == "platform_admin")
            finally:
                await engine.dispose()
            tenant = registered["user"]["tenant_id"]
            headers = {"Authorization": "Bearer " + registered["access_token"], "X-Tenant-Id": tenant}
            model = await client.post(origins["runtime"] + "/api/enterprise/llm-models", headers=headers,
                json={"provider": "openai", "model": "synthetic-installation", "label": "Synthetic configured model", "api_key": "SYNTHETIC_UNUSED_KEY", "base_url": "http://127.0.0.1:1/v1"})
            assert model.status_code == 201, model.text
            agent = await client.post(origins["api"] + "/api/agents/", headers=headers,
                json={"name": "Installed analyst", "role_description": "Review authorized inputs", "primary_model_id": model.json()["id"]})
            assert agent.status_code == 201, agent.text
            url = origins["runtime"] + f"/api/agents/{agent.json()['id']}/readiness-checks"
            command = {"request_id": str(uuid.uuid4())}
            created = await client.post(url, headers=headers, json=command)
            assert created.status_code == 202, created.text
            for _ in range(300):
                checked = await client.get(url + "/" + created.json()["id"], headers=headers)
                assert checked.status_code == 200, checked.text
                if checked.json()["execution_status"] == "completed":
                    break
                await asyncio.sleep(0.1)
            checks = {row["key"]: row["status"] for row in checked.json()["checks"]}
            assert all(checks[key] == "passed" for key in ("identity", "worker", "storage", "model_configuration")), checked.text
            assert checks["provider_inference"] == checks["capability_effects"] == "unverified"
            replay = await client.post(url, headers=headers, json=command)
            assert replay.json()["id"] == created.json()["id"]
            login = await client.post(origins["api"] + "/api/auth/login", json={"username": registered["user"]["username"], "password": "SyntheticInstallPass123!"})
            assert login.status_code == 200, login.text
            status = await client.get(origins["runtime"] + "/api/enterprise/onboarding-status", headers=headers)
            assert status.status_code == 200 and status.json()["configuration_only"] is True
            (tmp_path / "native-installation-receipt.json").write_text(json.dumps({"roles": ["api", "runtime"], "tenant_id": tenant,
                "agent_id": agent.json()["id"], "check_id": created.json()["id"], "checks": checks, "registration": 201, "login": 200,
                "runtime_has_owner_credentials": False, "initial_registration_roles": sorted(row["user"]["role"] for row in registered_users), "provider_inference": "unverified"}, indent=2))
        assert "schema preparation failed" not in (tmp_path / "runtime.log").read_text()
    finally:
        failures = []
        for process, log_path in reversed(processes):
            try:
                await stop(process, log_path)
            except AssertionError as exc:
                failures.append(str(exc))
        assert not failures, failures
