from __future__ import annotations

from collections import Counter
from pathlib import Path
import re
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT = REPO_ROOT / "docs/agent-native-unified-atomic-review-2026-07-14.md"
PROMPT = REPO_ROOT / "docs/reusable-agent-native-atomic-review-prompt.md"
CONTEXT_CONTRACT = REPO_ROOT / "docs/unified-context-assembly-and-progressive-disclosure-2026-07-14.md"
SESSION_CONTRACT = REPO_ROOT / "docs/session-v2-cc-codex-alignment-contract-2026-07-14.md"
DOC_INDEX = REPO_ROOT / "docs/README.md"

REQUIRED_TRUTH_DOCS = (
    REPORT,
    PROMPT,
    CONTEXT_CONTRACT,
    SESSION_CONTRACT,
)
EXPECTED_GROUP_COUNTS = {
    0: 0,
    1: 16,
    2: 14,
    3: 7,
    4: 6,
    5: 2,
    6: 10,
    7: 1,
    8: 9,
    9: 19,
    10: 19,
}
EXPECTED_SEVERITY_COUNTS = Counter({"P1": 37, "P2": 36, "P3": 29, "P0": 1})


def _region(source: str, name: str) -> str:
    match = re.search(
        rf"<!-- {re.escape(name)}-start -->(.*?)<!-- {re.escape(name)}-end -->",
        source,
        re.DOTALL,
    )
    assert match is not None, f"missing machine-readable region: {name}"
    return match.group(1)


def _git_tracked_paths() -> set[str]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return set(completed.stdout.splitlines())


def test_repair_truth_documents_are_tracked_and_indexed() -> None:
    tracked = _git_tracked_paths()
    docs_index = DOC_INDEX.read_text(encoding="utf-8")

    for path in REQUIRED_TRUTH_DOCS:
        relative = path.relative_to(REPO_ROOT).as_posix()
        assert relative in tracked, f"repair truth document is not Git-tracked: {relative}"
        assert path.name in docs_index, f"repair truth document is absent from docs/README.md: {path.name}"


def test_canonical_ledger_has_exactly_one_owner_group_per_leaf() -> None:
    source = REPORT.read_text(encoding="utf-8")
    ledger_rows = re.findall(
        r"^- (P[0-3]) \| ([A-Za-z0-9-]+) \|",
        _region(source, "canonical-ledger"),
        re.MULTILINE,
    )
    owner_rows = [
        (int(group), leaf_id)
        for group, leaf_id in re.findall(
            r"^- Group (\d+) \| ([A-Za-z0-9-]+)$",
            _region(source, "group-owner-map"),
            re.MULTILINE,
        )
    ]

    ledger_ids = [leaf_id for _, leaf_id in ledger_rows]
    owner_ids = [leaf_id for _, leaf_id in owner_rows]

    assert len(ledger_ids) == len(set(ledger_ids)) == 103
    assert Counter(severity for severity, _ in ledger_rows) == EXPECTED_SEVERITY_COUNTS
    assert Counter(owner_ids) == Counter(ledger_ids)
    assert all(count == 1 for count in Counter(owner_ids).values())
    assert {
        group: sum(1 for owner_group, _ in owner_rows if owner_group == group)
        for group in range(11)
    } == EXPECTED_GROUP_COUNTS


def test_missing_design_decisions_and_evidence_index_are_total() -> None:
    source = REPORT.read_text(encoding="utf-8")
    missing_rows = [
        (int(group), missing_id)
        for group, missing_id in re.findall(
            r"^\| (\d+) \| `([^`]+)` \|",
            _region(source, "missing-owner-map"),
            re.MULTILINE,
        )
    ]
    evidence_rows = [
        (int(group), int(leaves), int(missing))
        for group, leaves, missing in re.findall(
            r"^\| (\d+) \| .*? \| (\d+) leaf / (\d+) Missing \|",
            _region(source, "group-evidence-index"),
            re.MULTILINE,
        )
    ]

    assert len(missing_rows) == len({missing_id for _, missing_id in missing_rows}) == 5
    missing_counts = {
        group: sum(1 for owner_group, _ in missing_rows if owner_group == group)
        for group in range(11)
    }
    assert len(evidence_rows) == 11
    assert {
        group: (leaf_count, missing_count)
        for group, leaf_count, missing_count in evidence_rows
    } == {
        group: (EXPECTED_GROUP_COUNTS[group], missing_counts[group])
        for group in range(11)
    }

    context_decisions = re.findall(
        r"^- CTX-([A-F]) \| Group (\d+) \|",
        _region(source, "context-decision-map"),
        re.MULTILINE,
    )
    session_decisions = re.findall(
        r"^- S-(\d{2}) \| Group (\d+) \|",
        _region(source, "session-decision-map"),
        re.MULTILINE,
    )
    golden_cases = re.findall(
        r"^- SESSION-G(\d+) \| Group (\d+) \|",
        _region(source, "session-golden-map"),
        re.MULTILINE,
    )

    assert [decision for decision, _ in context_decisions] == list("ABCDEF")
    assert [decision for decision, _ in session_decisions] == [f"{number:02d}" for number in range(1, 13)]
    assert [int(case) for case, _ in golden_cases] == list(range(1, 14))


