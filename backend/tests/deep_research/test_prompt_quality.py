from __future__ import annotations


def test_prompt_craft_constants_carry_calibration_and_anti_slop():
    from app.services.deep_research.prompt_craft import REASONING_CALIBRATION, WRITING_QUALITY

    # Reasoning calibration: Toulmin warrant + epistemic status + alternative explanation.
    assert "WARRANT" in REASONING_CALIBRATION
    assert "epistemic status" in REASONING_CALIBRATION.lower()
    assert "alternative" in REASONING_CALIBRATION.lower()
    # Anti-AI-slop: flagged filler words + throat-clearing + rule-of-three.
    assert "throat-clearing" in WRITING_QUALITY.lower()
    assert "delve" in WRITING_QUALITY
    assert "three items" in WRITING_QUALITY.lower()


def test_digest_synthesis_instruction_includes_reasoning_and_writing_quality():
    from app.services.deep_research.synthesis_gates import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    text = build_digest_synthesis_instruction(ResearchRequest(question="Research X"), "English")
    # Integration DNA still present (from C1)…
    assert "INTEGRATION, NOT SUMMARIZATION" in text
    # …now augmented with reasoning calibration + anti-slop writing quality.
    assert "WARRANT" in text
    assert "epistemic status" in text.lower()
    assert "throat-clearing" in text.lower()


def test_digest_synthesis_instruction_mandates_dimension_coverage():
    """RC13: a full run fetched 6 worker lanes but synthesis wrote only 3 Key Findings
    subsections, silently dropping half the dimensions (issuer/use-cases/risk). The instruction
    must require one subsection per worker dimension — complete coverage over single-dimension
    depth — while keeping integration (no per-worker stitching)."""
    from app.services.deep_research.synthesis_gates import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    text = build_digest_synthesis_instruction(ResearchRequest(question="q", depth="full"), "English")
    lowered = text.lower()
    assert "coverage is mandatory" in lowered
    assert "every" in lowered and "dimension" in lowered
    assert "subsection" in lowered
    # coverage must not reintroduce stitching — integration discipline stays
    assert "INTEGRATION, NOT SUMMARIZATION" in text


def test_digest_synthesis_instruction_sets_depth_expectation():
    from app.services.deep_research.synthesis_gates import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    full = build_digest_synthesis_instruction(ResearchRequest(question="q", depth="full"), "English")
    quick = build_digest_synthesis_instruction(ResearchRequest(question="q", depth="quick"), "English")

    # Full depth must explicitly ask for a thorough, multi-section report so synthesis stops
    # under-producing (the production incident: synthesis failed twice below the 1200-char floor).
    assert "DEPTH EXPECTATION" in full
    assert "full" in full.lower()
    assert "multi-section" in full.lower() or "thorough" in full.lower()
    # …but depth is not a licence to pad — anti-filler discipline must remain explicit.
    assert "filler" in full.lower() or "padding" in full.lower()
    # Depth changes the expectation; a quick brief should read as concise.
    assert full != quick
    assert "concise" in quick.lower()


def test_digest_synthesis_instruction_requires_research_report_structure_not_stitching():
    from app.services.deep_research.synthesis_gates import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    text = build_digest_synthesis_instruction(ResearchRequest(question="Research X", depth="full"), "English")
    lowered = text.lower()

    assert "central thesis" in lowered
    assert "evidence matrix" in lowered
    assert "warrant" in lowered
    assert "so-what" in lowered or "so what" in lowered
    assert "worker-by-worker" in lowered
    assert "source-by-source" in lowered
    assert "資料匯編" in text or "资料汇编" in text
