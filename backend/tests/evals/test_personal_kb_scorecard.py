from __future__ import annotations

import json
from pathlib import Path


def test_score_personal_kb_benchmark_compares_hive_and_sag_trace() -> None:
    from app.evals.personal_kb_scorecard import score_personal_kb_benchmark

    report = score_personal_kb_benchmark(
        {
            "cases": [
                {
                    "id": "notebook-refs",
                    "query": "How does Open Notebook preserve source references?",
                    "expected_refs": ["seg-a", "seg-b"],
                    "forbidden_refs": ["private-a"],
                    "k": 2,
                },
                {
                    "id": "acl-boundary",
                    "query": "Which private agent note should be hidden?",
                    "expected_refs": ["seg-c"],
                    "forbidden_refs": ["private-c"],
                    "k": 3,
                },
            ],
            "providers": {
                "hive": [
                    {
                        "case_id": "notebook-refs",
                        "items": [{"source_ref": "seg-a"}, {"source_ref": "seg-b"}],
                        "latency_ms": 100,
                        "cost_usd": 0.01,
                    },
                    {
                        "case_id": "acl-boundary",
                        "items": [{"source_ref": "seg-c"}, {"source_ref": "private-c"}],
                        "latency_ms": 200,
                        "cost_usd": 0.02,
                    },
                ],
                "sag_trace": [
                    {
                        "case_id": "notebook-refs",
                        "items": [{"source_ref": "seg-a"}],
                        "latency_ms": 50,
                        "cost_usd": 0.0,
                    },
                    {
                        "case_id": "acl-boundary",
                        "items": [],
                        "latency_ms": 70,
                        "cost_usd": 0.0,
                    },
                ],
            },
        }
    )

    assert report["benchmark"] == "personal_kb_sag_scorecard"
    assert report["version"] == 1
    hive = report["providers"]["hive"]
    assert hive["provider_kind"] == "hive_native"
    assert hive["case_count"] == 2
    assert hive["recall_at_k"] == 1.0
    assert hive["citation_accuracy"] == 0.75
    assert hive["acl_leakage_count"] == 1
    assert hive["acl_leakage_zero"] is False
    assert hive["latency_ms_avg"] == 150
    assert hive["cost_total_usd"] == 0.03
    assert report["hard_gates"]["acl_leakage_zero"] is False
    assert report["providers"]["sag_trace"]["provider_kind"] == "comparison_trace"
    assert report["providers"]["sag_trace"]["recall_at_k"] == 0.25


def test_score_personal_kb_benchmark_accepts_personal_kb_result_shapes() -> None:
    from app.evals.personal_kb_scorecard import score_personal_kb_benchmark

    report = score_personal_kb_benchmark(
        {
            "cases": [
                {
                    "id": "real-shape",
                    "query": "What source segment backs the document?",
                    "expected_refs": ["seg-1"],
                    "forbidden_refs": ["private-seg"],
                    "k": 5,
                }
            ],
            "providers": {
                "hive": [
                    {
                        "case_id": "real-shape",
                        "items": [
                            {
                                "source_ref": "kb://person/u/documents/doc-1#segment=seg-1",
                                "source_refs": [{"document_id": "doc-1", "segment_id": "seg-1"}],
                                "score_trace": {"channels": {"text": {"raw_score": 0.9}}},
                            },
                            {
                                "refs": ["private-seg"],
                            },
                        ],
                    }
                ]
            },
        }
    )

    case_report = report["providers"]["hive"]["cases"]["real-shape"]
    assert case_report["retrieved_refs"][:2] == ["kb://person/u/documents/doc-1#segment=seg-1", "seg-1"]
    assert case_report["recall_at_k"] == 1.0
    assert case_report["citation_accuracy"] == 0.5
    assert case_report["acl_leakage_count"] == 1


def test_run_personal_kb_scorecard_script_writes_report_and_enforces_acl_gate(tmp_path: Path) -> None:
    from app.scripts.run_personal_kb_scorecard import main

    input_path = tmp_path / "scorecard-input.json"
    output_path = tmp_path / "scorecard-output.json"
    input_path.write_text(
        json.dumps(
            {
                "cases": [{"id": "ok", "query": "q", "expected_refs": ["seg-ok"], "forbidden_refs": [], "k": 3}],
                "providers": {"hive": [{"case_id": "ok", "items": [{"source_ref": "seg-ok"}]}]},
            }
        ),
        encoding="utf-8",
    )

    assert main(["--input", str(input_path), "--output", str(output_path), "--fail-on-acl-leakage"]) == 0
    written = json.loads(output_path.read_text(encoding="utf-8"))
    assert written["hard_gates"]["acl_leakage_zero"] is True

    input_path.write_text(
        json.dumps(
            {
                "cases": [
                    {"id": "leak", "query": "q", "expected_refs": ["seg-ok"], "forbidden_refs": ["private-seg"], "k": 3}
                ],
                "providers": {"hive": [{"case_id": "leak", "items": [{"source_ref": "private-seg"}]}]},
            }
        ),
        encoding="utf-8",
    )

    assert main(["--input", str(input_path), "--output", str(output_path), "--fail-on-acl-leakage"]) == 2
