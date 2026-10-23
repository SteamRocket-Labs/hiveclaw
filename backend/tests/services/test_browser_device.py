"""Protocol-child checks; these do not start Chrome or operate a browser."""

import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.browser_device import BrowserPage
from app.services.browser_pipe import BrowserLaunchSpec, BrowserPipe, BrowserPipeError


def test_profile_storage_is_private_and_excludes_canonical_files_and_backend_secrets(tmp_path, monkeypatch):
    import app.services.browser_device as device

    settings = SimpleNamespace(
        BROWSER_DEVICE_PROFILE="trusted_host",
        BROWSER_DEVICE_EXECUTABLE=sys.executable,
        BROWSER_PRIVATE_PROFILE_DIR=str(tmp_path / "private-browser"),
        AGENT_DATA_DIR=str(tmp_path / "canonical-agents"),
    )
    monkeypatch.setattr(device, "get_settings", lambda: settings)
    monkeypatch.setenv("BROWSER_TEST_PLATFORM_SECRET", "synthetic-platform-secret")
    spec = device.browser_device_spec(uuid4(), uuid4(), uuid4())
    assert not spec.cwd.is_relative_to(Path(settings.AGENT_DATA_DIR))
    assert (Path(settings.BROWSER_PRIVATE_PROFILE_DIR).stat().st_mode & 0o077) == 0
    assert "BROWSER_TEST_PLATFORM_SECRET" not in spec.environment
    assert "synthetic-platform-secret" not in str(spec.environment)
    settings.BROWSER_PRIVATE_PROFILE_DIR = settings.AGENT_DATA_DIR
    with pytest.raises(BrowserPipeError, match="browser_private_storage_invalid"):
        device.browser_device_spec(uuid4(), uuid4(), uuid4())


async def test_acknowledged_press_followed_by_lost_release_is_an_unknown_whole_action(tmp_path):
    script = r"""
import json,os,sys
reader=os.fdopen(3,'rb',buffering=0);writer=os.fdopen(4,'wb',buffering=0)
message=json.loads(reader.read(65536).split(b'\0')[0])
assert message['method']=='Input.dispatchMouseEvent' and message['params']['type']=='mousePressed'
open(sys.argv[1],'w').write('press happened once')
writer.write(json.dumps({'id':message['id'],'result':{}}).encode()+b'\0')
"""
    marker = tmp_path / "pressed.txt"
    spec = BrowserLaunchSpec(
        Path(sys.executable).resolve(),
        ("-B", "-c", script, str(marker)),
        {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        tmp_path,
    )
    async with await BrowserPipe.start(spec) as pipe:
        page = BrowserPage(pipe)

        async def authorize():
            if marker.exists():
                await pipe._process.wait()
                await pipe._reader_task

        with pytest.raises(BrowserPipeError) as error:
            await page.act("click", {"x": 1, "y": 2}, before_effect=authorize)
        assert error.value.effect == "unknown"
        assert marker.read_text() == "press happened once"
        assert pipe.closed is True
