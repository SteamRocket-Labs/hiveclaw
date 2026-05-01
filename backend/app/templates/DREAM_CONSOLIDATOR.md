# Dream Consolidator — Promotion Decision Contract

<promotion_pipeline>
Dream may propose candidates; it must not treat a proposed promotion as already
applied. Every durable T3/soul change must be representable as a
`memory_promotion_candidate` with source evidence, validation, and rollback.
</promotion_pipeline>

<memory_promotion_candidate_contract>
For every soul promotion candidate, include:
- `source_refs`: minimal pointers to T2/T0/runtime trace evidence.
- `evidence`: one of `tool_verified`, `user_stated`, `system_observed`, `inferred`.
- `novelty`: 0.0-1.0 estimate of non-obviousness.
- `reusability`: 0.0-1.0 estimate of future value.
- `volatility`: one of `ephemeral`, `session`, `project`, `stable`.
- `rollback_ref`: the target file or section before applying the proposed diff.

Never promote `inferred` or `ephemeral` evidence to soul. When evidence is thin,
emit a candidate with decision rationale but expect the ledger gate to hold it.
</memory_promotion_candidate_contract>

