"""Saved remote packets become real original-work files without another RPC."""

import base64
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.chat_artifact import ChatArtifact
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_result import RuntimeResultObject
from app.models.user import User
from app.services.content_input_receipts import ContentInputReceiptError
from app.services.runtime_notification_outbox import RuntimeNotificationOutboxService
from tests.integration import test_m3o_remote_a2a_tasks_postgres as _previous
from tests.integration.test_m3o_remote_a2a_tasks_postgres import command, configure, execute
from tests.services.test_remote_a2a_transport import remote_server

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
app_user_engine = _previous.app_user_engine
app_user_sessionmaker = _previous.app_user_sessionmaker
machine_api = _previous.machine_api
migrated_pg_url = _previous.migrated_pg_url
owner_engine = _previous.owner_engine
owner_sessionmaker = _previous.owner_sessionmaker
pg_container = _previous.pg_container
remote_work = _previous.remote_work


@pytest.fixture
async def artifact_work(remote_work, monkeypatch, tmp_path):
    from app.api import files

    seed, api, session_id = remote_work
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path / "employees"))
    api._transport.app.include_router(files.router)
    return seed, api, session_id


def packet(text, state="TASK_STATE_COMPLETED"):
    return {"task": {"id": "file-task", "status": {"state": state},
        "artifacts": [{"artifactId": "report", "parts": [{"filename": "report.txt", "text": text}]}]}}


def recovery_frame(seed):
    return {"expected_user_id": str(seed["owner"]), "expected_tenant_id": str(seed["tenant"])}


async def test_saved_packet_returns_complete_inline_files_to_original_work(artifact_work, app_user_sessionmaker):
    seed, api, session_id = artifact_work
    body = bytes(range(256)) * 600
    text = "Complete report\n" * 4000
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: {"task": {"id": "file-task", "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "report", "parts": [
                {"filename": "report.txt", "text": text},
                {"filename": "bytes.bin", "raw": base64.b64encode(body).decode()},
                {"filename": "data.json", "data": {"value": 42}},
                {"filename": "external.pdf", "url": remote.origin + "/external.pdf"},
            ]}]}}
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        assert created.status_code == 201, created.text
        await execute(seed, created.json()["id"], app_user_sessionmaker)
        delivery = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(
            worker_id="remote-file-return")
        assert delivery["claimed"] == delivery["delivered"] == 1, delivery
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            files = list(await db.scalars(select(ChatArtifact).where(ChatArtifact.session_id == session_id)))
            assert len(files) == 3
            originals = {"report.txt": text.encode(), "bytes.bin": body, "data.json": b'{"value":42}'}
            for artifact in files:
                assert artifact.owner_user_id == seed["owner"] and artifact.agent_id == seed["agent"]
                assert artifact.source == "remote_a2a_return" and artifact.runtime_task_id == UUID(created.json()["id"])
                root = Path(get_settings().AGENT_DATA_DIR) / str(seed["agent"])
                assert (root / artifact.path).read_bytes() == originals[artifact.name]
                event = await db.scalar(select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.message_id == artifact.message_id))
                assert artifact.id.hex in str(event.parts_json).replace("-", "")
                assert event.actor_type == "system" and event.metadata_json["role"] == "system"
                url = f"/agents/{seed['agent']}/files/artifacts/{artifact.id}"
                response = await api.get(url + "/download")
                assert response.status_code == 200 and response.content == originals[artifact.name], response.text
                versions = await api.get(f"/agents/{seed['agent']}/files/versions", params={"path": artifact.path})
                assert versions.status_code == 200 and versions.json()["total"] == 1, versions.text
                assert versions.json()["versions"][0]["content_hash"] == hashlib.sha256(originals[artifact.name]).hexdigest()
            packet_event = await db.scalar(select(ChatTranscriptEvent).where(
                ChatTranscriptEvent.session_id == session_id,
                ChatTranscriptEvent.event_type == "remote_a2a_artifacts"))
            assert packet_event.metadata_json["file_returns"][-1]["reason_code"] == "remote_a2a_external_url_not_fetched"
        assert len(remote.requests) == 2


