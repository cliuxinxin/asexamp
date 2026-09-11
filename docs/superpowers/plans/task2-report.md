# Task 2: shared context and capacity admission

Implemented the approved four integration functions:

- `artifact_context(store, task, artifact, rows, evidence, instruction='', explicit_source_ids=(), extra=None)` returns the direct model context dictionary, including detached artifact/row snapshots, evidence, profile, historical `analysis`/`scenarios`, `global_rules`, `dependency_manifest`, and `coverage`.
- `rule_projection(report, rows=())` projects rules, dependencies, exclusions, precedence, relationships, global rules and unresolved conflicts with stable IDs, original refs and provenance. Unrelated rules are disclosed as omitted. Applicable rules exceeding 24 entries or 16,000 characters reject visibly instead of being silently truncated.
- `analysis_signature(store, source_ids, roles, profile, scope=None)` includes source-content, effective-role and semantic-profile/scope digests.
- `request_budget(directory, task, context, settings=None)` returns `fits`, `window`, `output_tokens`, `input_tokens`, `count_method`, `margin`, `output_limit_mode`, and `request_digest`.

Context packs use recorded ancestor revisions, including per-item revision maps. They include required evidence referenced by selected rows, their actual ancestors, relevant rules, and nested explicit upstream draft/deletion context. Artifact-effective source roles override intrinsic source roles, and artifact-scoped clarification spans are mandatory. Optional new-source retrieval uses English terms and Chinese 2/3-grams, bounded to 12,000 characters, and discloses included/omitted/missing evidence IDs. Explicit `extra.analysis` and `extra.scenarios` stay authoritative for synchronization, with old inputs under `historical_ancestors`; unsaved proposed upstream rows are inline context, not falsely manifested as committed versions.

Estimate contexts contain selected scenarios plus limited design configuration and metadata, with no evidence or historical analysis. Context creation does not change artifacts or Run control policy. Profile snapshots are consumed through immutable artifact digests.

Both `DirectEngine.fits` and final `LangChainGateway` admission use the same message serialization and conservative token estimate, including SYSTEM, task contract and all added context. The final request is checked again after caller-added template/repair fields. Default window/output are 32,768/8,192, with a 512-token safety margin; legacy zero resolves to the finite default window. This estimator is explicitly marked `conservative_estimate`, not a model tokenizer.

Settings support `context_window`, `output_tokens`, `output_limit_mode` (`request` default), and `server_output_tokens`. Request mode sends the output allowance; server mode requires a declared enforced server cap and budgets against it while omitting the request output parameter. Existing custom `/api/v1/chat/completions` URL construction and auth/header protocol remain intact. Recorder entries include final budget, allowlisted explicit parameters and a digest of final messages/invocation identity; secrets are not included in the digest.

Validation:

- `PYTHONPATH=backend /workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q tests/test_framework_context_v270.py`: **10 passed**.
- Combined with `tests/test_backend_model.py` and `tests/test_model_request_inspection.py`: **19 passed, 2 failed** at time of this task's validation. Both failures are obsolete assertions that default OpenAI requests contain no `max_tokens`/recorded parameters; the approved default changed to an explicit output limit. Root owns updating those legacy assertions and integrated rerun.
- Tests exercise real SQLite, installed LangChain/Ollama transports and local HTTP test servers, plus a controlled `httpx` transport for admission tests. No actual intranet model endpoint was verified.

Root integration owns `flow.py`, `workflow.py`, `artifact_actions.py`, settings request schema/UI, and legacy test expectation updates. No files beyond Task 2 ownership were edited by this worker.
