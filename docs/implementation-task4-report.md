# Task 4 — Inline lineage and case review

Implemented in `frontend/src/ArtifactCard.tsx`, `ArtifactLineage.tsx`, and `artifact-lineage.css`. `WorkspaceCoverage.tsx` remains available for explicit read-only conversation coverage snapshots; the live artifact no longer adds a separate coverage table. Task 3 imports the scoped CSS through `main.tsx`.

## Behavior

- Scenario rows expose exact requirement IDs, titles, parent revisions, complete parent fields and available evidence chunks. Case rows expose their single primary scenario and that scenario's requirements. Parent content comes from the backend's `lineage_rows`; matching business IDs in unrelated coverage rows never establishes ancestry.
- `/artifacts/{id}/workspace?revision=N` is fetched once per version in each mounted card. Expansion, filters, checkboxes, and repeated snapshot props reuse that request. The backend's mixed per-row revisions and per-row `stale` state distinguish two cases sharing a scenario when only one is synchronized.
- Missing or incomplete ancestry, explicit exclusions, assumptions, changed parents and pending upstream discrepancies are separate visible states. Missing parents do not claim that an upstream edit occurred. Unversioned legacy evidence is explicitly labeled as current source content.
- The existing result owns compact scope filters, including missing downstream rows, unlinked requirements, pending synchronization, review findings and recorded changes. Requirements with no downstream rows remain visible. When case branches exist, scenario statistics name the selected branch.
- Case rows retain preconditions, the first action and its paired expected result, and total step count when collapsed. Expanded rows show the complete paired step table. Entering focus resets a prior collapse and opens complete steps without changing selection.
- Review findings bind only by explicit case IDs. The table shows recorded reasons and modification fields; revision changes and review-after-edit indicators consume actual backend `revision_diff` and `review` metadata. A report with no row-specific finding is labeled “未记录逐条问题”; linkage never becomes a semantic coverage or execution success claim.
- Template custom columns are selectable. The existing form/JSON editor, history/diff/restore, AI action, template, sample and export tools remain available on the live result. A form edit preserves untouched manual execution values.
- Historical snapshots hide row selection, edit, AI-target and restore actions. Compact historical cards include their revision in their accessible name and open the loaded snapshot.
- Additive `onEditingChange` and `onSelectionChange` callbacks let Task 3 hold stage confirmation during local editing and bind direct chat to current selection. `onTarget` also provides current filtered `viewOrder`, preserving visible ordinal references.

## Verification

Test-first failures covered missing parent content, missing collapsed step pairs, missing inline status/filter behavior, immutable historical loading, unrepresented uncovered requirements, sibling synchronization drift, partial ancestry status, direct checkbox scope propagation and entering focus after a prior collapse.

Current focused command:

```sh
cd frontend
node --import tsx --test tests/artifact-lineage-v290.test.tsx tests/conversation-layout-v290.test.tsx tests/coverage-contract-v271.test.tsx
npm run build
```

The card suite contains 12 cases; the integrated command also covers the conversation layout and explicit read-only coverage snapshots. Production TypeScript/Vite build succeeds, with the existing Mermaid chunk-size warning. `git diff --check` is clean for owned files.

The old `workspace-actions-v2512` suite was sampled, but its pre-conversation request fixtures lack the current required chat/turn contract; its collapse assertion also expects the first step to disappear, contrary to the approved v2.9 design. It is not used as a v2.9 completion gate. The current targeted suites exercise the preserved user interactions through their current contracts.

Browser visual verification is delegated to Task 5; this task's verification covers DOM behavior and production compilation.
