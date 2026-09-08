# Dual Runtime Implementation Plan

> For agentic workers: use superpowers:executing-plans for inline execution.

**Goal:** Publish the GitHub chat app on Sites while preserving the Python local deployment.

**Architecture:** One frontend and task contract, two runtime adapters. The cloud adapter uses LangGraph.js, D1 for transactional records/checkpoints/events, and R2 for source files. An SSE request drives one leased run. Disconnects suspend unfinished work; reconnects recover the checkpoint, with possible replay of the incomplete model call.

**Tech Stack:** Existing React/Vite frontend, Python local backend, TypeScript Workers, LangGraph.js/LangChain.js, D1, R2.

**Spec:** The approved design in this conversation: cloud execution may pause on disconnect; local background execution is unchanged.

## Global Constraints

- Keep start.py, local backend, .env behavior and local data format unchanged.
- Keep all nine graph nodes, seven intents, evidence checks, stable artifact revisions, manual confirmation, model input inspection and SSE replay.
- Set each model attempt timeout to 3600 seconds.
- Use D1 lease fencing for every run mutation; cancel or expired lease must reject late model output.
- Sources and results persist outside Worker memory; no browser-only authoritative state.
- Reuse the existing frontend with a cloud runtime flag, including correct cloud pause wording.
- API keys are server-side, never returned by settings or diagnostics.
- Deploy privately through Sites and publish code through a GitHub PR.

## Tasks

- [x] 1. Add isolated cloud package/build, D1 schema and transactional storage. Verify concurrent claims, fencing, artifact revision conflicts, atomic publication and event replay using real SQLite through a D1 test adapter.
- [x] 2. Add LangGraph.js graph and LangChain streaming gateway using the same contracts. Verify analysis batches, generation pages, schema repair, query/modify/template paths and human pauses with deterministic model fixtures.
- [x] 3. Implement existing REST API contract, R2 uploads and XLSX export. Exercise a complete run, disconnect/reconnect, cancellation, retry, input inspection and manual scenario edit through HTTP tests.
- [x] 4. Enable cloud frontend mode and test it. Run the existing Python suite as a regression gate, then build both frontend outputs.
- [ ] 5. Review, commit and push the branch; create PR. Push the identical source to Sites, package, deploy privately and verify terminal deployment status.
