from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
GROUP = ROOT / "docs" / "acceptance" / "2026-08-30-weekend-rc"
LEGACY_REDIRECT = ROOT / "docs" / "wip" / "weekend-release-readiness-and-zero-known-defects-2026-08-25.md"
ISSUE_TEMPLATE_GROUP = ROOT / ".github" / "ISSUE_TEMPLATE"
WEEKEND_WORK_PACKET = ISSUE_TEMPLATE_GROUP / "weekend_rc_work_packet.yml"
PRODUCTION_MANIFEST = ROOT / "acceptance" / "weekend_production_journeys.v1.json"
PRODUCTION_GATE = ROOT / "backend" / "scripts" / "weekend_rc_gate.py"
WORKER_GATE = ROOT / "backend" / "scripts" / "weekend_rc_worker_gate.py"

REQUIRED_FILES = (
    "README.md",
    "01-north-star-and-boundaries.md",
    "02-owner-decisions.md",
    "03-current-status.md",
    "04-journey-ledger.md",
    "05-findings.md",
    "06-runbook-and-release-gates.md",
    "domains/single-agent-and-session.md",
    "domains/memory-knowledge-and-growth.md",
    "domains/hr-identity-and-permissions.md",
    "domains/collaboration-workflow-and-a2a.md",
    "domains/automation-hooks-and-capabilities.md",
    "domains/frontend-and-product-consumption.md",
    "evidence/README.md",
    "archive/README.md",
    "archive/legacy-ledger-2026-08-25.md",
)

REQUIRED_METADATA = {
    "document_id",
    "owner",
    "status",
    "authority",
    "last_reviewed",
    "source_commit",
    "verification_status",
}


def _frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0] == "---", f"{path.relative_to(ROOT)} has no frontmatter"
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise AssertionError(f"{path.relative_to(ROOT)} has unterminated frontmatter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        assert separator and key.strip() and value.strip(), (
            f"{path.relative_to(ROOT)} has invalid frontmatter line: {line!r}"
        )
        metadata[key.strip()] = value.strip()
    return metadata


def _active_documents() -> list[Path]:
    return sorted(path for path in GROUP.rglob("*.md") if "archive" not in path.relative_to(GROUP).parts)


def test_weekend_acceptance_document_group_is_complete_and_indexed() -> None:
    missing = [relative for relative in REQUIRED_FILES if not (GROUP / relative).is_file()]
    assert not missing, f"missing Weekend RC documents: {missing}"

    index = (GROUP / "README.md").read_text(encoding="utf-8")
    for relative in REQUIRED_FILES[1:15]:
        assert f"]({relative})" in index, f"README.md does not index {relative}"


def test_active_documents_have_unique_authority_metadata() -> None:
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    document_ids: list[str] = []
    for path in documents:
        metadata = _frontmatter(path)
        assert REQUIRED_METADATA <= metadata.keys(), (
            f"{path.relative_to(ROOT)} missing metadata: {sorted(REQUIRED_METADATA - metadata.keys())}"
        )
        document_ids.append(metadata["document_id"])

    assert len(document_ids) == len(set(document_ids)), "document_id values must be unique"


def test_active_documents_stay_bounded_and_legacy_history_stays_archived() -> None:
    line_budgets = {
        "README.md": 160,
        "03-current-status.md": 220,
    }
    for path in _active_documents():
        relative = path.relative_to(GROUP).as_posix()
        budget = line_budgets.get(relative, 500)
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= budget, f"{relative} has {line_count} lines; budget is {budget}"

    archive = GROUP / "archive" / "legacy-ledger-2026-08-25.md"
    archive_text = archive.read_text(encoding="utf-8")
    assert len(archive_text.splitlines()) >= 5_000
    assert "历史档案，不是当前恢复入口" in archive_text
    assert len(LEGACY_REDIRECT.read_text(encoding="utf-8").splitlines()) <= 40
    assert "../acceptance/2026-08-30-weekend-rc/README.md" in LEGACY_REDIRECT.read_text(encoding="utf-8")


