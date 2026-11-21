"""Measure a real private Web turn alongside a detached T2 package job."""

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from shutil import copytree
from statistics import median

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine

from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events, seal_t0_session_segment
from app.memory.t2 import job_sweep, segment_package
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.runtime import hooks_setup, invoker
from tests.integration.test_a2fp1_private_turn_budget_postgres import (
    ANSWER,
    LocalAuxTransport,
    app_user_engine,  # noqa: F401
    assert_committed_reply,
    member_request,  # noqa: F401
)
from tests.integration.test_m1f_assistant_binding_postgres import _model
from tests.integration.test_sp1_send_statement_budget_postgres import claim, configure
from tests.integration.test_t2_source_revocation_postgres import native_workspace, read_turn  # noqa: F401
from tests.integration.test_web_chat_worker_restart_kernel_recovery import ScriptedLLMClient

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
SOURCE_EVENTS = 48


@pytest.fixture
async def packaging_work(read_turn, owner_sessionmaker, tmp_path):  # noqa: F811
    n = read_turn.n
    async with owner_sessionmaker() as db:
        row = (await db.scalars(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.session_id == n.session,
            ChatTranscriptEvent.item_kind == "tool_result",
        ))).one()
        model = await _model(db, n.tenant, label="BG1 local summary")
        from app.services.secrets_provider import get_secrets_provider

        model.api_key_encrypted = get_secrets_provider().encrypt("synthetic-local-provider-only")
        await db.commit()
    source_id = row.metadata_json["t0_bridge_event_id"]
    assert source_id in read_turn.tainted_ids
    source = next(item for item in replay_t0_session_events(agent_id=n.agent, session_id=n.session)
                  if item.event_id == source_id)
    seed_root = tmp_path / "packaging-input"
    # Repeat a genuine shared-read binding in a new sealed segment so every
    # trial revalidates it against the same authorized workspace source.
    for _ in range(SOURCE_EVENTS):
        append_t0_session_event(
            agent_id=n.agent, session_id=n.session, tenant_id=n.tenant, runtime_task_id=n.run,
            event_type=source.event_type, role=source.role, content=source.content,
            metadata={"transcript_event_id": str(row.id)}, data_root=seed_root,
        )
    seal = seal_t0_session_segment(agent_id=n.agent, session_id=n.session, reason="turn_stop", data_root=seed_root)
    assert seal is not None

    def enqueue(root):
        copytree(seed_root, root)
        args = dict(data_root=root, agent_id=n.agent, tenant_id=n.tenant,
                    session_id=str(n.session), t0_segment_id=seal.segment_id)
        queued = segment_package.enqueue_t2_segment_package_job(**args)
        return queued, dict(**args, job_id=queued.job_id, session_lineage=None)

    return enqueue


def _schedule(args):
    hooks_setup._schedule_t2_segment_package_job(**args)
    return hooks_setup._T2_BACKGROUND_TASKS[f"{args['agent_id']}:{args['job_id']}"]


@pytest.fixture
def hold_web_turn(monkeypatch):
    from app.services import web_chat_run_orchestrator, web_chat_runtime

    async def noop(*_args):
        pass

    @asynccontextmanager
    async def hold():
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_lifecycle(**_kwargs):
            entered.set()
            await release.wait()

        with monkeypatch.context() as patch:
            patch.setattr(web_chat_run_orchestrator, "run_web_chat_task", hold_lifecycle)
            patch.setattr(web_chat_runtime, "_retry_pending_session_run_settlements", noop)
            patch.setattr(web_chat_runtime, "_settle_completed_run_after_execution", noop)
            task = asyncio.create_task(web_chat_runtime.execute_web_chat_run(
                uuid.uuid4(), tenant_id=uuid.uuid4(), session_id=str(uuid.uuid4()),
            ))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                yield
            finally:
                release.set()
                await task

    return hold


@pytest.fixture
def local_t2_models(monkeypatch):
    from app.services import llm_client

    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))


def _package_files(job, root):
    manifest = json.loads((job.staging_dir / "job_manifest.json").read_text())
    assert manifest["status"] == "committed", manifest
    bundle = json.loads((job.staging_dir / "source_bundle.json").read_text())
    assert len([event for event in bundle["t0_events"] if event["event_type"] != "segment_boundary"]) == SOURCE_EVENTS
    bundle.pop("created_at")
    for source_event in bundle["t0_events"]:
        source_event["truth_path"] = str(Path(source_event["truth_path"]).relative_to(root))
    package_manifest = json.loads((job.package_dir / "manifest.json").read_text())
    package_manifest.pop("created_at")
    files = [(job.package_dir / name).read_bytes() for name in ("summary.md", "labels.md", "review.md")]
    return bundle, package_manifest, files


