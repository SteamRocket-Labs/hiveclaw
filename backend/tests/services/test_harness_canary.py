from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _QueuedDB:
    def __init__(self, *results):
        self._results = list(results)
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute call")
        return _Result(self._results.pop(0))

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        raise AssertionError("rollback should not be called")


def _agent(**overrides):
    values = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "name": "Autonomy Agent",
        "heartbeat_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _trigger(agent_id):
    return SimpleNamespace(id=uuid4(), agent_id=agent_id, is_enabled=True)


async def test_harness_canary_writes_runtime_task_artifacts_and_evolution_ledger(tmp_path):
    from app.services.agent_work_ledger import load_agent_work_ledger
    from app.services.harness_canary import run_harness_canary
    from app.services.harness_validation_report import audit_agent_harness_snapshot

    agent = _agent()
    trigger = _trigger(agent.id)
    db = _QueuedDB([agent], [trigger])

    result = await run_harness_canary(
        db=db,
        max_agents=10,
        data_root=tmp_path,
    )

    assert result["schema"] == "harness_canary_run.v1"
    assert result["mode"] == "write"
    assert result["totals"]["agents_considered"] == 1
    assert result["totals"]["agents_written"] == 1
    assert result["totals"]["h4_written"] == 1
    assert result["totals"]["h5_written"] == 1
    assert db.commits == 1
    assert len(db.added) == 1

    runtime_task = db.added[0]
    assert runtime_task.task_type == "harness_canary"
    assert runtime_task.status == "completed"
    assert runtime_task.parent_agent_id == agent.id
    assert runtime_task.metadata_json["harness_canary"] is True
    assert runtime_task.metadata_json["long_task_validation_passed"] is True
    assert runtime_task.metadata_json["evolution_validation_passed"] is True

    runtime_task_id = runtime_task.id.hex
    agent_root = tmp_path / str(agent.id)
    artifact_root = agent_root / "runtime_artifacts" / "long_tasks" / runtime_task_id
    assert (artifact_root / "plan.json").exists()
    assert (artifact_root / "progress.jsonl").exists()
    assert (artifact_root / "validation_report.json").exists()
    work_ledger = load_agent_work_ledger(
        agent_id=agent.id,
        runtime_task_id=runtime_task.id,
        data_root=tmp_path,
    )
    assert work_ledger is not None
    assert {item["status"] for item in work_ledger["todo_items"]} == {"completed"}
    assert {item["status"] for item in work_ledger["verification"]} == {"completed"}
    assert (agent_root / "evolution" / "evolution_ledger.jsonl").exists()
    assert (agent_root / "evolution" / "evolution_validation_report.json").exists()

    harness_report = audit_agent_harness_snapshot(
        agent=agent,
        runtime_tasks=db.added,
        enabled_trigger_count=1,
        data_root=tmp_path,
    )
    assert harness_report["findings"] == []


async def test_harness_canary_skips_agents_with_existing_harness_evidence(tmp_path):
    from app.services.evolution_ledger import record_evolution_candidate, record_eval_run, record_promotion_decision
    from app.services.evolution_validation import validate_evolution_ledger
    from app.services.harness_canary import run_harness_canary

    agent = _agent()
    trigger = _trigger(agent.id)
    workspace = tmp_path / str(agent.id)
    candidate = record_evolution_candidate(
        workspace,
        target_type="harness_canary",
        target_id="existing",
        diff="+ existing held canary",
        source_attempt_ids=["attempt-1"],
        baseline_version="canary@v1",
        metadata={"harness_canary": True},
    )
    record_eval_run(
        workspace,
        candidate_id=candidate["candidate_id"],
        dataset="harness_canary",
        reward=1.0,
        baseline_reward=1.0,
        passed=True,
        traces=["attempt-1"],
        critical_regressions=0,
        metadata={"harness_canary": True},
    )
    record_promotion_decision(
        workspace,
        candidate_id=candidate["candidate_id"],
        decision="hold",
        reason="Existing canary evidence is enough; no behavior change should be promoted.",
        metadata={"harness_canary": True},
    )
    validate_evolution_ledger(workspace, write_report=True)

    db = _QueuedDB([agent], [trigger])

    result = await run_harness_canary(
        db=db,
        max_agents=10,
        data_root=tmp_path,
    )

    assert result["totals"]["agents_considered"] == 1
    assert result["totals"]["agents_written"] == 0
    assert result["totals"]["skipped"] == 1
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["reason"] == "harness_evidence_already_present"
    assert db.added == []
    assert db.commits == 0