def test_active_markdown_links_resolve() -> None:
    markdown_link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    failures: list[str] = []

    for path in documents:
        for target in markdown_link.findall(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative_target = target.split("#", 1)[0]
            if not relative_target:
                continue
            resolved = (path.parent / relative_target).resolve()
            if not resolved.exists():
                failures.append(f"{path.relative_to(ROOT)} -> {target}")

    assert not failures, "broken Weekend RC Markdown links:\n" + "\n".join(failures)


def test_journey_ledger_preserves_ci_ids_and_has_one_frozen_denominator() -> None:
    ledger = (GROUP / "04-journey-ledger.md").read_text(encoding="utf-8")
    ci_ids = re.findall(r"^\| (J-\d{2}) \|", ledger, flags=re.MULTILINE)
    candidate_ids = re.findall(r"^\| (PJ-\d{2}) \|", ledger, flags=re.MULTILINE)

    assert ci_ids == [f"J-{index:02d}" for index in range(1, 16)]
    assert candidate_ids == [f"PJ-{index:02d}" for index in range(1, 36)]
    assert PRODUCTION_MANIFEST.is_file()
    assert "frozen-production-denominator-96-p29-padmin-pass1-only" in ledger
    assert "共 **96** 条可独立计分的 production journeys" in ledger
    assert "weekend_production_journeys.v1.json" in ledger
    assert "0/96 Closed；NPTCR 0%" in ledger


def test_structural_checks_do_not_claim_semantic_acceptance() -> None:
    index = (GROUP / "README.md").read_text(encoding="utf-8")
    evidence_contract = (GROUP / "evidence" / "README.md").read_text(encoding="utf-8")
    production_gate = PRODUCTION_GATE.read_text(encoding="utf-8")
    assert "结构检查只验证文件、ID、链接、字段" in index
    assert "不判断语义质量" in index
    assert "frozen-machine-contract-production-evidence-active" in evidence_contract
    assert '"semantic_verdict": "not_computed_by_tool"' in production_gate


def test_execution_control_contract_is_explicit_and_non_semantic() -> None:
    decisions = (GROUP / "02-owner-decisions.md").read_text(encoding="utf-8")
    index = (GROUP / "README.md").read_text(encoding="utf-8")
    runbook = (GROUP / "06-runbook-and-release-gates.md").read_text(encoding="utf-8")

    assert "Kimi Code 负责前端，zCode 负责后端" in decisions
    assert "Codex 是唯一验收总控" in decisions
    assert "当前 `agent-delegation` Skill 是唯一派发协议" in decisions
    assert "取消 zCode/Kimi 分工" not in decisions
    assert "GitHub Issue 只是" in index
    assert "都不是 Journey/Finding verdict" in index
    assert "agent-delegate" in runbook
    assert "初次派发跨 Issue 无状态" in runbook
    assert "同一 Issue correction 必须携带已经核验的诊断" in runbook
    assert "隔离 worktree" in runbook
    assert "`cwd` 只是上下文，不是 OS sandbox" in runbook
    assert "approve-all" in runbook
    assert "--authorization-note" in runbook
    assert "`exit=0` 只表示 transport 返回" in runbook
    assert "任何单字段都不能自动升级为成功" in runbook
    assert "weekend_rc_worker_gate.py preflight" in runbook
    assert "stop_reason 只做分类" in runbook
    assert "semantic_verdict=not_computed_by_tool" in runbook
    assert WORKER_GATE.is_file()
    for decision_id in ("PDEC-001", "PDEC-002", "PDEC-003", "PDEC-004", "PDEC-005", "PDEC-006"):
        assert decision_id in decisions
    assert "一个可独立回滚的共享根因对应一个 Codex integration commit" in runbook
    assert "新增纯 evidence/docs commit `E`" in runbook


def test_weekend_work_packet_template_matches_current_repository_and_boundaries() -> None:
    assert WEEKEND_WORK_PACKET.is_file()
    packet = WEEKEND_WORK_PACKET.read_text(encoding="utf-8")
    issue_templates = "\n".join(path.read_text(encoding="utf-8") for path in sorted(ISSUE_TEMPLATE_GROUP.glob("*.yml")))

    assert "dataelement/hive-agents" not in issue_templates
    assert "https://github.com/SteamRocket-Labs/hiveclaw/issues" in issue_templates
    assert 'labels: ["rc:weekend"]' in packet
    for field_id in (
        "finding_id",
        "journey_ids",
        "worker",
        "base_commit",
        "objective",
        "reproduction",
        "scope",
        "validation",
        "authority",
    ):
        assert re.search(rf"^    id: {field_id}$", packet, flags=re.MULTILINE)

    assert "statelessly in one isolated Git worktree" in packet
    assert "cannot self-accept" in packet
    assert "grants no production, credential, billing, destructive" in packet


def test_active_markdown_fences_are_balanced() -> None:
    documents = _active_documents() + [GROUP / "archive" / "README.md", LEGACY_REDIRECT]
    failures = [
        str(path.relative_to(ROOT))
        for path in documents
        if sum(line.startswith("```") for line in path.read_text(encoding="utf-8").splitlines()) % 2
    ]
    assert not failures, f"unbalanced Markdown fences: {failures}"
