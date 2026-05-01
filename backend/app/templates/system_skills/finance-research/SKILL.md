---
name: Finance Research
description: Finance data, valuation, filings, primary-market diligence, IPO pipeline, and research workflow guide
tools:
  - finance_get_provider_status
  - finance_resolve_entity
  - finance_get_source_ledger
  - finance_get_price_history
  - finance_get_financial_statements
  - finance_search_filings
  - finance_get_filing
  - finance_get_ipo_pipeline
  - finance_get_funding_rounds
  - finance_get_company_registry
  - finance_compute_dcf
  - finance_build_comps
  - finance_compile_research_packet
  - finance_run_workflow
is_system: true
---

# Finance Research

<role>
Use this skill when the user asks for public-company analysis, equity research,
DCF valuation, trading comps, filings, IPO pipeline monitoring, primary-market
diligence, portfolio risk review, or an IC memo. This skill turns raw finance
data into source-attributed, reproducible work products.
</role>

<when_to_use>
- User asks to analyze US, Hong Kong, or A-share listed companies.
- User asks for financial statements, price history, filings, source ledger, DCF, comps, or an investment memo.
- User asks for private-market or primary-market diligence, funding rounds, company registry, KYC-style checks, or IPO pipeline.
- User wants a workflow artifact rather than one isolated data lookup.
</when_to_use>

<do_not_use_when>
- The task is generic web research with no finance data requirement.
- The user only wants office document editing; use the matching office skill.
- A paid provider is required but not configured; report the configuration gap instead of inventing data.
</do_not_use_when>

## Credential Boundary

- Finance provider credentials are tenant-scoped tool settings. Do not inspect environment variables or use `run_command` to look for SEC, FMP, Tushare, Wind, iFinD, Choice, Qichacha, Tianyancha, PitchBook, Crunchbase, Capital IQ, Polygon, or EODHD credentials.
- First call `finance_get_provider_status` when provider readiness matters.
- If a paid provider is not configured, continue with public defaults where possible and label missing paid-source coverage as a configuration gap.
- Never claim a paid-source-only field is verified unless the finance tool returns a source ledger record for that field.

## Tool Reference

<tool_reference>

| Need | Tool |
|------|------|
| Check public/paid provider readiness without leaking secrets | `finance_get_provider_status` |
| Normalize company/ticker/entity identity | `finance_resolve_entity` |
| Audit source coverage for a field or entity | `finance_get_source_ledger` |
| Get US/HK/A-share price history | `finance_get_price_history` |
| Get financial statements and free cash flow | `finance_get_financial_statements` |
| Search SEC/HKEX/CNINFO/exchange filings | `finance_search_filings` |
| Read a specific filing and extracted tables | `finance_get_filing` |
| Monitor IPO pipeline | `finance_get_ipo_pipeline` |
| Get funding rounds | `finance_get_funding_rounds` |
| Get company registry / KYC identifiers | `finance_get_company_registry` |
| Run deterministic DCF | `finance_compute_dcf` |
| Build comparable-company snapshot | `finance_build_comps` |
| Compile reusable research packet | `finance_compile_research_packet` |
| Run an end-to-end finance workflow | `finance_run_workflow` |

</tool_reference>

## Workflow

<workflows>

### Listed-company deep dive
Use `finance_run_workflow` with workflow secondary-equity-deep-dive when the user asks for an equity research report or IC memo. It compiles entity, filings, financials, market data, DCF, comps, quality gates, and memo text in one call.

### Manual valuation path
When the user wants control over assumptions:
1. `finance_resolve_entity`
2. `finance_compile_research_packet`
3. `finance_compute_dcf`
4. `finance_build_comps`
5. `finance_get_source_ledger`

### Primary-market diligence
Use `finance_run_workflow` with workflow primary-market-due-diligence. If funding rounds or registry providers require paid credentials, call `finance_get_provider_status` and state which tenant tool setting must be configured.

### IPO pipeline
Use `finance_run_workflow` with workflow ipo-pipeline-monitor for market-level monitoring, or `finance_get_ipo_pipeline` for the raw event list.

</workflows>

## Examples

<examples>

### Example A - Equity research

Input: Analyze Apple and produce a sell-side style view.

Correct flow:
```
finance_run_workflow(workflow="secondary-equity-deep-dive", query="AAPL", region="us", peer_set=["MSFT", "GOOGL"])
```

Output: summarize memo, DCF, comps, quality gates, and source ledger ids. State whether values came from public live providers, static fallback, or configured paid providers.

### Example B - Provider readiness

Input: Can we use Wind and Qichacha for A-share diligence?

Correct flow:
```
finance_get_provider_status()
```

Output: report configured/not configured by provider name only. Never print credential values.

### Example C - Manual DCF

Input: Use 10 percent discount rate and 3 percent terminal growth.

Correct flow:
```
finance_resolve_entity(query="AAPL", region="us")
finance_get_financial_statements(entity_id="entity:us:aapl", market="us")
finance_compute_dcf(financials={...}, assumptions={"discount_rate": 0.10, "terminal_growth_rate": 0.03})
```

</examples>

## Anti-patterns

<anti_patterns>

- ❌ Treating LLM memory or unsourced web snippets as verified financial data.
- ❌ Reporting valuation outputs without assumptions, source ledger, and recomputation path.
- ❌ Using shell commands to find provider credentials instead of tenant-scoped tool settings.
- ❌ Calling only price history for an equity research task and skipping filings/financial statements.
- ❌ Hiding public-source gaps when HK, A-share, private-market, or paid-source coverage is unavailable.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every report-level answer includes source coverage and quality-gate status.
- DCF and comps outputs are reproducible from explicit inputs.
- Paid provider gaps are described as configuration gaps, not data facts.
- US/HK/A-share scope is stated explicitly.
</success_criteria>
