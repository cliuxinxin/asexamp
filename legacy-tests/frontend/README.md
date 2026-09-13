# Archived frontend contracts

These files preserve the original v2.5–v2.8 test sources for historical reference.
They are outside the current `frontend/tests/*.test.tsx` test discovery pattern.
They are not passing v3.0 acceptance tests and should not be counted as such.
Their relative source imports describe their original `frontend/tests` location;
use the matching historical checkout when investigating an old release.

The v3.0 refactor keeps the v2.9.3 React source unchanged. Earlier tests still
expected UI and transport contracts that had already been replaced:

| Archived source | Superseded contract | Current coverage |
| --- | --- | --- |
| `chat-estimate-v2513.test.tsx` | `/interpret` followed by `/messages` and an estimate selector | `chat-only-v293`, `composer-restore-v291`, native HTTP journeys |
| `clarification-v290.test.tsx`, `human-gates-v271.test.tsx` | Independent answer textareas, adoption/continue buttons, and handwritten control-version payloads | A single composer and prompt-bound replies in `chat-only-v293`; actual native interrupts in backend tests |
| `workspace-actions-v2512.test.tsx`, `workspace-v280.test.tsx` | Separate modify/estimate/sync dialogs and workspace apply/reconciliation buttons | Chat tools and preview-once HTTP journeys; inline lineage and phase-navigation tests |
| `conversation-v260.test.tsx` | The removed “并排查看结果” entry, independent clarification drafts and controller commands | Compatible chat receipt, request, retry, selection and Profile behavior extracted to `chat-receipts-current` |
| `guided-v258.test.tsx` | Old exact prompt copy, task selectors, answer-adoption forms and legacy export/template actions | Current prompt behavior and isolated Profile/template tests extracted to `profile-templates-current` |
| `project-context-v2512.test.tsx` | Clarification sharing forms, supplementary-input dialogs and old sample-pinning calls | Memory display and Profile sample editing extracted to `project-memory-current`; native shared-knowledge and pin tools tested in backend |
| `settings-capacity.test.tsx` | Direct RunCard confirmation using custom interrupt/control fields | Capacity settings retained in `settings-capacity-current`; native confirmation tested over HTTP |
| `workspace-smoke.test.tsx` | `/messages`, source-confirmation buttons and an old template-completion control flow | Diagnostic-link and column-definition tests retained in `artifact-options-current` |
| `interactions.test.tsx` | Old agent/reliable modes, SSE expectations, `/messages`, removed navigation labels and workflow buttons | Independent settings, editor, diagram, request-inspector and data-rendering checks retained in `components-current`; current composer tests cover chat interaction |

The extraction preserves applicable component assertions; it does not change
React code or restore obsolete buttons. The prompt test now checks the current
non-destructive insertion behavior: custom draft text remains, the selected task
prompt is appended, and selecting it again does not duplicate it. The mindmap
test remains current and reads its preserved historical fixture from
`legacy-tests/fixtures/user_analysis.json`.

The pre-cleanup `npm test` run executed 169 tests: 94 passed and 75 failed. Those
failures included absent legacy controls/routes, old wording, and the moved
mindmap fixture; they are not represented as a passing historical regression run.

Run current frontend checks from `frontend` with `npm test`. Native business
workflow acceptance lives in `tests/test_native_journey_v300.py` and the other
`tests/test_native_*` files; their controlled model uses actual tool calls.

After this contract cleanup, the complete current `npm test` run passed all
77 tests (0 failures, 0 skipped). This is a DOM/component check, not a rendered
browser or real-model acceptance claim.