@pytest.mark.parametrize("human_edit,second", [(False, "SECOND"), (True, "SECOND"), (False, "FIRST")])
async def test_remote_update_versions_preserve_original_and_human_edits(artifact_work, app_user_sessionmaker, human_edit, second):
    from tests.integration.test_m3o_remote_a2a_tasks_postgres import request_frame

    seed, api, session_id = artifact_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: packet("FIRST", "TASK_STATE_WORKING")
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        service = RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker)
        assert (await service.drain_once(worker_id="first-file"))["delivered"] == 1
        first = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert first.status_code == 200 and len(first.json()["parts"]) == 1, first.text
        first_part = first.json()["parts"][0]
        file_path = first_part["path"]
        if human_edit:
            edited = await api.put(f"/agents/{seed['agent']}/files/content", params={"path": file_path},
                json={"content": "OWNER EDIT", "expected_current_hash": hashlib.sha256(b"FIRST").hexdigest()})
            assert edited.status_code == 200, edited.text
        remote.reply = lambda request: packet(second)["task"]
        refreshed = await api.post(path + f"/tasks/{task_id}/operations", json={**request_frame(seed, 1),
            "operation_id": str(uuid4()), "action": "refresh"})
        assert refreshed.status_code == 200, refreshed.text
        await execute(seed, task_id, app_user_sessionmaker)
        assert (await service.drain_once(worker_id="second-file"))["delivered"] == 1
        recovered = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert recovered.status_code == 200, recovered.text
        assert len(recovered.json()["parts"]) == 2
        last = recovered.json()["parts"][-1]
        assert (last["path"] != file_path) is human_edit
        root = Path(get_settings().AGENT_DATA_DIR) / str(seed["agent"])
        assert (root / file_path).read_text() == ("OWNER EDIT" if human_edit else second)
        assert (root / last["path"]).read_bytes() == second.encode()
        assert recovered.json()["file_returns"][-1]["status"] == ("preserved_conflict" if human_edit else "available")
        original = await api.get(f"/agents/{seed['agent']}/files/artifacts/{first_part['artifact_id']}/download")
        assert original.status_code == 200 and original.content == b"FIRST", original.text
        versions = await api.get(f"/agents/{seed['agent']}/files/versions", params={"path": file_path})
        assert versions.status_code == 200 and versions.json()["total"] == 2, versions.text
        old = next(row for row in versions.json()["versions"] if row["content_hash"] == hashlib.sha256(b"FIRST").hexdigest())
        reopened = await api.get(f"/agents/{seed['agent']}/files/versions/{old['version_id']}/download", params={"path": file_path})
        assert reopened.status_code == 200 and reopened.content == b"FIRST", reopened.text
        replay = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert replay.json() == recovered.json() and len(remote.requests) == 3


async def test_storage_failure_recovers_saved_packet_without_rpc_or_duplicate_versions(
    artifact_work, app_user_sessionmaker, monkeypatch
):
    from app.services import remote_a2a_artifacts as importer

    seed, api, session_id = artifact_work
    capture = importer.capture_receipt_version

    def fail_capture(*_args):
        raise ContentInputReceiptError("workspace_version_unavailable", "Owned test storage failure.", 503)

    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: packet("COMPLETE SAVED BYTES")
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        monkeypatch.setattr(importer, "capture_receipt_version", fail_capture)
        failed = await RuntimeNotificationOutboxService(session_factory=app_user_sessionmaker).drain_once(worker_id="disk-failure")
        assert failed["delivered"] == 0, failed
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert not list(await db.scalars(select(ChatArtifact).where(ChatArtifact.session_id == session_id)))
            saved = await db.scalar(select(RuntimeResultObject).where(RuntimeResultObject.source_run_id.startswith(task_id + ":")))
            assert "COMPLETE SAVED BYTES" in saved.payload_bytes.decode()
        monkeypatch.setattr(importer, "capture_receipt_version", capture)
        # Historical local recovery is still available after the connection is disabled.
        assert (await api.post(path + "/disable", json={**recovery_frame(seed), "expected_version": 1})).status_code == 200
        recovered = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert recovered.status_code == 200 and len(recovered.json()["parts"]) == 1, recovered.text
        assert len(remote.requests) == 2
        replay = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert replay.json() == recovered.json()
        versions = await api.get(f"/agents/{seed['agent']}/files/versions", params={"path": recovered.json()["parts"][0]["path"]})
        assert versions.status_code == 200 and versions.json()["total"] == 1, versions.text


