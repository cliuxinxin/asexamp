# v2.6.0 integration verification

The integration suite uses the actual `Store`, persisted revisions, controller,
registered capability services, and XLSX files. Semantic model outputs are
controlled. Seeded committed artifacts are explicitly identified; this suite
does not emulate LangGraph transitions or certify a real model provider.

## Runtime recovery

- Available interpreter: `/opt/codex/runtimes/codex-primary-runtime/dependencies/python/bin/python` (Python 3.12).
- Available: standard-library SQLite/unittest, Pydantic, OpenPyXL, document helpers.
- Missing: pytest, FastAPI, httpx, LangGraph and their supporting packages.
- Searched existing release directories, `/workspace`, `/root/.cache`, `/opt`,
  `/usr/local`, and `/tmp` for virtual environments, installed package trees and
  cached wheels. No reusable backend runtime or package cache was present.
- Created an isolated external test environment with system packages visible;
  the ordinary pip installation attempt could not execute. Tool result:
  `network approval was cancelled before a decision was returned`.
  No escalation or alternate network path was attempted, and no system Python
  environment was modified.

Usable dependency-light command from the release directory:

```bash
python -m unittest discover -s tests -p 'test_conversation_e2e_v260.py' -v
```

Required full-runtime command when dependencies are available:

```bash
python -m pytest tests/test_conversation_e2e_v260.py tests/test_conversation_v260.py tests/test_gateway_workflow_http.py -q
```

The second command is a remaining gate; it has not passed in this environment.

## Failure-first observation

The initial integration invocation failed to import `tcg.conversation` before
the controller implementation existed. The fixture uses real `Store.artifact`
commits, revision snapshots, reference identifiers and parent relationships.
Unexpected semantic model tasks fail loudly.

## Final focused result

`python -m unittest discover -s tests -p 'test_conversation_e2e_v260.py' -v`
completed successfully on 2026-09-11: **6 tests passed**. This includes the complete
16-message seeded service chain plus five integration boundary scenarios below.
The final source-change check confirms both public/private input revisions advance
and the waiting run receives new source IDs and roles.

## Baseline checks

- `test_chat_estimate_v2513.py`: 20 tests passed before new adapters landed.
- `test_project_context_v2512.py`: 5 tests passed and 1 failed at line 131.
  Reproduced the identical failure in untouched v2.5.13: the test installs a
  project-key workspace lock, while the existing store checks a chat-key lock.
  This is a preexisting test/lock-scope mismatch, not a new integration finding.

## Continuous acceptance scope

`tests/fixtures/conversation_v260.json` records all 16 original Chinese dialogue
steps, input data and state assertions. It is a live acceptance input, not proof
that those steps passed. The controller integration tests begin with committed
analysis, scenario and case fixtures and inspect actual reads, writes, revisions,
manual fields, references, cross-chat reuse and export bytes.

The executable seeded chain sends 16 messages through `submit`, using the default
production capability registry. It verifies expanded case steps, visible-order
scenario selection, inherited subset estimates and arithmetic, a harmless bare
acknowledgment, explanation, preview/application, a scenario-plus-case linked
preview, exact new scenario drafts supplied to synchronization, structural
coverage, review without case writes, versioned format samples available to
another chat, a summary with current IDs, two parsed Excel files, and cancellation
of one preview without cancelling other work. Each response is recovered through
the persisted `get` interface.

Separate tests cover:

- Chat adoption → the card's actual draft-update service → closed/reopened SQLite
  → chat save/share, checking one exact confirmed answer and evidence in another
  chat's stored run inputs.
- Actual write receipts across controller/store restart, and a stale preview
  failing to overwrite a later manual revision.
- A snapshot read finishing after the main run advances, without taking a write
  lease or changing the run's new gate.
- Learning/application of both templates, then changing only the scenario
  template while preserving the case template/manual policy; real exports are
  loaded with OpenPyXL to assert distinct sheet names and columns.
- New business evidence changing only one requirement and its scenario at a
  waiting gate, preserving unrelated rows, cases and the scenario stop; the new
  evidence must reach the waiting run's next input snapshot.

The source-update assertion initially caught missing new sources in the waiting
run despite successful artifact commits. The artifact owner corrected propagation
within the commit transaction. A follow-up assertion also requires the public and
private input revision counters to remain synchronized.

Reusable human acceptance attachments are provided under
`examples/conversation-v260/`: two actual `.xlsx` templates, initial requirements,
and the forced-logout change document. Template workbooks explicitly mark
examples as formatting only and define manual/default fields.
Both final workbooks were exported using the spreadsheet artifact runtime and
reopened with OpenPyXL to verify the two sheet names, Chinese cell values and
header order. Four range renders were inspected. The runtime has no Chinese font,
so Chinese glyphs are absent from those rendered previews; glyph-level visual QA
in Excel remains unverified. The saved workbook Unicode values are intact.

Actual upload → LangGraph clarification → generation → review through FastAPI,
browser interactions and the user's internal model still require their real
runtimes. Passing service tests must not be described as completing those gates.