def test_every_group_has_an_executable_document_and_evidence_route() -> None:
    source = REPORT.read_text(encoding="utf-8")
    group_matches = list(re.finditer(r"^### Group (\d+)：", source, re.MULTILINE))

    assert [int(match.group(1)) for match in group_matches] == list(range(11))
    for index, match in enumerate(group_matches):
        group = int(match.group(1))
        end = (
            group_matches[index + 1].start()
            if index + 1 < len(group_matches)
            else source.index("\n## 10.", match.start())
        )
        section = source[match.start() : end]
        required = (
            "**依赖 Group**",
            "**AA 开工入口**",
            "**@原始断点证据**",
            "@必须先读",
            "**首个 Red**",
            "**证据回填**",
            "**退出门**",
            f"EVID-G{group}-*",
            "**执行**" if group == 0 else "**源码入口**",
        )
        assert all(marker in section for marker in required), f"Group {group} is missing an executable route"
        assert f"@{CONTEXT_CONTRACT.relative_to(REPO_ROOT).as_posix()}" in section, (
            f"Group {group} does not route to the Context contract"
        )
        assert f"@{SESSION_CONTRACT.relative_to(REPO_ROOT).as_posix()}" in section, (
            f"Group {group} does not route to the Session contract"
        )


def test_repair_ledger_defines_context_read_receipt_and_evidence_round_trip() -> None:
    source = REPORT.read_text(encoding="utf-8")

    for required in (
        "`AA → 上下文包 → 施工 → 证据` 闭环合同",
        "context_read_receipt:",
        'aa_entry: "§9 Group <n> + §12.1/§12.2 owner rows"',
        'role: "authority | design | original_evidence | migration | acceptance"',
        'evidence_sink: "EVID-G<group>-<序号>"',
        "先在 `§12.4` 创建或更新稳定 `EVID-G<group>-<序号>`",
    ):
        assert required in source


def test_document_routes_are_portable_and_external_refs_are_snapshot_bound() -> None:
    source = REPORT.read_text(encoding="utf-8")
    references = set(re.findall(r"`@([^`]+\.md)`", source))
    local_references = {reference for reference in references if reference.startswith("docs/")}
    external_references = {
        reference.removeprefix("hive-connect:")
        for reference in references
        if reference.startswith("hive-connect:")
    }

    assert not any(reference.startswith("/") for reference in references), (
        "@document routes must not depend on one developer's absolute filesystem path"
    )
    assert all((REPO_ROOT / reference).is_file() for reference in local_references)

    external_rows = re.findall(
        r"^- hive-connect \| ([0-9a-f]{40}) \| ([^|]+?) \| ([0-9a-f]{64})$",
        _region(source, "external-doc-registry"),
        re.MULTILINE,
    )
    assert external_rows
    assert {path.strip() for _, path, _ in external_rows} == external_references
    assert len({commit for commit, _, _ in external_rows}) == 1


def test_prompt_and_critical_design_docs_bind_back_to_the_repair_ledger() -> None:
    prompt = PROMPT.read_text(encoding="utf-8")
    context_contract = CONTEXT_CONTRACT.read_text(encoding="utf-8")
    session_contract = SESSION_CONTRACT.read_text(encoding="utf-8")

    for required in (
        "终极施工文档与 `@文档路由` 合同",
        "每个 `canonical_leaf_id` 恰好属于一个 owner Group",
        "每个 Missing 恰好属于一个建设 Group",
        "稳定证据锚点",
        "机器门禁",
    ):
        assert required in prompt

    for contract in (context_contract, session_contract):
        assert "施工消费合同" in contract
        assert "必须回填总报告" in contract


def test_every_extreme_scenario_and_liveness_gate_has_one_primary_group() -> None:
    report = REPORT.read_text(encoding="utf-8")
    prompt = PROMPT.read_text(encoding="utf-8")

    prompt_scenarios = re.findall(r"^\| (X-[A-Z0-9-]+) \|", prompt, re.MULTILINE)
    scenario_rows = re.findall(
        r"^- (X-[A-Z0-9-]+) \| Group (\d+) \|",
        _region(report, "extreme-scenario-owner-map"),
        re.MULTILINE,
    )
    prompt_liveness_gates = re.findall(r"^- \*\*LB-(\d+) ", prompt, re.MULTILINE)
    liveness_rows = re.findall(
        r"^- LB-(\d+) \| Group (\d+) \|",
        _region(report, "liveness-gate-owner-map"),
        re.MULTILINE,
    )

    assert len(prompt_scenarios) == len(set(prompt_scenarios))
    assert Counter(scenario for scenario, _ in scenario_rows) == Counter(prompt_scenarios)
    assert all(0 <= int(group) <= 10 for _, group in scenario_rows)
    assert prompt_liveness_gates == [str(number) for number in range(1, 11)]
    assert Counter(gate for gate, _ in liveness_rows) == Counter(prompt_liveness_gates)
    assert all(0 <= int(group) <= 10 for _, group in liveness_rows)


def test_repair_documents_have_stable_markdown_fences_and_whitespace() -> None:
    fence_pattern = re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")

    for path in REQUIRED_TRUTH_DOCS:
        source = path.read_text(encoding="utf-8")
        assert source.endswith("\n")
        assert "\x00" not in source
        assert not [
            line_number
            for line_number, line in enumerate(source.splitlines(), 1)
            if line.endswith((" ", "\t"))
        ]

        opened: tuple[str, int, int] | None = None
        for line_number, line in enumerate(source.splitlines(), 1):
            match = fence_pattern.match(line)
            if match is None:
                continue
            marks = match.group(2)
            trailing = match.group(3).strip()
            if opened is None:
                opened = (marks[0], len(marks), line_number)
            elif marks[0] == opened[0] and len(marks) >= opened[1] and not trailing:
                opened = None
        assert opened is None, f"unclosed Markdown fence in {path}: {opened}"
