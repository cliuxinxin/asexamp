# TCG v2.3 implementation

Approved scope: whole-document generation by default, capacity-triggered grouping, targeted corrections, table plus persistent conversation UI. No full regression or deployment.

1. Add DirectEngine on graph version 5; route generation directly to one node. Keep version 4 resumable. Include full evidence, explicit prompt budget and output reserve. Only partition oversized requests; retain scope context and source references. Persist accepted pages and repair invalid rows only. Clarifications use the existing durable interrupt.
2. Use the execution-agent-UI reference's white navigation, rose accent, gray canvas and table/chat layout. Keep existing editing, evidence and export APIs. Default to generation, auto-open results, keep diagnostics collapsed.
3. Exercise upload → one-call generation → targeted update → XLSX export; exercise clarification and capacity fallback in the same smoke module. Build frontend. Ship sources and built assets, excluding data, credentials and dependencies.
