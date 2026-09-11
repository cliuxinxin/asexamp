# Requirement reconciliation implementation

Added `backend/tcg/requirement_reconciliation.py` with the async `reconcile_requirements(engine, run_id, key, items, reports, evidence)` contract. Returns relationships, global_rules, unresolved conflicts, and reconciliation_coverage; does not change the supplied items. Root should invoke only after original extraction uses multiple capacity groups.

Rules contain stable generated REC IDs, type, text, exact requirement_ids and refs. The validator rejects unknown IDs, unsupported reference links and silently resolved conflicts. Model grounding is semantic, while ID validation is deterministic.

The reconciler uses full extracted requirement projections, capacity groups and bounded lexical/adjacent cross-group pairs. Full original chunks are included whenever the complete request fits, otherwise evidence manifests retain provenance and the context explicitly identifies projections. No original chunk or requirement description is sliced. Oversized single projections and unexamined cross-pairs are disclosed. Coverage records each group, requirement and evidence section and never claims all semantic pairs examined.

Each group uses ReliableEngine.validated with a digest of the supplied key, policy, prompt and actual context. Successful earlier groups survive interruption and are reused on retry. `reports` is accepted for call compatibility but intentionally not repeated into model contexts.

Validation: actual Python runtime, production app/store/engine and external FakeModel: `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_reconciliation_v270.py` — 3 passed. Tests cover a linked cross-section definition/threshold exception with preserved IDs, invalid refs/unresolved conflicts, and interrupted multi-group retry reusing accepted work. One upstream Starlette deprecation warning.

Limit: candidate retrieval is not comprehensive semantic pair review; coverage explicitly reports partial scope for multiple capacity groups. This bounded pass does not invent cross-section relationships outside the supplied candidates.
