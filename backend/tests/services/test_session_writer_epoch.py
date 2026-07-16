from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_writer_artifact_digest_is_source_stable_across_railway_deployments(monkeypatch) -> None:
    from app.services.session_writer_epoch import session_writer_artifact_identity

    monkeypatch.delenv("HIVE_ARTIFACT_DIGEST", raising=False)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "backend-deployment")
    first = session_writer_artifact_identity()[2]
    monkeypatch.setenv("RAILWAY_DEPLOYMENT_ID", "backend-api-deployment")
    second = session_writer_artifact_identity()[2]

    assert first == second


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_assign_new_runtime_task_uses_current_database_epoch_generation() -> None:
    from app.services.session_writer_epoch import assign_runtime_task_writer_generation

    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(
                    state="v1_draining",
                    new_run_generation=2,
                    allowed_existing_generations_json=[1, 2],
                    enforcement_mode="enforce",
                    version=7,
                    release_id="release-7",
                )
            )
        )
    )
    task = SimpleNamespace(writer_generation=None)

    snapshot = await assign_runtime_task_writer_generation(db, task)

    assert task.writer_generation == 2
    assert snapshot.new_run_generation == 2
    assert snapshot.version == 7


@pytest.mark.asyncio
async def test_assign_new_runtime_task_fails_closed_when_epoch_row_is_missing() -> None:
    from app.services.session_writer_epoch import SessionWriterEpochUnavailable, assign_runtime_task_writer_generation

    db = SimpleNamespace(execute=AsyncMock(return_value=_ScalarResult(None)))

    with pytest.raises(SessionWriterEpochUnavailable, match="missing"):
        await assign_runtime_task_writer_generation(db, SimpleNamespace(writer_generation=None))


@pytest.mark.asyncio
async def test_assign_new_runtime_task_rejects_artifact_without_generation_support() -> None:
    from app.services.session_writer_epoch import (
        SessionWriterGenerationUnsupported,
        assign_runtime_task_writer_generation,
    )

    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=_ScalarResult(
                SimpleNamespace(
                    state="v2_only",
                    new_run_generation=3,
                    allowed_existing_generations_json=[3],
                    enforcement_mode="enforce",
                    version=9,
                    release_id="future-release",
                )
            )
        )
    )

    with pytest.raises(SessionWriterGenerationUnsupported, match="generation 3"):
        await assign_runtime_task_writer_generation(
            db,
            SimpleNamespace(writer_generation=None),
            supported_generations=(1, 2),
        )


def test_epoch_transition_validation_requires_drain_before_v2_only() -> None:
    from app.services.session_writer_epoch import SessionWriterEpochTransitionError, validate_writer_epoch_transition

    with pytest.raises(SessionWriterEpochTransitionError, match="generation 1 active runs"):
        validate_writer_epoch_transition(
            current_state="v1_draining",
            target_state="v2_only",
            new_run_generation=2,
            allowed_existing_generations=(2,),
            active_runs_by_generation={1: 1, 2: 4},
            live_supported_generations=((1, 2), (2,)),
        )


def test_epoch_transition_validation_accepts_v2_only_after_drain_and_heartbeat_support() -> None:
    from app.services.session_writer_epoch import validate_writer_epoch_transition

    validate_writer_epoch_transition(
        current_state="v1_draining",
        target_state="v2_only",
        new_run_generation=2,
        allowed_existing_generations=(2,),
        active_runs_by_generation={1: 0, 2: 4},
        live_supported_generations=((1, 2), (2,)),
    )


@pytest.mark.asyncio
async def test_writer_heartbeat_upsert_persists_artifact_support_set() -> None:
    from app.services.session_writer_epoch import upsert_session_writer_heartbeat

    class _Db:
        def __init__(self):
            self.execute = AsyncMock(return_value=_ScalarResult(None))
            self.added = []
            self.flushed = False

        def add(self, row):
            self.added.append(row)

        async def flush(self):
            self.flushed = True

    db = _Db()

    row = await upsert_session_writer_heartbeat(
        db,
        service="backend-api",
        instance_id="instance-1",
        artifact_digest="sha256:artifact",
        supported_generations=(1, 2),
    )

    assert row.service == "backend-api"
    assert row.instance_id == "instance-1"
    assert row.artifact_digest == "sha256:artifact"
    assert row.supported_generations_json == [1, 2]
    assert db.added == [row]
    assert db.flushed is True
