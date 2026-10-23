"""Native CDP framing with a protocol child; never starts a browser."""

import asyncio
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from app.services.browser_pipe import BrowserLaunchSpec, BrowserPipe, BrowserPipeError

CHILD = r"""
import json,os,time
reader=os.fdopen(3,'rb',buffering=0); writer=os.fdopen(4,'wb',buffering=0); buffer=b''; held=None
while True:
    chunk=reader.read(65536)
    if not chunk:break
    buffer+=chunk
    while b'\0' in buffer:
        line,buffer=buffer.split(b'\0',1); message=json.loads(line); method=message['method']
        if method=='hold':held=message;continue
        if method=='mutate':
            target=message['params']['path'];previous=open(target).read() if os.path.exists(target) else ''
            with open(target+'.ready','w') as effect:effect.write(previous+'effect happened')
            os.replace(target+'.ready',target)
            permit=message['params'].get('ack_permit')
            if permit:
                while not os.path.exists(permit):time.sleep(.005)
            else:time.sleep(.2)
        response={'id':message['id'],'result':{'method':method,'environment':dict(os.environ),'pid':os.getpid()}}
        writer.write(json.dumps(response).encode()+b'\0')
        if method=='second' and held:
            writer.write(json.dumps({'id':held['id'],'result':{'method':'hold'}}).encode()+b'\0');held=None
        if method=='Browser.close':raise SystemExit(0)
"""


def launch(tmp_path):
    return BrowserLaunchSpec(
        executable=Path(sys.executable).resolve(),
        arguments=("-B", "-c", CHILD),
        environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
    )


async def test_native_pipe_keeps_backend_secrets_out_of_the_child(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_BROWSER_TEST_SECRET", "synthetic-control-secret")
    async with await BrowserPipe.start(launch(tmp_path)) as pipe:
        result = await pipe.call("inspect", {})
        assert result["method"] == "inspect"
        assert "HIVE_BROWSER_TEST_SECRET" not in result["environment"]
        assert "synthetic-control-secret" not in json.dumps(result)
        pid = result["pid"]
    assert pipe.closed is True
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_native_pipe_correlates_out_of_order_responses(tmp_path):
    async with await BrowserPipe.start(launch(tmp_path)) as pipe:
        first = asyncio.create_task(pipe.call("hold", {}))
        await asyncio.sleep(0.01)
        second = asyncio.create_task(pipe.call("second", {}))
        assert (await second)["method"] == "second"
        assert (await first)["method"] == "hold"


async def test_timeout_after_a_real_effect_remains_unknown_and_is_not_retried(tmp_path):
    target = tmp_path / "effect.txt"
    async with await BrowserPipe.start(launch(tmp_path)) as pipe:
        with pytest.raises(BrowserPipeError) as error:
            await pipe.call("mutate", {"path": str(target)}, timeout=0.05)
        assert error.value.effect == "unknown"
        assert error.value.code == "browser_response_timeout"
        await asyncio.sleep(0.25)
        assert target.read_text() == "effect happened"
        assert pipe.late_results[error.value.request_id]["result"]["method"] == "mutate"
        assert (await pipe.call("inspect", {}))["method"] == "inspect"


async def test_missing_program_is_typed_and_does_not_start_a_resource(tmp_path):
    spec = BrowserLaunchSpec(executable=tmp_path / "missing", arguments=(), environment={}, cwd=tmp_path)
    with pytest.raises(BrowserPipeError) as error:
        await BrowserPipe.start(spec)
    assert error.value.effect == "not_started"
    assert error.value.code == "browser_executable_unavailable"


async def test_cancelled_effect_is_not_replayed_and_keeps_the_late_ack(tmp_path):
    target = tmp_path / "cancelled-effect.txt"
    permit = tmp_path / "release-ack"
    async with await BrowserPipe.start(launch(tmp_path)) as pipe:
        pending = asyncio.create_task(pipe.call("mutate", {"path": str(target), "ack_permit": str(permit)}))
        try:
            for _ in range(200):
                if target.exists():
                    break
                await asyncio.sleep(0.005)
            assert target.read_text() == "effect happened"
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            permit.write_text("release")
            async with asyncio.timeout(2):
                while not pipe.late_results:
                    await asyncio.sleep(0.005)
            assert len(pipe.late_results) == 1
            assert next(iter(pipe.late_results.values()))["result"]["method"] == "mutate"
            assert target.read_text() == "effect happened"
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_close_reaps_a_descendant_that_ignores_termination(tmp_path):
    script = r"""
import json,os,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-B','-c',
    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'],
    stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
reader=os.fdopen(3,'rb',buffering=0);writer=os.fdopen(4,'wb',buffering=0)
while True:
    data=reader.read(65536)
    if not data:break
    for line in data.split(b'\0'):
        if line:
            message=json.loads(line)
            writer.write(json.dumps({'id':message['id'],'result':{'descendant':child.pid}}).encode()+b'\0')
"""
    async with await BrowserPipe.start(replace(launch(tmp_path), arguments=("-B", "-c", script))) as pipe:
        descendant = (await pipe.call("inspect", {}))["descendant"]
    for _ in range(200):
        try:
            os.kill(descendant, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(descendant, 0)


async def test_large_response_is_not_truncated_on_child_exit(tmp_path):
    script = r"""
import json,os
reader=os.fdopen(3,'rb',buffering=0);writer=os.fdopen(4,'wb',buffering=0)
message=json.loads(reader.read(65536).split(b'\0')[0])
body=json.dumps({'id':message['id'],'result':{'data':'x'*1048576}}).encode()+b'\0'
while body:
    written=writer.write(body);body=body[written:]
"""
    async with await BrowserPipe.start(replace(launch(tmp_path), arguments=("-B", "-c", script))) as pipe:
        response = await pipe.call("inspect", {})
        assert response["data"] == "x" * 1048576
