# Task 3 — Conversation-first UI

Implemented the approved conversation-first layout while retaining the existing conversation command and artifact contracts.

- The conversation uses the main content width. The current artifact and its stage controls live inside that conversation; there is one editable artifact surface. Expanding, collapsing and entering/leaving focus mode keep the same mounted table and selection.
- Historical typed receipts open their saved revision in a separate read-only snapshot. Stage receipts open their saved stage revision. Browsing history leaves the active artifact, selected scope, run gate and confirmation revision unchanged. Legacy publication metadata now supports `artifact_revisions` supplied by Task 5; old messages without any saved revision remain readable.
- Checkbox selection updates the composer scope without stealing focus. Filtered row order travels with the selected artifact revision; the scope remains until explicitly cleared. Clearing the scope also clears the table selection. Stage confirmation is disabled during backend edits and local editor drafts.
- Empty conversations show a concise welcome and three editable task prompts. Shortcuts append to an existing draft or replace explicitly selected text. Unrelated workspace actions preserve a typed composer draft. The composer starts at two lines and grows to six 24px lines, then scrolls internally.
- Source upload copy asks for the intended purpose and target. Upload-only state does not force a requirement-understanding action. Existing source, profile, model, export, history, project memory and run controls remain accessible.
- Desktop navigation can collapse; narrow screens use the sidebar drawer. Table overflow remains local. The new scoped stylesheet uses the original `ly061/execution-agent-UI` reference commit `8bda3bd9f5321e8ca131e593637849a7575a1219`: white sidebar, `#f6f7fa` canvas, `#d31145`/`#ad0e38` primary controls, `#fff0f3` selected state, `#182230` ink, `#667085` muted text and `#e7e9ef` lines. No remote font import was added.

## Interfaces and ownership

`ArtifactWorkspace` adds optional focus, selection reset, selection change and local editing callbacks. `ArtifactCard` optional callbacks were implemented by Task 4; existing two-argument `onTarget` callers remain valid. `useDrafts` accepts an optional visible row order and an opt-in target-preserving submission clear. `StageArtifact` adds `onOpenSnapshot` and retains its earlier callback for compatibility. `ProjectFacts` from Task 2 is exposed in the sidebar. `main.tsx` imports the conversation and lineage stylesheets.

## Verification

Observed failing regressions before the corresponding changes for missing conversation ownership/history behavior, local edit confirmation, scope clearing and direct checkbox selection. The final focused command passed **32/32**:

```sh
cd frontend
node --import tsx --test tests/conversation-layout-v290.test.tsx tests/workspace-v280.test.tsx tests/phase-navigation-v280.test.tsx tests/human-gates-v271.test.tsx
npm run build
```

Production build and TypeScript checking passed. The existing Mermaid bundle produces the standard chunk-size warning. `git diff --check` passed for the owned files.

A supplemental run of `conversation-v260.test.tsx` hit 16 obsolete setup assertions that require the removed “并排查看结果” button; that button is absent in the unchanged v2.8 baseline as well. The current workspace and conversation contracts are verified by the focused files above, without reinstating the obsolete split-panel UI.

Actual Browser visual verification was attempted by Task 5, but local navigation was blocked with `net::ERR_BLOCKED_BY_CLIENT`. Responsive layout and reference colors are implemented, but a rendered browser comparison is not claimed.

## Clarification UI integration follow-up

Added `frontend/tests/clarification-v290.test.tsx` without changing production code. The four focused DOM tests pass and verify that clicking “采用此答案” sends `clarification.adopt` with `submit:true`, the current draft/question-set/control bindings and the selected question ID; answered questions disappear while unanswered questions remain; the main submit flushes the edited answer and sends `clarification.save` using the returned draft revision; and “先按假设使用” sends `status:provisional`. Every flow asserts the exact command count and no implicit `workflow.continue`. No production issue was found.

```sh
cd frontend
node --import tsx --test tests/clarification-v290.test.tsx
```
