import json
import re
from time import perf_counter
from types import SimpleNamespace
from uuid import uuid4

import pytest
from loguru import logger

from app.core import request_timing as rt

SITE = r"app\.[A-Za-z0-9_.]+:[A-Za-z0-9_<>]+:\d+"


def finish_statement():
    connection = SimpleNamespace(info={})
    context = SimpleNamespace()
    rt._cursor_started(connection, None, "SELECT 'private SQL sentinel'", ("private parameter",), context, False)
    rt._finish_cursor(context)


def candidate_row(timing):
    row = timing.summary(perf_counter())
    return {**row, "phase_sites": timing.phase_sites_summary(row)}


def test_sync_phase_restores_outer_phase_on_exception(monkeypatch):
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

    with rt.run_timing_scope(uuid4()) as timing:
        outer()
        finish_statement()

    line = candidate_row(timing)
    expected = {"prompt_build": 2, "memory_context": 1, "pre_model_other": 1}
    assert {name: phase["sites"][signature]["n"] for name, phase in line["phase_sites"].items()} == expected
    assert [entry["frame"] for entry in line["statements"]] == ["app.db:fetch:10"] * 4
    assert "private" not in json.dumps(line)


def test_at_most_thirty_sites_keep_the_highest_counts(monkeypatch):
    location = 0
    monkeypatch.setattr(rt, "_application_frame", lambda _limit=1: f"app.db:fetch:{location}")

    with rt.run_timing_scope(uuid4()) as timing:
        for location in range(35):
            finish_statement()
        finish_statement()
        finish_statement()

    bucket = candidate_row(timing)["phase_sites"]["pre_model_other"]
    assert (len(bucket["sites"]), bucket["other_n"]) == (30, 5)
    assert next(iter(bucket["sites"].items()))[0] == "app.db:fetch:34"
    assert bucket["sites"]["app.db:fetch:34"]["n"] == 3


def test_site_key_cap_and_serialized_row_bound(monkeypatch):
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

    line = candidate_row(timing)
    assert set(line["phase_sites"]) == set(phases)
    assert line["instrumentation_errors"] == 100
    for phase in line["phase_sites"].values():
        assert set(phase) == {"sites", "other_n", "other_db_ms"}
        assert 0 < len(phase["sites"]) <= 30
        assert phase["other_n"] + sum(site["n"] for site in phase["sites"].values()) == 300
        assert phase["other_db_ms"] >= 0
        assert all(signature_pattern.fullmatch(signature) for signature in phase["sites"])
        assert all(set(site) == {"n", "db_ms"} and site["db_ms"] >= 0 for site in phase["sites"].values())
    payload = json.dumps(line, separators=(",", ":"))
    message = f"run_timing {payload}"
    lines = []
    handler = logger.add(lines.append, serialize=True, filter=lambda record: record["message"] == message)
    try:
        logger.info("{}", message)
    finally:
        logger.remove(handler)
    envelope, = lines
    print("A2FP12_ROW_BYTES", len(payload.encode()), len(envelope.encode()))
    assert len(envelope.encode()) < 64_000
    assert "private" not in payload
