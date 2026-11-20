import json
import logging
import re
from io import StringIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from loguru import logger

from app.core import logging_config
from app.core import request_timing as rt

SITE = r"app\.[A-Za-z0-9_.]+:[A-Za-z0-9_<>]+:\d+"


def finish_statement():
    connection = SimpleNamespace(info={})
    context = SimpleNamespace()
    rt._cursor_started(connection, None, "SELECT 'private SQL sentinel'", ("private parameter",), context, False)
    rt._finish_cursor(context)


@pytest.fixture
def serialized_log_sink(monkeypatch):
    sink = StringIO()
    try:
        with monkeypatch.context() as scoped:
            scoped.delenv("HIVE_LOG_FORMAT", raising=False)
            logging_config.configure_logging(sink=sink)
            yield sink
    finally:
        logging_config.configure_logging()


def emitted_phase_lines(sink):
    logger.complete()
    lines = []
    for line in sink.getvalue().splitlines(keepends=True):
        message = json.loads(line)["record"]["message"]
        if message.startswith("run_phase_sites "):
            lines.append((json.loads(message.removeprefix("run_phase_sites ")), len(line.encode("utf-8"))))
    return lines


def test_sync_phase_restores_outer_phase_on_exception(monkeypatch, caplog, serialized_log_sink):
    signature = "app.db:fetch:10 < app.service:read:20"
    monkeypatch.setattr(rt, "_application_frame", lambda _limit=1: signature)

    @rt.timed_phase("memory_context")
    def inner():
        finish_statement()
        raise RuntimeError("private error sentinel")

    @rt.timed_phase("prompt_build")
    def outer():
        finish_statement()
        with pytest.raises(RuntimeError):
            inner()
        finish_statement()

    caplog.set_level(logging.INFO, logger=rt.__name__)
    with rt.run_timing_scope(uuid4()):
        outer()
        finish_statement()

    phase_lines = emitted_phase_lines(serialized_log_sink)
    line, = [json.loads(message.removeprefix("run_timing ")) for message in caplog.messages
        if message.startswith("run_timing ")]
    expected = {"prompt_build": 2, "memory_context": 1, "pre_model_other": 1}
    assert {phase["phase"]: phase["sites"][signature]["n"] for phase, _ in phase_lines} == expected
    assert [entry["frame"] for entry in line["statements"]] == ["app.db:fetch:10"] * 4
    assert "private" not in json.dumps([line, *phase_lines])


def test_at_most_thirty_sites_keep_the_highest_counts(monkeypatch, serialized_log_sink):
    location = 0
    monkeypatch.setattr(rt, "_application_frame", lambda _limit=1: f"app.db:fetch:{location}")

    with rt.run_timing_scope(uuid4()):
        for location in range(35):
            finish_statement()
        finish_statement()
        finish_statement()

    (bucket, _), = emitted_phase_lines(serialized_log_sink)
    assert bucket["phase"] == "pre_model_other"
    assert (len(bucket["sites"]), bucket["other_n"]) == (30, 5)
    assert next(iter(bucket["sites"].items()))[0] == "app.db:fetch:34"
    assert bucket["sites"]["app.db:fetch:34"]["n"] == 3


def test_site_key_cap_and_serialized_row_bound(monkeypatch, serialized_log_sink):
    location = 0
    frame = f"app.{'long_module_name' * 5}:load"
    signature_pattern = re.compile(rf"{SITE}( < {SITE}){{0,2}}")
    monkeypatch.setattr(
        rt, "_application_frame", lambda _limit=1: " < ".join(f"{frame}:{location + i}" for i in range(3)),
    )
    phases = ("tool_collection", "prompt_build", "memory_context", "hook_dispatch",
        "skill_catalog", "secret_boundary", "model_input_source_validation")

    def statements():
        nonlocal location
        for _ in range(300):
            finish_statement()
            location += 1

    with rt.run_timing_scope(uuid4()) as timing:
        for name in phases:
            rt.timed_phase(name)(statements)()

    phase_lines = emitted_phase_lines(serialized_log_sink)
    assert len(phase_lines) == len(phases)
    assert {phase["phase"] for phase, _ in phase_lines} == set(phases)
    assert timing.instrumentation_errors == 100
    for phase, size in phase_lines:
        assert set(phase) == {"run_id", "phase", "sites", "other_n", "other_db_ms"}
        assert phase["run_id"] == timing.run_id
        assert 0 < len(phase["sites"]) < 30
        assert phase["other_n"] + sum(site["n"] for site in phase["sites"].values()) == 300
        assert phase["other_db_ms"] >= 0
        assert all(signature_pattern.fullmatch(signature) for signature in phase["sites"])
        assert all(set(site) == {"n", "db_ms"} and site["db_ms"] >= 0 for site in phase["sites"].values())
        assert size < 15_000
        assert "private" not in json.dumps(phase)
    print("A2FP12_MAX_PHASE_LINE_BYTES", max(size for _, size in phase_lines))


def test_phase_emission_failure_preserves_run_timing(monkeypatch, caplog):
    def fail(*_args, **_kwargs):
        raise RuntimeError("phase emission failed")

    monkeypatch.setattr(rt._PhaseSites, "summaries", fail)
    caplog.set_level(logging.INFO, logger=rt.__name__)
    with rt.run_timing_scope(uuid4()) as timing:
        finish_statement()

    line, = [json.loads(message.removeprefix("run_timing ")) for message in caplog.messages
        if message.startswith("run_timing ")]
    assert line["run_id"] == timing.run_id
    assert line["db_statements"] == 1
    assert line["instrumentation_errors"] == 0
    assert timing.instrumentation_errors == 1