@pytest.mark.usefixtures("local_t2_models")
async def test_detached_t2_yields_before_lock_and_commits_same_sources(packaging_work, hold_web_turn, tmp_path):
    quiet, quiet_args = packaging_work(tmp_path / "quiet")
    await _schedule(quiet_args)
    queued, args = packaging_work(tmp_path / "held")
    task = None
    try:
        async with hold_web_turn():
            task = _schedule(args)
            await asyncio.sleep(0)
            assert not job_sweep.T2_PACKAGE_JOB_LOCK.locked()
            assert json.loads((queued.staging_dir / "job_manifest.json").read_text())["status"] == "queued"
    finally:
        if task is not None:
            await task
    assert _package_files(queued, args["data_root"]) == _package_files(quiet, quiet_args["data_root"])


@pytest.mark.usefixtures("local_t2_models")
async def test_t2_rechecks_between_event_sessions(packaging_work, hold_web_turn, monkeypatch, tmp_path):
    from app import database

    job, args = packaging_work(tmp_path / "resume")
    first_event_done, resume = asyncio.Event(), asyncio.Event()
    opened = 0
    session = database.tenant_scoped_session

    @asynccontextmanager
    async def tracked_session(*args, **kwargs):
        nonlocal opened
        is_job = asyncio.current_task() is task
        if is_job:
            opened += 1
        async with session(*args, **kwargs) as db:
            yield db
        if is_job and opened == 2:  # Inventory plus the first event.
            first_event_done.set()
            await resume.wait()

    monkeypatch.setattr(database, "tenant_scoped_session", tracked_session)
    task = _schedule(args)
    try:
        await asyncio.wait_for(first_event_done.wait(), timeout=10)
        async with hold_web_turn():
            resume.set()
            await asyncio.sleep(0)
            assert opened == 2
    finally:
        resume.set()
        await task
    _package_files(job, args["data_root"])


@pytest.mark.usefixtures("local_t2_models")
async def test_detached_t2_exhausts_one_wait_budget(packaging_work, hold_web_turn, monkeypatch, tmp_path):
    from app.runtime import interactive_turn_gate as gate

    monkeypatch.setattr(gate, "BACKGROUND_WAIT_CAP_SECONDS", 0.02)
    monkeypatch.setattr(segment_package, "T2_JOB_WAIT_BUDGET_SECONDS", 0.05)
    wait = gate.wait_for_no_interactive_turns
    waited, caps = [], []

    async def observed_wait(cap):
        elapsed = await wait(cap)
        if elapsed:
            waited.append(elapsed)
            caps.append(cap)
        return elapsed

    monkeypatch.setattr(gate, "wait_for_no_interactive_turns", observed_wait)
    job, args = packaging_work(tmp_path / "cap")
    async with hold_web_turn():
        await asyncio.wait_for(_schedule(args), timeout=15)
        _package_files(job, args["data_root"])
    # BG1 r2: wait budgets are wall-clock deadlines so queued jobs cannot multiply lock hold time.
    assert sum(waited) >= sum(caps) - 0.001
    assert max(caps) <= 0.02
    assert sum(caps) <= 0.0501


@dataclass
class Meter:
    statements: int = 0
    wall_ms: float = 0
    cpu_ms: float = 0

    def start(self):
        self.wall_start = time.perf_counter()
        self.cpu_start = time.thread_time()

    def stop(self):
        self.wall_ms = (time.perf_counter() - self.wall_start) * 1000
        self.cpu_ms = (time.thread_time() - self.cpu_start) * 1000

    def record(self):
        return {"wall_ms": round(self.wall_ms, 3), "thread_cpu_ms": round(self.cpu_ms, 3),
                "statements": self.statements}


