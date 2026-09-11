# Frontend v2.6.0 focused verification

2026-09-11. Owner: conversation_frontend. This records frontend DOM/API-boundary behavior with controlled fetch responses; it does not claim a real intranet model or browser-to-live-backend acceptance run.

## Implemented behavior

- Every plain composer message uses one `POST /api/chats/{chat}/turns`, with a stable per-request `client_message_id`. No `/interpret`, `/dialogue`, `/instructions`, `/edit`, `/resume`, or `/messages` semantic dispatch remains in App. Hints include the actual artifact revision, displayed stable ID order, selected IDs, Profile, mode, and existing generation preferences.
- Running tasks and pending turn requests leave the composer usable. Distinct requests have independent retry identities. A later pause/hold can arrive while an earlier edit is pending. Read requests do not disable a run's real confirmation control.
- Typed persisted replies render answer evidence, estimate tables, pinned artifact versions, actual case step/expected tables, actual before/after diffs, coverage relationships, separate real export links, draft state, and pending/deferred messages. They reuse ArtifactCard, ChangePreview, ChatEstimate, and WorkspaceCoverage.
- Run controls, analysis/estimate/preview/apply/discard shortcuts, sample pinning, targeted supplements, and export invoke unified backend capabilities. Export passes the exact artifact/revision and preserves the dialog's “generation snapshot” option. Existing direct manual artifact/Profile editors retain their existing validated domain endpoints.
- Shared clarification drafts load and PATCH the backend's authoritative revision. Chat adoption invalidates the same visible draft. Adopt, save, share, and continue are separate actions. Continue after save/share retains the original interrupt/control binding, allowing a later hold to supersede it.
- Draft polling preserves unblurred local text. External changes retain the original local base revision and cause a visible conflict instead of silent overwrite. An owned successful PATCH advances only its own local base while preserving further typing. Failed saves preserve input and offer explicit reload.
- Saved artifact versions and conversational Profile changes update the next composer request. Unsent manual Profile selections remain local. Failed HTTP 200 receipts show their actual failure and retain composer content.
- Existing default 52 px input, 780 px intake width, wide conversation layout, prompt prefill protections, scenario/case template separation, and real case step/expected rendering remain. Display/package versions are 2.6.0.

## Failure first evidence

New DOM tests were added before each corresponding behavior change and run with `node --import tsx --test tests/conversation-v260.test.tsx` from `frontend/` (focused `--test-name-pattern` where appropriate).

Observed initial failures covered blocked running send, confirmation locked by a read, missing typed parts, nonpersistent adoption, legacy estimate shortcut, legacy export, blocked later hold, silent external draft rebase, stale next-turn artifact version, export scope/snapshot omissions, stale conversational Profile selection, unbound pending continue, the owned-PATCH stale-revision race, and failed receipt clearing.

Scratch transcripts: `/tmp/frontend-v260-red.log`, `/tmp/frontend-v260-actions-red.log`, `/tmp/frontend-v260-export-red.log`, `/tmp/frontend-v260-hold-red.log`, `/tmp/frontend-v260-conflict-red.log`, `/tmp/frontend-v260-focus-red.log`, `/tmp/frontend-v260-contract-red.log`, `/tmp/frontend-v260-owned-red.log`. A transient DOM assertion was corrected from comparing a live HTMLElement against null to a boolean absence assertion to avoid inspecting React's entire attached fiber graph during an expected wait.

## Fresh passing checks

1. `node_modules/.bin/tsc --noEmit` — passed.
2. `node --import tsx --test tests/conversation-v260.test.tsx` — **16 tests passed, 0 failed**.
3. `node --import tsx --test --test-name-pattern='task prompt fills|task selection fills|scenario card exposes|case scoped proposal|App carries learned' tests/guided-v258.test.tsx` — **5 tests passed, 0 failed**. Covers prefill/draft preservation, scenario export UI, and scoped template behavior.
4. Direct CSS invariant check — existing 52 px composer height, 780 px intake max width, and unbounded wide conversation rules retained. No pixel/browser screenshot claim.

The root owner will run the final production build and capture the fresh dist manifest after all integration changes.

## Limits and historical suite

An exploratory `npm test` during migration ran 95 historical/current tests: 52 passed and 43 failed. This is not a passing full-suite claim. Historical tests encode superseded `/interpret`/run endpoint routing, synchronous local-only draft adoption, and read-induced confirmation locks. Some unrelated historical expectations also already conflict with the supplied original UI (for example “新对话” versus “新建生成会话” and raw stream visibility). Per root direction, historical suites were left unchanged; acceptance evidence is the focused current tests above. Backend agents/root own real domain-state and full chain integration checks.

## Final pending-object display check

Added an ordered rendering of the actual pending candidates (title, stable ID, saved revision), preserving server order for ordinal chat replies. The new DOM test first failed because no candidate listitems existed, then passed after implementation. Transcript: `/tmp/frontend-v260-candidates-red.log`. Fresh focused DOM suite: 16 passed, 0 failed; TypeScript check passed.
