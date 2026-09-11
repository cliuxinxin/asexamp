# Task 3 follow-up: read-only exhaustive source impact

Added registered project.source_impact with read effect and required analysis targeting. It reports affected requirement IDs, known saved downstream candidates, complete requirement/evidence coverage, uncertainty/global flags and immutable input versions. Read-only inline content uses transient references without creating a source; saved sources, artifact revisions and Run controls remain unchanged.

Factored snapshot capture and impact calculation for reuse by project.update_from_sources. Every selected source chunk and every requirement is covered. Oversized requests partition both requirement/evidence axes; an individually oversized evidence chunk is split losslessly with original reference ID plus character offsets. No top-k retrieval is used as evidence of no impact. Minimal unfit inputs fail explicitly. Model refs are checked against only the actual partition supplied.

Long command calculations save validated partition receipts keyed by command, input manifest, effective source roles and exact bounded context. Interrupted retries reuse completed partitions. Update validates the captured artifact/source manifest and current effective source roles before using impact to decide targeted writes; existing global/uncertain escalation and explicit scope-all behavior remain.

Verification using real Store and restored dependencies:

```
PYTHONPATH=backend python -m pytest -q tests/test_framework_impact_v270.py tests/test_conversation_project_v260.py tests/test_framework_turn_v270.py --show-capture=no
```

21 passed. New tests prove read-only behavior (including inline text), all requirement/chunk combinations with byte-for-byte fragment reconstruction, changed-source rejection before update, and interrupted update partition receipt replay. Existing project capability and durable Turn tests pass. git diff --check passed.

Model responses are controlled program tests, not intranet model quality verification. Partition coverage does not prove perfect model reasoning across separate fragments; report explicitly states this limitation and restricts downstream candidates to known saved lineage.
