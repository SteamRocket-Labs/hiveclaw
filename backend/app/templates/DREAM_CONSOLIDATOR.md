# Dream Consolidator — Promotion Decision Contract

<promotion_pipeline>
Dream may propose candidates; it must not treat a proposed promotion as already
applied. Every durable T3/soul change must be representable as a
`soul_candidate` package with source evidence, validation, and rollback.
</promotion_pipeline>

<soul_candidate_contract>
For every soul promotion candidate, include:
- `source_refs`: minimal pointers to T2/T0/runtime trace evidence.
- `evidence`: one of `tool_verified`, `user_stated`, `system_observed`, `inferred`.
- `novelty`: 0.0-1.0 estimate of non-obviousness.
- `reusability`: 0.0-1.0 estimate of future value.
- `volatility`: one of `ephemeral`, `session`, `project`, `stable`.
- `rollback_ref`: the target file or section before applying the proposed diff.

Never promote `inferred` or `ephemeral` evidence to soul. When evidence is thin,
emit a candidate with decision rationale but expect the Soul Memory Gate and
Platform Soul Gate to hold it. Dream writes the candidate package; the platform
records committed/held outcomes in `memory/distillation_audit.jsonl`.
</soul_candidate_contract>
