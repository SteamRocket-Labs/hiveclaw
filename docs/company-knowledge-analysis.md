# Company Knowledge analysis and graph projection

Company Knowledge keeps the original publication and its graph projection separate. A document can remain readable under its publication permissions while unknown types wait for an administrator to confirm their mapping. Extracted candidates are not company facts until reviewed and materialized.

## Configure analysis

In the company's model settings, enable a model and use **Set as Default** to save the selection explicitly. A model shown as an inferred default has not yet been selected for background analysis. Analysis uses the saved model's provider, reasoning settings and output budget; it does not silently select another model when the configured model is unavailable.

Accepted imports enqueue analysis through the same evidence path for direct files, Personal Knowledge promotions and legacy imports. The existing background worker processes these jobs. Analysis reads complete canonical text and an authorized set of retrieved comparison publications. Conflict and duplicate findings describe that retrieved set, not an exhaustive search of all company knowledge.

## Review and publish

Open **Company Knowledge → Review & publish** to inspect coverage, conflicts, possible duplicates and unresolved questions. New document reviews require completed analysis.

When analysis is unavailable, a human with `override_analysis_gate` permission can record a reason to proceed with manual review. This does not create an empty analysis or approve a graph. The publication is marked `skipped_by_override`; a late analysis result does not remove that exception or automatically publish graph facts.

Reviews that already existed before the analysis migration retain their original subjects and decisions. Their documents can continue through the existing review policy; graph analysis requires explicit backfill.

## Confirm unknown types

In **Published knowledge lifecycle**, open **Knowledge graph** for the exact active publication. An administrator can inspect the retained candidate, choose an active installed schema, map each candidate type or property and record a confirmation reason. The confirmation binds the publication's content hash and the exact analysis candidate hash.

If no suitable schema is active, use Company Ontology management to install and validate the domain pack first. The API entry points are:

- `POST /api/knowledge/company/ontology/package-installations`
- `POST /api/knowledge/company/ontology/activations`
- `POST /api/knowledge/company/ontology/activations/{activation_id}/dry-run`

Unknown references remain candidates. Schema validation failures or unresolved analysis findings hold the graph projection without inventing freeform formal types. Materialization reuses Company Ontology objects, assertions, links, releases and evidence bindings. A document projection does not install unrelated package actions or rules.

## Access and recovery

Graph access inherits the current original publication's read and citation permissions. It also requires current access to every comparison source supplied to the model, including sources the model omitted from its semantic citations. Graph access never grants access to those sources or creates an independent permission surface.

Retiring or superseding a publication withdraws its graph. Restoring the original rebuilds the projection when its analysis and schema remain usable. If comparison authority has changed, the original can still be restored while its graph is held. An administrator can explicitly request fresh analysis, then confirm the new candidate before rebuilding the graph.

For existing publications, **Analyze this existing publication** requests one scoped backfill. There is no automatic whole-library re-extraction.

Retry behavior distinguishes execution outcomes:

- Missing configuration holds before a model request is sent.
- Reaching the configured output limit holds the analysis and retains its reply. Increasing the output budget requires an explicit configuration change and retry; analysis does not automatically request a larger generation.
- A rejected request or received invalid candidate can be retried explicitly within the job's attempt limit; invalid replies remain available as quarantined evidence.
- An unknown provider outcome or expired dispatch lease is not automatically resent. Wait for reconciliation or use the explicit review exception where appropriate.
- Replaying the same retry command does not authorize another generation.

Administrators can inspect current tenant job status, error counts, held/failed rates and the recorded proposal override rate at `GET /api/knowledge/company/analysis/metrics`.
