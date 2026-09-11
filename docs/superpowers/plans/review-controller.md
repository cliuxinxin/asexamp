# v2.6.0 controller independent review

Reviewed against `docs/TCG-CONVERSATIONAL-WORKFLOW-DESIGN.md` and `docs/superpowers/plans/2026-09-11-conversation-controller.md` on 2026-09-11.

Scope: `conversation.py`, `conversation_context.py`, `conversation_receipts.py`, the `storage.py` user-message reuse change, and controller startup/routes/shutdown in `main.py`. Domain modules were read only to verify result and receipt contracts. No production files were edited by this reviewer.

Diff against v2.5.13: `scratch/review-controller.diff`; reviewed snapshot hashes: `scratch/review-controller-inputs.json`. New modules are included in full. Other agents and root continue updating production files, so the diff records the named snapshot rather than asserting a final immutable release.

## Findings corrected during review

| Priority | Finding | Current focused evidence |
|---|---|---|
| P1 | A bare acknowledgement after a side estimate applied an older open preview. | Current controller binds acknowledgement to latest presented/replied pending; stale preview test passes. |
| P1 | Applying a preview in a later Turn did not resume the original compound request's remaining actions. | Tail resumes once after apply; duplicate apply request does not repeat it. |
| P1 | Deferred writes resolved targets/versions again from changed conversational focus. | Frozen target A/revision 1 remains A/1 after side read B; stale write fails instead of modifying A v2 or B. |
| P1 | Adapter-returned needs-input objects were omitted from persistent pending state. | Global-impact question persists and the explicit scope-all answer resumes the original operation and tail. |
| P1 | In-flight/deferred modification had no durable command cancellation route. | Planned in-flight write can now be cancelled and its late domain commit is rejected. The unplanned-interpreter edge is also corrected and verified below. |
| P1 | Resolving an input question bypassed the original Turn lock, permitting a retransmitted original request to race the resumed command. | Concurrent original retry waits; exactly one read capability executes. |
| P1 | Applied-preview tail could remain persisted as needs-confirmation while executing, preventing restart recovery after abrupt process exit. | Original Turn now persists running status while the resumed tail is awaiting its capability. |

The existing success receipt is used on retry after an injected post-commit cancellation: one artifact version is saved, one capability execution occurs. A newer explicit hold cancels the original request's not-yet-executed continue after preview application while retaining applied work.

## Final focused rereview

All three remaining controller follow-ups above were corrected by root and checked in the final scoped rereview:

1. **Explicit cancellation during initial interpretation:** an identified running Turn can be cancelled before any command exists. The persisted cancellation is rechecked when late interpretation returns; the proposed artifact write is never created or executed.
2. **Authoritative workflow control target:** initial run ID, interrupt ID, and control version are saved before awaiting interpretation. A delayed continue binds the original gate/control version and is rejected when another actor has advanced to a new gate.
3. **Follow-up planning capacity:** completed case-details results are summarized to actual IDs and version references without long step bodies. Capacity is checked after completed results are added; a rejected follow-up does not make a second model call or repeat the successful first capability.

No open finding remains from this bounded controller review. This is not a claim about model interpretation quality or unreviewed domain/frontend behavior.

Historical read version mismatch was reported to the artifact adapter owner: the owner reports it corrected and verified by its own 12 focused plus 13 legacy action checks. These checks are not included in this reviewer's count.

## Executed focused verification

Command:

```bash
python3 -m unittest discover -s tests -p test_conversation_review_v260.py -v
```

Result at this review snapshot: **13 tests passed**. Tests use actual SQLite Store/ConversationController and controlled capability/model responses, with event barriers for concurrent paths. No full historical suite was repeated, no real model quality was measured, and these controller checks do not claim full HTTP/LangGraph integration.

The owned regression file is `tests/test_conversation_review_v260.py`. Scope covers stale acknowledgement; preview continuation/idempotency; newer hold; deferred target/version retention; domain input persistence; original retry serialization; post-commit receipt recovery; planned in-flight cancellation; persisted tail recovery state; cancellation before interpretation returns; delayed gate/control binding; compact completed-result planning; and the post-result capacity guard.
