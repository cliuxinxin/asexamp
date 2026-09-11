# Workflow and clarification peer review — 2026-09-11

Reviewed `graph.py`, `workflow.py`, `conversation_workflow.py`, and `clarification.py` against the extracted v2.5.13 release. Unified diffs were captured with Python `difflib` in scratch as `review-<module>.diff`. No lifecycle production files were edited by this reviewer.

This is source review plus a real SQLite clarification reproduction; it is not an executed LangGraph/HTTP acceptance run. Full-runtime dependencies are unavailable in this environment.

## Blocking findings sent to the owner and root

1. **Boundary wrapper changes interrupt positions during node replay.** `graph.observed_node` clears `_boundary_node` immediately after its interrupt resumes. If the node body then reaches a natural clarification/scenario interrupt, that interrupt occupies position 1. On the next replay the wrapper is omitted and the natural interrupt becomes position 0, consuming the prior boundary resume payload. A write pause before `clarification_gate`, resumed with `{approved:true}`, can therefore make the later clarification consume that object instead of its answer. Inserting a new wrapper while resuming an already persisted natural interrupt has the same ordering problem. Keep the boundary position stable for that node invocation across all replays, and handle an existing natural gate before inserting another interrupt. This conclusion follows the documented index matching and node replay contract in [LangGraph interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts). The lifecycle owner confirmed the finding and is fixing stable node/input markers.

2. **Full textarea edits append contradictory adopted answers.** `clarification.update_draft` retains structured question answers while saving the entire edited textarea as `_freeform_answer`; `_answer` concatenates both. Real Store reproduction: adopt `Lock time? / 5 minutes`, PATCH the complete answer replacing `5` with `10`, then read. Actual result was `Lock time?\n5 minutes\n\nLock time?\n10 minutes`, and the per-question answer remained `5 minutes`. The current frontend sends exactly this full textarea payload. The edit must replace/reconcile the authoritative answer rather than append to old adopted values before save/share/continue.

3. **Revised upstream content can coexist with stale global requirement context.** `node_understanding_gate` caches `v6:requirement_map` and `confirmed_requirements`. Later `FlowEngine.with_evidence` always supplies that cache. An analysis revision applied while waiting at the scenario gate does not replay the earlier understanding gate; subsequent case generation may therefore receive new scenarios with old confirmed requirements or old exclusions. The owned artifact apply path should refresh the exact Run's analysis context and ancestry revision when it atomically commits an upstream edit. This has been escalated to root for the bounded artifact-side fix.

## Additional concrete boundary issue

`observed_node` updates `_boundary_reason` in SQLite but builds its payload from the previously read `run` first. A preceding pause can mislabel a later write/scope boundary as `pause`, preventing the controller's intended automatic continuation. This was sent to the lifecycle owner alongside the replay marker fix.

## Checks that support the intended design

- New `workflow.start` maps intermediate goals to the same resumable `generate_case` graph and persists `stop_after`; the scenario and understanding gates check this durable field.
- Both the conversation continuation adapter and `engine.resume` reject advancement past an unchanged stop position.
- `workflow.continue` uses a submitted shared draft source; `node_apply_answer` checks the draft source/cache before creating a source. Ordinary continuation has an explicit path avoiding duplicate clarification sources.
- Scope changes are queued while generation is active and applied after the runner persists a waiting boundary.
- Graph model invocation records input version before the call and rejects output if that version changes.
- Artifact apply now merges evidence IDs/roles and both public/private input versions into guarded waiting Runs atomically; stop/hold fields remain intact. Root's controlled integration suite independently exercises this path.

Final acceptance still needs the owner's fixes and actual LangGraph replay tests, particularly a boundary pause immediately before a natural gate, a queued existing gate resume followed by a write, and adopt → full textarea edit → save → shared fact lookup.

## Resolution update

The lifecycle owner reports fixing stable boundary replay markers and full-text answer reconciliation, with nine focused tests passing. Those owner changes still require root's final integrated verification. The third finding is fixed in the artifact apply path: only matching Run ancestry/snapshots refresh `v6:requirement_map`, `confirmed_requirements` and parent revision; both input version fields advance for changed Run inputs. A new SQLite regression passed. Artifact peer findings about illegal parent reassignment and mutable proposal replay were also fixed; the artifact suite now passes 17 focused tests plus 13 legacy action tests.

## Final bounded source recheck

The final implementation removes the dynamically inserted control interrupt from `graph.observed_node`. `WorkflowEngine.build_workflow` now routes each business transition through a separate fixed `boundary_<name>` node; `workflow_boundary` owns that node's sole interrupt, so natural clarification/confirmation nodes retain their own positions. A later hold routes through a fresh boundary invocation. `graph._execute` reloads Run state after checkpoint loading and preserves an existing actual interrupt whenever a boundary request is pending, before consuming a queued resume. The boundary payload uses the updated stored reason. These changes resolve the reviewed interrupt-order and stale-reason source defects without changing business node names. Full LangGraph execution remains an explicitly unverified environment boundary.

The exact original SQLite textarea reproduction was rerun: adopting `5 minutes`, editing the full text to `10 minutes`, and rereading now yields only `Lock time?\n10 minutes`; the per-question value is also `10 minutes`. No old value remains. This resolves the contradictory-answer finding.
