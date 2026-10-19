"""Native Office production may consume only the declared authorized input copies."""

import base64
import subprocess
from hashlib import sha256
from uuid import uuid4
from zipfile import ZipFile

import pytest

from app.services.office_document_service import OfficeDocumentService
from app.services.workspace_resource_authority import (
    WorkspaceAuthorityScope, WorkspaceExecutionInputs, WorkspaceInputRead, WorkspaceOutputWrite,
)


@pytest.mark.parametrize('declared', [False, True])
def test_native_office_reads_only_declared_authorized_files(tmp_path, monkeypatch, declared):
    from docx import Document
    from app.services.officecli_adapter import OfficeCLIAdapter, OfficeCLIExecutionError

    root = tmp_path / 'agent'
    path = 'workspace/report.docx'
    source = root / path
    source.parent.mkdir(parents=True)
    Document().save(source)
    image_path = 'workspace/authorized-image.png'
    image = root / image_path if declared else tmp_path / 'another-user-private.png'
    image.write_bytes(base64.b64decode(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a7S8AAAAASUVORK5CYII='))
    before = source.read_bytes()
    read = WorkspaceInputRead(path, uuid4(), 1, sha256(before).hexdigest(), 'synthetic-dependencies')
    writer = WorkspaceOutputWrite.capture(root, path)
    image_read = WorkspaceInputRead(image_path, uuid4(), 1, sha256(image.read_bytes()).hexdigest(), 'synthetic-dependencies')
    scope = WorkspaceAuthorityScope(uuid4(), uuid4(), uuid4(), frozenset({path, *([image_path] if declared else [])}),
        False, 'session_owner', input_reads=(read, *([image_read] if declared else [])), output_writes=(writer,),
        execution_inputs=WorkspaceExecutionInputs())
    calls = []
    monkeypatch.setenv('OFFICE_PRODUCER_PRIVATE_TOKEN', 'synthetic-never-inherited')
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[0].endswith('sandbox-exec') or argv[0].endswith('bwrap')
        assert all(key not in kwargs['env'] for key in ('DATABASE_URL', 'OFFICE_PRODUCER_PRIVATE_TOKEN', 'SECRET_KEY'))
        return subprocess.run(argv, **kwargs)
    service = OfficeDocumentService(root, authority_scope=scope, adapter=OfficeCLIAdapter(runner=run))
    operations = [{'command': 'add', 'path': '/body', 'type': 'picture',
        'props': {'src': image_path if declared else str(image)}}]
    if declared:
        service.run_apply(path, operations=operations, resource_paths=[image_path])
        assert writer.verified and not scope.execution_inputs.snapshot()['unbound']
        assert {item['source_id'] for item in scope.execution_inputs.snapshot()['sources']} == {str(read.receipt_id), str(image_read.receipt_id)}
    else:
        with pytest.raises(OfficeCLIExecutionError):
            service.run_apply(path, operations=operations)
        assert source.read_bytes() == before and not writer.verified
    assert len(calls) == 1
    with ZipFile(source) as document:
        embedded = any(document.read(name) == image.read_bytes() for name in document.namelist())
        assert embedded == declared, 'Native Office must preserve authorized images and exclude foreign host files'
