import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.memory.t2 import job_sweep, segment_package
from app.runtime import hooks_setup

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest.fixture
def packaging(monkeypatch, tmp_path):
    release = asyncio.Event()
    events: list[tuple[str, str]] = []

    async def run_package(*, job_id, t0_segment_id, **_kwargs):
        events.append(("start", job_id))
        await release.wait()
        events.append(("finish", job_id))
        return segment_package.T2SegmentPackageResult(
            status="held" if t0_segment_id == "retry" else "committed",
            job_id=job_id,
            package_dir=tmp_path,
            staging_dir=tmp_path,
        )

    monkeypatch.setattr(segment_package, "build_t2_segment_package_with_llm", run_package)
    return release, events


def _enqueue(tmp_path, job_id, *, agent_id=None, segment_id=None):
    kwargs = dict(
        data_root=tmp_path,
        agent_id=agent_id or uuid4(),
        tenant_id=uuid4(),
        session_id=str(uuid4()),
        t0_segment_id=segment_id or job_id,
        job_id=job_id,
        session_lineage=None,
    )
    receipt = segment_package.enqueue_t2_segment_package_job(**kwargs)
    return kwargs, receipt.staging_dir / "job_manifest.json"


def _schedule(job):
    hooks_setup._schedule_t2_segment_package_job(**job)
    return hooks_setup._T2_BACKGROUND_TASKS[f"{job['agent_id']}:{job['job_id']}"]


def _recover(job):
    return job_sweep.sweep_stale_t2_jobs(
        data_root=job["data_root"], agent_id=job["agent_id"], now=datetime.now(UTC) + timedelta(hours=2)
    )


async def test_detached_jobs_and_sweep_retry_share_fifo_slot(tmp_path, packaging):
    release, events = packaging
    queued_ids = ["first", "second", "third"]
    tasks = [_schedule(_enqueue(tmp_path, job_id)[0]) for job_id in queued_ids]
    retry, _ = _enqueue(tmp_path, "sweep")
    _recover(retry)

    await asyncio.sleep(0)
    sweep = asyncio.create_task(job_sweep.sweep_t2_jobs(agent_id=retry["agent_id"], data_root=tmp_path))
    await asyncio.sleep(0)
    before_release = events.copy()
    release.set()
    *_, report = await asyncio.wait_for(asyncio.gather(*tasks, sweep), timeout=10)

    assert before_release == [("start", "first")]
    assert events == [(event, job_id) for job_id in [*queued_ids, "sweep"] for event in ("start", "finish")]
    assert report.committed == ("sweep",)


async def test_recovered_waiting_job_runs_only_once_even_if_retry_remains_held(tmp_path, packaging):
    release, events = packaging
    blocker = _schedule(_enqueue(tmp_path, "first")[0])
    job, path = _enqueue(tmp_path, "recovered", segment_id="retry")
    waiting = _schedule(job)

    await asyncio.sleep(0)
    recovery = _recover(job)
    sweeps = [
        asyncio.create_task(job_sweep.sweep_t2_jobs(agent_id=job["agent_id"], data_root=tmp_path)) for _ in range(2)
    ]
    await asyncio.sleep(0)
    release.set()
    _, _, first, second = await asyncio.wait_for(asyncio.gather(blocker, waiting, *sweeps), timeout=10)

    assert recovery.recovered_stale == ("recovered",)
    assert events == [("start", "first"), ("finish", "first"), ("start", "recovered"), ("finish", "recovered")]
    assert first.retried == first.still_held == ("recovered",)
    assert second.retried == ()
    manifest = json.loads(path.read_text())
    assert manifest["recovered_from"] == "queued"
    assert manifest["retry_count"] == 1


async def test_sweep_does_not_recover_a_stale_snapshot_after_detached_job_commits(tmp_path, packaging):
    release, events = packaging
    earlier, _ = _enqueue(tmp_path, "a")
    _recover(earlier)
    later, path = _enqueue(tmp_path, "b", agent_id=earlier["agent_id"])
    detached = _schedule(later)

    await asyncio.sleep(0)
    sweep = asyncio.create_task(
        job_sweep.sweep_t2_jobs(
            agent_id=earlier["agent_id"], data_root=tmp_path, now=datetime.now(UTC) + timedelta(hours=2)
        )
    )
    await asyncio.sleep(0)
    release.set()
    _, report = await asyncio.wait_for(asyncio.gather(detached, sweep), timeout=10)

    assert events.count(("start", "b")) == 1
    assert report.retried == ("a",)
    assert report.recovered_stale == ()
    assert json.loads(path.read_text())["status"] == "committed"


async def test_cancelled_waiters_stay_queued_for_crash_recovery(tmp_path, packaging):
    release, _ = packaging
    blocker = _schedule(_enqueue(tmp_path, "first")[0])
    second, second_path = _enqueue(tmp_path, "second")
    third, third_path = _enqueue(tmp_path, "third", agent_id=second["agent_id"])
    paths = [second_path, third_path]
    before = [path.read_text() for path in paths]
    waiters = [_schedule(second), _schedule(third)]

    await asyncio.sleep(0)
    for task in waiters:
        task.cancel()
    outcomes = await asyncio.gather(*waiters, return_exceptions=True)
    after = [path.read_text() for path in paths]
    release.set()
    await asyncio.wait_for(blocker, timeout=10)
    report = _recover(second)

    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)
    assert after == before
    assert report.recovered_stale == ("second", "third")
    for path in paths:
        manifest = json.loads(path.read_text())
        assert manifest["status"] == "held"
        assert manifest["recovered_from"] == "queued"
