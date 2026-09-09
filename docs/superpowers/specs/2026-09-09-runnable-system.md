# TCG 2.2 runnable system

User authorization: build the system from the previously discussed PRD and recommendations.

Deliver a standalone Python 3.11+ application with a prebuilt React frontend, .env.example,
Docker configuration, Chinese startup guide and regression tests. Retain the reference UI's
white/gray layout and rose accent. Never generate pretend cases in the production gateway.

Architecture: retain FastAPI, SQLite durable storage and LangGraph. Add a fixed reliable
workflow for new UI runs. Phases: route, exhaustive chunk analysis, optional clarification,
batched scenarios, optional scenario approval, batched cases, one logical batched review,
atomic publication. Cache accepted work units in SQLite so process restart or retry resumes
unfinished work. Existing legacy routes remain available for backward compatibility.

Gateway: POST the configured /api/v1/chat/completions endpoint using X-API-Key and content
block arrays. No mandatory temperature, JSON mode, tools or streaming. Real model only.
Network retries have a single owner. Configure request deadlines; reject truncated/empty output.
Validation collects all invalid references in a batch, bounds corrections, preserves legal items.

Document limits: configurable upload limit default 100 MiB, extracted text 2 million characters.
Native parsers preserve coordinates; optional Docling for scan parsing with explicit installation.
No silent truncation. Unknown role is provisionally classified with visible rationale and manual
correction. Long evidence is partitioned without losing characters or source provenance.

Generation configuration: visible Business/Negative/Boundary type selection and quick/standard/deep
depth before a run; immutable per-run profile snapshot. User-confirmed project memory is stored,
editable/deletable and used in new chats; never shared across projects. Artifacts remain editable,
versioned and exportable to XLSX. UI surfaces phase and completed work-unit counts and retry.

Scope: runnable local demonstration system, single process with restart recovery. Hatchet
is not required for installation because current SQLite/worker can implement this bounded deployment.
This is not a claim of enterprise SSO/ACL or distributed workers. Bind loopback only in this release. Remote host access is deferred together with authentication;
this narrows an implementation option, not an explicit user requirement.

Acceptance: actual HTTP protocol tested with local HTTP fixture; full upload→XLSX flow; three
bad references repaired together; durable retry does not repeat accepted batches; restart recovery;
long input partitions; settings precedence; project memory isolation; frontend build/UI inspection.
The private model cannot be claimed verified without its actual key/network access.
