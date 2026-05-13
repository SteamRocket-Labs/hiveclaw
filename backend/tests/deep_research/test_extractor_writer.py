from __future__ import annotations

import json

import pytest


def test_extractor_removes_fetch_envelope_and_keeps_claims_bounded(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/rwa-report",
        title="RWA Report",
        publisher="Example Research",
        source_type=SourceType.SECONDARY,
        content=(
            "📄 **Fetched content from: https://example.com/rwa-report**\n"
            "Title: Example RWA Report\n"
            "Navigation Login Search Menu Subscribe Contact\n"
            "Tokenized treasury funds are one of the clearest RWA adoption categories in 2026. "
            "Institutional issuers still face custody, liquidity, and regulatory disclosure risks. "
            "This sentence is intentionally extra context that should not make the claim unbounded."
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims
    assert all("Fetched content from" not in claim.text for claim in claims)
    assert all("Navigation Login" not in claim.text for claim in claims)
    assert all(len(claim.text) <= 600 for claim in claims)
    assert all(source.source_id in claim.source_ids for claim in claims)


def test_extractor_does_not_create_synthetic_unsupported_claim_for_empty_source(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/login",
        title="Login",
        publisher="Example",
        source_type=SourceType.SECONDARY,
        content="Login Subscribe Contact Menu Search",
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims == []
    assert ledger.claims == []


def test_extractor_rejects_chinese_page_chrome_and_breadcrumbs(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.cn/article",
        title="专业论文",
        publisher="Example CN",
        source_type=SourceType.SECONDARY,
        content=(
            "登录中 执业证号/统一社会信用代码 密码 中国律师身份核验登录 "
            "会员须知 申请实习证 注销人员证明申请入口 网上投稿 切换新版 "
            "当前位置： 首页 >> 业务研究大厅 >> 专业委员会 >> 专业论文 "
            "研究动态 专业委通知 案例评析 法讯 律师文库 ESG 保险 并购与重组 "
            "This page chrome mentions RWA market risk in 2026 but is not article evidence."
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims == []
    assert ledger.claims == []


def test_extractor_rejects_chinese_directory_menu_claims(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.cn/article",
        title="专业论文",
        publisher="Example CN",
        source_type=SourceType.SECONDARY,
        content=(
            "执业证号/统一社会信用代码(字母小写) 大写锁定已打开 中国律师身份核验登录 "
            "协会介绍 行业党建 行业资讯 业务研究 律师文化 会员服务 法讯 研究成果 "
            "业务指引 专业委信息 要闻·立法动态 ESG | 保险 | 并购与重组 | 财税与海关 | "
            "城市更新（征收） | 调解 | 房地产 | 反垄断与反不正当竞争 | 基金 | "
            "金融工具与金融基础设施 | 劳动与社会保障 | 破产与不良资产."
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims == []
    assert ledger.claims == []


def test_extractor_rejects_pipe_heavy_category_lists(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.cn/categories",
        title="分类导航",
        publisher="Example CN",
        source_type=SourceType.SECONDARY,
        content=(
            "中国律师身份核验登录 业务研究 ESG | 保险 | 并购与重组 | 财税与海关 | "
            "城市更新（征收） | 调解 | 房地产 | 反垄断与反不正当竞争 | "
            "非银行金融 | 公司与商事 | 国际法 | 国际贸易与自贸区 | "
            "数据合规与网络安全 | 数字科技与人工智能."
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims == []
    assert ledger.claims == []


def test_extractor_removes_firecrawl_envelope_and_markdown_images(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/firecrawl",
        title="Firecrawl Result",
        publisher="Example",
        source_type=SourceType.SECONDARY,
        content=(
            "📄 **Firecrawl content from: https://example.com/firecrawl** "
            "![](https://example.com/logo.png) ![](https://example.com/hero.png) "
            "RWA market adoption continued to broaden in 2026 as tokenized fund issuers expanded distribution. "
            "Custody, liquidity, and regulatory disclosure remain material risks for institutional adoption."
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims
    assert all("Firecrawl content from" not in claim.text for claim in claims)
    assert all("![]" not in claim.text for claim in claims)


def test_extractor_drops_article_preamble_before_intro(tmp_path):
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import SourceType

    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/rwa",
        title="RWA Monthly",
        publisher="Example",
        source_type=SourceType.SECONDARY,
        content=(
            "RWA 行业月度全景报告｜宏观政策、机构布局与重点项目解析（2025 年 11 月） "
            "[Starbase Accelerator](https://example.com/author) 收藏文章 订阅专栏 # 引言 "
            "2025 年 11 月，全球真实世界资产（RWA）市场延续稳步增长态势，机构发行人继续扩大代币化基金分销。"
        ),
    )

    claims = extract_claims_from_source(ledger, source)

    assert claims
    assert claims[0].text.startswith("2025 年 11 月")
    assert "Starbase Accelerator" not in claims[0].text
    assert "收藏文章" not in claims[0].text


def test_clean_fetched_text_removes_site_footer_after_article_body():
    from app.services.deep_research.extractor import clean_fetched_text

    cleaned = clean_fetched_text(
        "RWA market adoption broadened in 2026 as tokenized funds expanded. "
        "一键已读 [系统通知](https://example.com/notice) 登录 / 注册 copyright © 2022 - 2026 Example"
    )

    assert "RWA market adoption broadened" in cleaned
    assert "登录 / 注册" not in cleaned
    assert "copyright" not in cleaned.casefold()


@pytest.mark.asyncio
async def test_reader_strips_fetch_envelope_before_persisting_source_content():
    from app.services.deep_research.reader import ResearchReader
    from app.services.deep_research.schemas import SearchCandidate, SourceType

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        assert tool_name == "web_fetch"
        return (
            "📄 **Fetched content from: https://example.com/rwa-report**\n"
            "Title: Example RWA Report\n"
            "Tokenized treasury products are a visible RWA adoption lane in 2026. "
            "Institutional issuers still face custody, liquidity, and regulatory disclosure risks."
        )

    source = await ResearchReader(fake_tool).fetch_candidate(
        SearchCandidate(url="https://example.com/rwa-report"),
        source_type=SourceType.SECONDARY,
    )

    assert source is not None
    assert source.title == "Example RWA Report"
    assert "Fetched content from" not in source.content


def test_writer_writes_supplied_report_markdown_verbatim_without_dumping_content(tmp_path):
    """Tier 1-2: finalize writes the supplied report_markdown verbatim. The writer
    never injects raw source content or auto-generates a pasted evidence ledger."""
    from app.services.deep_research.evaluator import ResearchEvaluator
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.planner import build_research_plan
    from app.services.deep_research.schemas import ResearchRequest, SourceType
    from app.services.deep_research.writer import ResearchArtifactWriter

    request = ResearchRequest(question="Research RWA adoption", max_sources=2)
    plan = build_research_plan(request)
    writer = ResearchArtifactWriter(tmp_path)
    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/rwa-report",
        title="RWA Report",
        publisher="Example Research",
        source_type=SourceType.SECONDARY,
        content=(
            "📄 **Fetched content from: https://example.com/rwa-report**\n"
            "Tokenized treasury products are a visible RWA adoption lane in 2026. "
            "Issuers still face custody, liquidity, and regulatory disclosure risks."
        ),
    )
    extract_claims_from_source(ledger, source)
    evaluation = ResearchEvaluator().evaluate(request=request, ledger=ledger, round_index=1)

    analyst_report = (
        "# RWA Brief\n\n"
        "## Executive Thesis\n\n"
        f"Issuer A growth is backed by source `{source.source_id}`.\n\n"
        "## Source Ledger\n\n"
        f"- `{source.source_id}` Issuer A 2026 disclosure\n"
    )
    writer.finalize(
        request=request,
        plan=plan,
        ledger=ledger,
        evaluation=evaluation,
        status="completed",
        report_markdown=analyst_report,
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))

    assert "Fetched content from" not in report
    assert source.content not in report
    assert f"`{source.source_id}`" in report
    assert "Source Ledger" in report
    assert final["claims"][0]["source_ids"] == [source.source_id]


def test_writer_failure_notice_does_not_dump_raw_source_content(tmp_path):
    """Tier 1-2: failed runs produce a short failure notice that preserves diagnostics
    (gaps, counts) but never pastes raw source content or the legacy evidence dump."""
    from app.services.deep_research.evaluator import ResearchEvaluator
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.planner import build_research_plan
    from app.services.deep_research.schemas import ResearchRequest, SourceType
    from app.services.deep_research.writer import ResearchArtifactWriter

    request = ResearchRequest(question="Research RWA adoption", max_sources=2)
    plan = build_research_plan(request)
    writer = ResearchArtifactWriter(tmp_path)
    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/rwa-report",
        title="RWA Report",
        publisher="Example Research",
        source_type=SourceType.SECONDARY,
        content=(
            "📄 **Fetched content from: https://example.com/rwa-report**\n"
            "Tokenized treasury products are a visible RWA adoption lane in 2026."
        ),
    )
    extract_claims_from_source(ledger, source)
    evaluation = ResearchEvaluator().evaluate(request=request, ledger=ledger, round_index=1)
    evaluation.gaps.append("Synthesis failed; no user-deliverable report was produced.")

    writer.finalize(
        request=request,
        plan=plan,
        ledger=ledger,
        evaluation=evaluation,
        status="failed",
        report_markdown=None,
    )

    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Synthesis Failed" in report
    assert "Fetched content from" not in report
    assert source.content not in report
    assert "Source-Grounded Findings" not in report
    assert "Evidence coverage spans" not in report


def test_evaluator_flags_full_research_without_source_plurality_or_coverage(tmp_path):
    from app.services.deep_research.evaluator import ResearchEvaluator
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import ResearchRequest, SourceType

    request = ResearchRequest(
        question="Research RWA adoption",
        mode="industry_research",
        depth="full",
        max_sources=8,
    )
    ledger = EvidenceLedger(tmp_path)
    source = ledger.add_source(
        url="https://example.com/rwa-report",
        title="RWA Report",
        publisher="Example Research",
        source_type=SourceType.SECONDARY,
        content="Tokenized treasury products are a visible RWA adoption lane in 2026.",
    )
    extract_claims_from_source(ledger, source)

    result = ResearchEvaluator().evaluate(request=request, ledger=ledger, round_index=1)

    assert result.quality_gates["plurality"] == "failed"
    assert result.quality_gates["completeness"] == "failed"
    assert result.next_queries
    assert any("Fewer than" in gap for gap in result.gaps)


def test_evaluator_requires_broader_coverage_for_full_depth(tmp_path):
    from app.services.deep_research.evaluator import ResearchEvaluator
    from app.services.deep_research.extractor import extract_claims_from_source
    from app.services.deep_research.ledger import EvidenceLedger
    from app.services.deep_research.schemas import ResearchRequest, SourceType

    request = ResearchRequest(
        question="Research RWA adoption",
        mode="industry_research",
        depth="full",
        max_sources=8,
        max_rounds=2,
    )
    ledger = EvidenceLedger(tmp_path)
    for idx, host in enumerate(["one.example", "two.example"], start=1):
        source = ledger.add_source(
            url=f"https://{host}/rwa-report",
            title=f"RWA Report {idx}",
            publisher=host,
            source_type=SourceType.SECONDARY,
            content=f"Tokenized treasury products are a visible RWA adoption lane in 2026 from source {idx}.",
        )
        extract_claims_from_source(ledger, source)

    result = ResearchEvaluator().evaluate(request=request, ledger=ledger, round_index=1)

    assert result.quality_gates["plurality"] == "passed"
    assert result.quality_gates["completeness"] == "failed"
    assert any("coverage" in gap.lower() for gap in result.gaps)
