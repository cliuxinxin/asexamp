# Implementation ledger — v2.7.0

Baseline: 49c24b2. Separate v2.7.0 directory; v2.6.0 preserved.

- Runtime: pinned Python dependencies restored; genuine LangGraph/SQLite and HTTP test runtime available.
- Task 1: immutable revisions, explicit provenance and separate freshness guards, source snapshots, precise parent relations and atomic operation receipts implemented. Kernel review fixes completed.
- Task 2: historical ContextPacks, mandatory rule closure, finite final request budgeting, bounded legacy dialogue and cross-section reconciliation implemented.
- Task 3: durable reference-only Turn LangGraph, capability-owned effects and target contracts, source-impact read capability and context receipts implemented.
- Root integration: main stages consume ContextPacks; semantic analysis reuse; no in-place report writes; guard across generation batches and before commit; draft lineage consumed by linked changes; confirmation binds current revision/control; request/usage receipt hooks wired.
- UI: capacity settings, version-bound continue, source-impact card. Wide results and compact composer retained.
- Verification: final focused backend gate 101 passed; frontend 5 passed; production build and diff check passed. Recovery, generation epoch, capacity grouping and confirmation review findings fixed. Controlled model responses do not establish intranet model routing reliability.
- Delivery: Chinese continuous demo guide and validation report complete; release archive prepared with code, built frontend and demo documents.
