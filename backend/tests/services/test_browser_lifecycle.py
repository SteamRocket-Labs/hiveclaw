"""Signed supervisor facts use real pipes and files, never a browser."""

import json
import secrets
from dataclasses import replace
from uuid import uuid4

from app.services.browser_lifecycle import BrowserLifecycleSpec
from app.services.browser_pipe import BrowserPipe
from tests.services.test_browser_pipe import launch


async def test_private_supervisor_receipt_requires_the_current_identity_and_signature(tmp_path):
    identity = BrowserLifecycleSpec(str(uuid4()), 1, uuid4().hex, secrets.token_hex(32), tmp_path / "closed.json")
    assert identity.secret not in repr(identity)
    async with await BrowserPipe.start(replace(launch(tmp_path), lifecycle=identity)) as pipe:
        result = await pipe.call("inspect", {})
        assert identity.secret not in json.dumps(result)
        assert identity.observes_closed() is False
    assert identity.observes_closed() is True
    assert replace(identity, nonce=uuid4().hex).observes_closed() is False
    assert replace(identity, secret=secrets.token_hex(32)).observes_closed() is False
    body = json.loads(identity.receipt_path.read_text())
    body["signature"] = "0" * 64
    identity.receipt_path.write_text(json.dumps(body))
    assert identity.observes_closed() is False