async def test_background_packaging_preserves_first_request_budget(
    member_request, packaging_work, owner_sessionmaker, monkeypatch, tmp_path,  # noqa: F811
):
    from app.core.events import close_redis
    from app.services import agent_tools, content_input_receipts, llm_client, web_chat_runtime as runtime

    _, _, _, factory = member_request
    seed, client = await configure(member_request, owner_sessionmaker, factory, monkeypatch, tmp_path)
    hooks_setup.register_memory_hooks()
    monkeypatch.setattr(agent_tools, "_COLLECTED_TOOLS", None)
    monkeypatch.setattr(agent_tools, "_always_core_tools", None)
    monkeypatch.setattr(llm_client, "create_llm_client", lambda **_kwargs: LocalAuxTransport([]))
    execute = runtime.execute_web_chat_run
    package = segment_package.run_t2_segment_package_job
    validate = content_input_receipts._validate_input_dependencies
    meter_context = ContextVar("bg1_sql_meter", default=None)

    def statement(_conn, _cursor, _statement, _params, _context, _many):
        meter = meter_context.get()
        if meter is not None and not meter.wall_ms:
            meter.statements += 1

    async def trial(name, with_background=False):
        turn, job = Meter(), Meter()
        job_task = None
        job_submitted = job_finished = 0
        before_request = validations = 0
        manifest_result = None
        if with_background:
            queued, args = packaging_work(tmp_path / f"work-{name}")

        class Provider(ScriptedLLMClient):
            async def stream(self, **kwargs):
                nonlocal before_request
                turn.stop()
                before_request = job.statements
                return await super().stream(**kwargs)

        provider = Provider([{"content": ANSWER, "stream_text": ANSWER}])

        async def measured_package(**kwargs):
            nonlocal manifest_result
            if not with_background or kwargs["job_id"] != queued.job_id:
                return await package(**kwargs)
            token = meter_context.set(job)
            job.start()
            try:
                manifest_result = await package(**kwargs)
                return manifest_result
            finally:
                job.stop()
                meter_context.reset(token)

        async def count_validation(*args, **kwargs):
            nonlocal validations
            if meter_context.get() is job:
                validations += 1
            return await validate(*args, **kwargs)

        def mark_job_finished(_task):
            nonlocal job_finished
            job_finished = time.perf_counter()

        async def measured_execute(*args_run, **kwargs):
            nonlocal job_task, job_submitted
            if with_background:
                job_submitted = time.perf_counter()
                job_task = _schedule(args)
                job_task.add_done_callback(mark_job_finished)
            token = meter_context.set(turn)
            turn.start()
            try:
                await execute(*args_run, **kwargs)
            finally:
                meter_context.reset(token)

        response = await client.post(f"/api/agents/{seed.agent_id}/sessions/runs",
                                     json={"content": "hello", "input_id": str(uuid.uuid4())})
        assert response.status_code == 201, response.text
        body = response.json()
        run_id = uuid.UUID(body["run"]["run_id"])
        await claim(seed, run_id, owner_sessionmaker)
        with monkeypatch.context() as patch:
            patch.setattr(invoker, "create_llm_client", lambda **_kwargs: provider)
            patch.setattr(runtime, "execute_web_chat_run", measured_execute)
            patch.setattr(segment_package, "run_t2_segment_package_job", measured_package)
            patch.setattr(content_input_receipts, "_validate_input_dependencies", count_validation)
            assert runtime.dispatch_web_chat_run(run_id, claim_version=1, worker_id="rl0c")
            await runtime._TASKS[run_id.hex]
            # Drain the measured job and any terminal-hook work before starting the next trial.
            await asyncio.gather(*hooks_setup._T2_BACKGROUND_TASKS.values())
            if with_background:
                await job_task
        assert len(provider.requests) == 1
        await assert_committed_reply(owner_sessionmaker, run_id, uuid.UUID(body["session"]["id"]), seed)
        record = {"trial": name, "turn": turn.record()}
        if with_background:
            assert manifest_result is not None and manifest_result.status == "committed", manifest_result
            assert validations >= SOURCE_EVENTS, validations
            record.update(job=job.record(), job_elapsed_ms=round((job_finished - job_submitted) * 1000, 3),
                          job_statements_at_first_request=before_request, validations=validations)
        print("BG1_SAMPLE", json.dumps(record), flush=True)
        return record

    samples = {"A": [], "B": []}
    event.listen(Engine, "before_cursor_execute", statement)
    try:
        await trial("warmup")
        for index in range(1, 6):
            for lane in ("A", "B") if index % 2 else ("B", "A"):
                samples[lane].append(await trial(f"{lane}{index}", with_background=lane == "B"))
    finally:
        event.remove(Engine, "before_cursor_execute", statement)
        await close_redis()

    def medians(records, key):
        return {name: median(row[key][name] for row in records) for name in records[0][key]}

    a, b = (medians(samples[lane], "turn") for lane in ("A", "B"))
    summary = {"A": a, "B": b, "B_job": medians(samples["B"], "job"),
               "B_job_elapsed_ms": median(row["job_elapsed_ms"] for row in samples["B"])}
    print("BG1_MEDIANS", json.dumps(summary), flush=True)
    assert all(row["job_statements_at_first_request"] == 0 for row in samples["B"]), samples["B"]
    assert b["wall_ms"] == pytest.approx(a["wall_ms"], rel=0.10), summary
