"""Native working-copy framing preserves image evidence for vision models."""

import base64
import json
import uuid

import pytest

from app.tools.handlers.filesystem import fs_read, read_file
from app.tools.runtime import ToolExecutionContext


@pytest.mark.parametrize("reader", [read_file, fs_read])
def test_native_working_copy_receipt_retains_the_complete_image(reader, tmp_path):
    body = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+lmJkAAAAASUVORK5CYII=")
    (tmp_path / "source.png").write_bytes(body)
    context = ToolExecutionContext(agent_id=uuid.uuid4(), user_id=uuid.uuid4(),
                                   tenant_id=None, workspace=tmp_path)
    result = reader(tmp_path, {"path": "source.png"}, execution_context=context)
    receipt = json.loads(str(result))
    assert receipt["ok"] is True and "source.png" in receipt["content"]
    assert len(result.blocks) == 1 and result.blocks[0].type == "image"
    assert result.blocks[0].media_type == "image/png"
    assert base64.b64decode(result.blocks[0].data) == body
