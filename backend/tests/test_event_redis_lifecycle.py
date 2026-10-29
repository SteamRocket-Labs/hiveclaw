"""The test harness must release cached Redis before each function loop closes."""

import asyncio
import shutil
import socket
import subprocess

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import events


@pytest.fixture
async def event_redis_origin(tmp_path, monkeypatch):
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        port = endpoint.getsockname()[1]
    executable = shutil.which("redis-server")
    assert executable is not None
    url = f"redis://127.0.0.1:{port}/0"
    with (tmp_path / "redis.log").open("wb") as output:
        server = subprocess.Popen(
            [
                executable,
                "--bind",
                "127.0.0.1",
                "--port",
                str(port),
                "--save",
                "",
                "--appendonly",
                "no",
                "--dir",
                str(tmp_path),
            ],
            stdout=output,
            stderr=subprocess.STDOUT,
        )
        probe = Redis.from_url(url, decode_responses=True)
        try:
            async with asyncio.timeout(5):
                while True:
                    try:
                        if await probe.ping():
                            break
                    except (OSError, RedisConnectionError):
                        pass
                    assert server.poll() is None
                    await asyncio.sleep(0.02)
            monkeypatch.setattr(events.settings, "REDIS_URL", url)
            yield url
        finally:
            await probe.aclose()
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


@pytest.mark.parametrize("loop_name", ["initial", "next"])
async def test_cached_event_redis_is_released_between_function_loops(event_redis_origin, loop_name):
    assert events._redis_client is None, f"A previous {loop_name} loop retained the event Redis client"
    client = await events.get_redis()
    assert str(client.connection_pool.connection_kwargs["port"]) == event_redis_origin.split(":")[-1].split("/")[0]
    assert await client.ping() is True
    # Leave the process-global cache to the harness, like ordinary event producers.
