# Agent document access and repair

The supplied diagnostic shows successful model responses followed by an application OutputValidationError: a whole-response schema repair changed the item count. The first rejected field is absent from the diagnostic. Planning, analysis and repair each included the same 72 evidence chunks. Reported totals were 26,813 input and 24,676 output tokens; the private endpoint and user document are not committed.

## Design

Keep Python, LangGraph, local SQLite, existing immutable artifacts, instruction epochs, confirmation gates, 60-minute model deadlines, custom headers and minimal gateway mode. No external vector database or embedding service. Existing waiting graph checkpoints remain usable.

Create a local document workspace: cache immutable evidence by source IDs, expose a bounded catalog, lexical Chinese/English search and explicit reads. The planner may request search/read actions through structured JSON so gateways without native tool calling remain compatible. Reads validate source scope, enforce a text budget, retain paragraph IDs and report omitted content. Planner/feedback/summary contexts use metadata and accepted findings. Downstream generation uses cited, bounded excerpts. Large message sources must not reappear wholesale in request/history/instructions.

Full requirement design examines all supplied business chunks in bounded batches, preserves each accepted batch in SQLite and combines stable IDs deterministically. It does not substitute top-k retrieval for a full requirement review. Evidence can be explicitly classified as non-requirement with a reason, rather than forcing document titles and explanatory text into invented requirements. Cross-batch integration uses accepted summaries/graphs, never the entire source again. Query tasks select relevant evidence; an empty result is explicit, not an arbitrary first-page fallback.

Schema repair receives only the rejected response fragment, the precise validation issue and necessary reference/strategy constraints. It returns a replacement for the single allowed path. The server applies the replacement to its stored draft; list counts, valid stable IDs and unrelated content are preserved. Semantic coverage omissions are handled by a separate additive completion path, not count-changing schema repairs. Store safe field/type diagnostics before every repair and expose actionable recovery text. A bounded repair failure retains its original draft and checkpoint.

Reuse existing AgentPanel insights and SSE for document reads, per-batch progress, reuse and repair summaries. Avoid new selectors or a frontend rebuild. Finish directly when the output is already accepted and coverage is complete, with a substantive final summary.

## Verification

Real FastAPI/LangGraph/SQLite tests with controlled models: reproduce count-changing repair, reject unrelated edits/drops, record original issue, bounded planner and read contexts, unique complete batch coverage, restart cache reuse, scoped search/read including Chinese and no-match, tail-document requirements, summary without raw source, changes/invalidation and old checkpoints. Run the existing backend and frontend suites, startup check, and an independent review before GitHub merge. Do not contact the private endpoint or claim real model validation.