async def test_source_reader_binding_and_missing_snapshot_do_not_fall_back(artifact_work, app_user_sessionmaker):
    from app.core.security import create_access_token
    from app.services.remote_a2a_artifacts import validate_remote_dependency

    seed, api, session_id = artifact_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: packet("ORIGINAL")
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        recovered = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert recovered.status_code == 200, recovered.text
        part = recovered.json()["parts"][0]
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            result = await db.scalar(select(RuntimeResultObject).where(RuntimeResultObject.source_run_id.startswith(task_id + ":")))
            other = User(id=uuid4(), tenant_id=seed["tenant"], username=uuid4().hex, email=uuid4().hex + "@example.test",
                password_hash="synthetic", display_name="Other reader", role="org_admin")
            db.add(other)
            await db.commit()
            dep = {"source_kind": "remote_a2a_result", "source_id": str(result.id), "source_version": result.sha256}
            with pytest.raises(ContentInputReceiptError, match="exact remote packet"):
                await validate_remote_dependency(db, seed["tenant"], dep, user=other)
            with pytest.raises(ContentInputReceiptError):
                await validate_remote_dependency(db, uuid4(), dep, user=await db.get(User, seed["owner"]))
            copied = result.payload_bytes + b" "
            unbound = RuntimeResultObject(id=uuid4(), tenant_id=result.tenant_id, source_kind=result.source_kind,
                source_run_id=result.source_run_id, payload_bytes=copied, sha256=hashlib.sha256(copied).hexdigest(),
                size_bytes=len(copied))
            db.add(unbound)
            await db.commit()
            with pytest.raises(ContentInputReceiptError):
                await validate_remote_dependency(db, seed["tenant"], {**dep, "source_id": str(unbound.id),
                    "source_version": unbound.sha256}, user=await db.get(User, seed["owner"]))
            token = create_access_token(str(other.id), other.role, str(other.tenant_id))
            denied = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed),
                headers={"Authorization": "Bearer " + token})
            assert denied.status_code == 404, denied.text
            artifact = await db.get(ChatArtifact, UUID(part["artifact_id"]))
            root = Path(get_settings().AGENT_DATA_DIR) / str(seed["agent"])
            (root / artifact.path).write_bytes(b"NEW WORKSPACE BYTES")
            snapshot = root / artifact.snapshot_json["snapshot_storage_path"]
            snapshot.unlink()
        missing = await api.get(f"/agents/{seed['agent']}/files/artifacts/{part['artifact_id']}/download")
        assert missing.status_code != 200 and b"NEW WORKSPACE BYTES" not in missing.content
        restored = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert restored.status_code == 200, restored.text
        download = await api.get(f"/agents/{seed['agent']}/files/artifacts/{part['artifact_id']}/download")
        assert download.status_code == 200 and download.content == b"ORIGINAL", download.text
        assert (root / artifact.path).read_bytes() == b"NEW WORKSPACE BYTES"
        snapshot.write_bytes(b"CHANGED SNAPSHOT")
        corrupt = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert corrupt.status_code == 409 and corrupt.json()["detail"]["code"] == "remote_a2a_snapshot_changed"
        assert snapshot.read_bytes() == b"CHANGED SNAPSHOT"
        assert len(remote.requests) == 2


async def test_empty_binary_and_untrusted_names_stay_in_generated_workspace(artifact_work, app_user_sessionmaker):
    seed, api, session_id = artifact_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        remote.reply = lambda request: {"task": {"id": "file-task", "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "report", "parts": [{"filename": "../../private/empty.bin", "raw": ""}]}]}}
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        returned = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert returned.status_code == 200, returned.text
        part = returned.json()["parts"][0]
        assert part["size"] == 0 and part["name"] == "empty.bin"
        assert part["path"].startswith(f"workspace/remote-a2a/{task_id}/") and ".." not in part["path"]
        download = await api.get(f"/agents/{seed['agent']}/files/artifacts/{part['artifact_id']}/download")
        assert download.status_code == 200 and download.content == b""
        assert len(remote.requests) == 2


async def test_duplicate_remote_artifact_ids_are_recoverable_and_never_overwrite(artifact_work, app_user_sessionmaker):
    seed, api, session_id = artifact_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        response = packet("FIRST")
        response["task"]["artifacts"].append({"artifactId": "report", "parts": [{"filename": "report.txt", "text": "SECOND"}]})
        remote.reply = lambda request: response
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        returned = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert returned.status_code == 409, returned.text
        assert returned.json()["detail"]["code"] == "remote_a2a_artifact_identity_conflict"
        assert returned.json()["detail"]["remote_request_replayed"] is False
        async with tenant_scoped_session(seed["tenant"], session_factory=app_user_sessionmaker) as db:
            assert not list(await db.scalars(select(ChatArtifact).where(ChatArtifact.session_id == session_id)))
        assert len(remote.requests) == 2


async def test_binary_credential_echo_is_denied_locally_without_losing_other_files(artifact_work, app_user_sessionmaker):
    seed, api, session_id = artifact_work
    with remote_server() as remote:
        path = await configure(seed, api, remote)
        response = packet("SAFE REPORT")
        response["task"]["artifacts"][0]["parts"].append({"filename": "echo.bin",
            "raw": base64.b64encode(b"prefix::synthetic-remote-credential::suffix").decode()})
        remote.reply = lambda request: response
        created = await api.post(path + "/tasks", json=command(seed, session_id))
        task_id = created.json()["id"]
        await execute(seed, task_id, app_user_sessionmaker)
        returned = await api.post(path + f"/tasks/{task_id}/artifacts", json=recovery_frame(seed))
        assert returned.status_code == 200, returned.text
        assert len(returned.json()["parts"]) == 1
        assert returned.json()["file_returns"][1]["reason_code"] == "remote_a2a_credential_redacted"
        saved = await api.get(path + f"/tasks/{task_id}")
        assert response["task"]["artifacts"][0]["parts"][1]["raw"] not in saved.text
        assert "synthetic-remote-credential" not in saved.text and "SAFE REPORT" in saved.text
        assert len(remote.requests) == 2
