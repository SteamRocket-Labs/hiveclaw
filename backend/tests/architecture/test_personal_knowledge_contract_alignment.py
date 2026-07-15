from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
MATRIX_START = "<!-- personal-kb-read-authority-matrix-start -->"
MATRIX_END = "<!-- personal-kb-read-authority-matrix-end -->"
CONTRACT_DOCS = (
    REPO_ROOT / "docs" / "personal-knowledge-base-spec.md",
    REPO_ROOT / "docs" / "personal-company-knowledge-tool-boundary-2026-07-10.md",
    REPO_ROOT / "docs" / "agent-permission-governance-spec-2026-07-07.md",
)


def _authority_matrix(path: Path) -> str:
    source = path.read_text(encoding="utf-8")
    assert MATRIX_START in source, f"{path.name} is missing the canonical Personal KB read matrix"
    assert MATRIX_END in source, f"{path.name} is missing the canonical Personal KB read matrix terminator"
    return source.split(MATRIX_START, 1)[1].split(MATRIX_END, 1)[0].strip()


def test_personal_knowledge_specs_share_one_read_authority_matrix() -> None:
    matrices = [_authority_matrix(path) for path in CONTRACT_DOCS]

    assert len(set(matrices)) == 1
    matrix = matrices[0]
    assert "Interactive owner-direct turn" in matrix
    assert "explicit grant not required" in matrix
    assert "Autonomous owner Agent" in matrix
    assert "Shared/cross-user/A2A/subagent" in matrix
    assert "unexpired explicit grant" in matrix
    assert "sensitivity ceiling" in matrix
    assert "opaque credential reference only" in matrix


def test_personal_knowledge_tool_descriptions_match_the_canonical_matrix() -> None:
    from app.tools.handlers.knowledge import read_personal_kb, search_personal_kb

    search_description = search_personal_kb.meta.description
    read_description = read_personal_kb.meta.description
    combined = f"{search_description}\n{read_description}"

    assert "Interactive owner turns may read agent_searchable PL1-PL3" in combined
    assert "autonomous, shared, cross-user, A2A, and subagent turns require" in search_description
    assert "unexpired explicit grant" in search_description
    assert "sensitivity ceiling" in combined
    assert "PL4" in combined
    assert "opaque credential reference" in combined
    assert "Results are tenant-, owner-, sensitivity-, and grant-filtered" not in combined
