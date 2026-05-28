# Deep Research Playbooks

Use this reference only when reviewing or diagnosing Deep Research v2 artifacts.
Execution still belongs to `deep_research_run` / `deep_research_start`, not a
manual web-search workflow.

## V2 Artifact Flow

1. Planner selects `topic_deep_dive`, `industry_research`, or `source_ledger_audit`.
2. The orchestrator fans out bounded worker topics. Workers may browse only through
   governed read-only web tools inside the runtime.
3. Worker digests and fetched source metadata are persisted in `worker_reports.jsonl`.
4. The parent ledger assigns durable `src_*` ids in `sources.jsonl`, extracts claims
   into `claims.jsonl`, writes structured `source_notes.jsonl`, and aggregates
   `lane_summaries.jsonl`.
5. Final writing uses `synthesize_from_digests`; unknown `src_*` citations fail the
   synthesis gate instead of being shown to the user.

## Topic Deep Dive

Use for a bounded question, product, company, policy, event, or technical
topic.

Required checks:

- Define the exact question and decision context.
- Identify the highest-authority primary sources first.
- Separate current facts from historical background.
- Build a claim ledger before writing conclusions.
- Review `worker_reports.jsonl`, `source_notes.jsonl`, and `lane_summaries.jsonl`
  before judging the final report.
- Include contradictions, stale sources, and what remains unknown.

Recommended source lanes:

- Official source or documentation.
- Regulator, standards body, or legal source when relevant.
- First-party data, filings, release notes, or changelogs.
- High-quality secondary source for context.

## Industry Research

Use for market landscape, value chain, competitors, customer segments,
regulation, adoption, and risk analysis.

Required checks:

- Define market boundaries and adjacent markets.
- Split players by role, segment, or buyer instead of listing logos.
- Track source date because market maps drift quickly.
- Separate observed adoption signals from analyst estimates.
- Explain what would change the conclusion.
- Check that market-size, segment, and competitor claims cite source ids that resolve
  to `sources.jsonl`.

Recommended sections:

- Market definition and exclusions.
- Value chain and buyer map.
- Competitor groups and positioning.
- Adoption, demand, and distribution signals.
- Regulation, technical constraints, and risk drivers.
- Evidence ledger and unresolved gaps.

## Source Ledger Audit

Use when the user provides a draft, report, memo, deck, or claim list and
asks whether it is supported.

Required checks:

- Read the draft and any existing ledger first.
- Fetch cited URLs; do not trust citation text without source content.
- Classify each material claim as `verified`, `inferred`, `contradicted`,
  `stale`, or `unsupported`.
- Lead the output with blocking claims, not prose polish.
- Suggest replacement wording where a claim is too strong for the evidence.
- Treat any unknown `src_*` as a blocking citation defect.

Audit table:

| Claim | Current Citation | Status | Problem | Recommended Fix |
|---|---|---|---|---|
