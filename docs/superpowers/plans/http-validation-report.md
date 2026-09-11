# v2.7 HTTP framework validation report

Validated 2026-09-11 with the real local runtime at
`/workspace/scratch/34c288a69784/tcg-v260-testenv`.

## Scope

`tests/test_framework_http_v270.py` exercises one continuous conversation via
`TestClient(create_app(tmp_path, controlled_model))`. Every user request is sent
as natural-language content to `POST /api/chats/{chat_id}/turns`; no request body
contains the legacy `command` or `intent` selectors.

The semantic model outputs are controlled and deterministic. This test therefore
does **not** claim that a live model will always infer the same actions from the
Chinese prose. It validates that model-selected registered actions execute
correctly across the real HTTP routes, v7 LangGraph authoring workflow, SQLite
storage/checkpoints, artifact services, lineage rules, and Excel exporters.

## Validated path

1. A conversation turn starts the production mainflow in auto mode from a real
   uploaded requirement source. The graph creates and publishes analysis,
   scenarios, reviewed cases, and genuine revision history; nothing is hand
   seeded and no revisions table is modified by the test.
2. Scenario estimation returns a numeric range without mutating the scenario or
   completed Run.
3. Artifact explanation names the actual case and remains read-only.
4. Read-only case review exposes the saved action/expected step while preserving
   the case revision.
5. A scenario edit with related-case synchronization produces a two-artifact
   preview, changes neither artifact until apply, and does not restart or advance
   the completed mainflow.
6. A new change source is impact-analyzed, then updates the analysis and its
   linked scenario/case branch with checked version increments.
7. Coverage reports one linked requirement, scenario, and case with current
   analysis/scenario lineage.
8. Separate frozen scenario and case XLSX downloads open successfully with
   `openpyxl`; asserted cells contain the updated scenario title, case title, and
   expected logout behavior.
9. Stored conversation turns identify the turn graph as `turn-v270`, and the
   workflow Run identifies the production graph as version 7.

## Verification

Command:

```bash
/workspace/scratch/34c288a69784/tcg-v260-testenv/bin/python -m pytest -q \
  tests/test_framework_http_v270.py
```

Result:

```text
1 passed, 1 warning in 0.93s
```

The warning is Starlette's existing deprecation warning for the
`anyio.abc.BlockingPortal` alias. No production defect was encountered by this
bounded validation path.
