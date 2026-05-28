from __future__ import annotations


def _seed_ledger(tmp_path, source_ids):
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceRecord, SourceType

    ledger = EvidenceLedger(tmp_path)
    for sid in source_ids:
        ledger.sources[sid] = SourceRecord(
            source_id=sid,
            url=f"https://example.invalid/{sid}",
            title=f"Title {sid}",
            publisher=f"Publisher {sid}",
            source_type=SourceType.UNKNOWN,
            content="",
        )
    return ledger


# ── language resolution & detection ────────────────────────────────────────


def test_detect_language_code_basic():
    from app.services.deep_research.language import detect_language_code

    assert detect_language_code("研究 RWA 在亚洲的合规路径") == "zh"
    assert detect_language_code("Research the RWA launchpad opportunity") == "en"


def test_resolve_output_language_explicit_override_wins():
    from app.services.deep_research.language import resolve_output_language_code
    from app.services.deep_research.schemas import ResearchRequest

    # English question but explicit zh override.
    req = ResearchRequest(question="Research RWA", output_language="中文")
    assert resolve_output_language_code(req) == "zh"


def test_resolve_output_language_falls_back_to_question():
    from app.services.deep_research.language import resolve_output_language_code
    from app.services.deep_research.schemas import ResearchRequest

    assert resolve_output_language_code(ResearchRequest(question="研究 RWA 托管")) == "zh"
    assert resolve_output_language_code(ResearchRequest(question="Research RWA custody")) == "en"


# ── paragraph-level consistency (the gate's core) ──────────────────────────


def test_paragraph_consistency_flags_two_foreign_paragraphs_for_zh_target():
    from app.services.deep_research.language import paragraph_language_consistency

    report = (
        "# 报告标题\n\n"
        "本报告整合了多个来源的证据，围绕 RWA 托管与合规展开分析，并给出关键发现。\n\n"
        "This paragraph is entirely in English and contains well over twelve latin words "
        "describing custody, transfer controls, disclosure and secondary liquidity in detail.\n\n"
        "Another fully English paragraph also exceeds the twelve word threshold and discusses "
        "regulatory exemptions, issuer onboarding, and compliance rails at considerable length.\n"
    )
    ok, foreign = paragraph_language_consistency(report, "zh")
    assert ok is False
    assert foreign >= 2


def test_paragraph_consistency_allows_inline_english_entities_in_chinese():
    from app.services.deep_research.language import paragraph_language_consistency

    report = (
        "# RWA 深度研究\n\n"
        "BlackRock 的 BUIDL 基金在 2026 年 Q4 增长到 17 亿美元，SEC 的 Reg D 506(c) 是主要合规路径。"
        "本段虽然包含 BlackRock、SEC、BUIDL 等英文实体名与 $4.2B 等数字，但整体是中文叙述。\n\n"
        "Securitize 与 Republic Forge 等平台在 28 次发行中累计超过 42 亿美元交易量，托管由 BNY Mellon 提供。"
        "这一段同样以中文为主，内联英文专有名词不应被判为混语。\n"
    )
    ok, foreign = paragraph_language_consistency(report, "zh")
    assert ok is True
    assert foreign == 0


def test_paragraph_consistency_flags_chinese_paragraphs_in_english_report():
    from app.services.deep_research.language import paragraph_language_consistency

    report = (
        "# Report\n\n"
        "This English executive thesis integrates evidence across sources on custody and liquidity.\n\n"
        "本段是完整的中文句子，描述了托管、转让限制、信息披露与二级流动性的关键机制与风险。\n\n"
        "这是另一段完整中文叙述，进一步说明监管豁免、发行人尽调与合规通道的具体安排与影响。\n"
    )
    ok, foreign = paragraph_language_consistency(report, "en")
    assert ok is False
    assert foreign >= 2


# ── worker prompt language + integration contract ──────────────────────────


def test_worker_prompt_pins_language_and_integration_contract():
    from app.services.deep_research.schemas import ResearchRequest
    from app.services.deep_research.worker import _build_worker_prompt, _build_worker_system_prompt

    zh_req = ResearchRequest(question="研究 RWA 托管合规")
    prompt = _build_worker_prompt(zh_req, "custody lane")
    assert "Chinese" in prompt
    assert "Never mix languages" in prompt
    # integration / anti-cherry-pick contract
    assert "INTEGRATED" in prompt
    assert "do not summarize each page" in prompt
    assert "do not cherry-pick" in prompt.lower()

    en_req = ResearchRequest(question="Research RWA custody compliance")
    sys_prompt = _build_worker_system_prompt(en_req, "custody lane")
    assert "English" in sys_prompt
    assert "Integrate findings across sources" in sys_prompt


# ── synthesis instruction: integration not summarization + language ────────


def test_synthesis_instruction_has_integration_antipatterns_and_language():
    from app.services.deep_research.reasoner import build_digest_synthesis_instruction
    from app.services.deep_research.schemas import ResearchRequest

    req = ResearchRequest(question="研究 RWA 行业格局", mode="industry_research")
    text = build_digest_synthesis_instruction(req, "Chinese (简体中文)")
    assert "INTEGRATION, NOT SUMMARIZATION" in text
    assert "Sequential summarization" in text
    assert "Cherry-picking" in text
    assert "Chinese" in text
    assert "Never mix languages" in text


# ── orchestrator gate rejects mixed-language report ────────────────────────


def test_synthesis_gate_rejects_mixed_language_report(tmp_path):
    from app.services.deep_research.orchestrator import _evaluate_synthesis_quality
    from app.services.deep_research.schemas import ResearchRequest

    source_ids = ["src_aaaaaaaaaaaa", "src_bbbbbbbbbbbb", "src_cccccccccccc", "src_dddddddddddd"]
    ledger = _seed_ledger(tmp_path, source_ids)
    # Chinese question → target zh, but body has two full English paragraphs.
    request = ResearchRequest(question="研究 RWA 行业格局", mode="industry_research", depth="standard")
    report = (
        "# RWA 行业研究\n\n"
        "## Executive Thesis\n\n"
        "This entire executive thesis paragraph is written in English with far more than twelve words, "
        f"covering custody, compliance, and liquidity across jurisdictions. Sources {source_ids[0]}, {source_ids[1]}.\n\n"
        "## Key Findings\n\n"
        "This findings paragraph is also fully English and exceeds the twelve word threshold while citing "
        f"the evidence ledger entries {source_ids[2]} and {source_ids[3]} for traceability.\n\n"
        "## Source Ledger\n\n"
        f"- `{source_ids[0]}` issuer\n- `{source_ids[1]}` regulator\n"
    )

    state, gap = _evaluate_synthesis_quality(report, request=request, ledger=ledger)

    assert state == "failed"
    assert "language" in gap.lower()
