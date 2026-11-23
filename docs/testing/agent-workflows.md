# Agent workflow verification

Candidate: **0.1.28 development preview**. No release or upstream/catalog submission.

ZIP SHA-256: `c87ed71d72096351f8c8d49fbe8d798dac921633cf42a38b1186f36023d50a9f`.

- Full backend: **621 passed**. New regressions cover native print-consumption events, unknown weights, linked spool profiles, exact publisher previews, shared swatch conflicts, denied/revoked access, incomplete sources, input bounds, duplicate SKUs and preserving verified fields when a network budget expires.
- All **11 browser suites passed**, including 320px, phone, desktop, editor/standalone separation, history/late replies, model-free tasks, pagination, blocked-source feedback and computed intake transfer. The intake test accepts product fields, reuses a swatch, confirms a spool design and reaches final confirmation without saving.
- Exact ZIP install, upgrade, rollback, native MCP read tools and grounded chat passed on stock **FilaMan 2.2.2** and **2.4.0**.
- Changed Python passed Ruff. The existing spool helper retains its unrelated FURB167/SIM905/SIM102 style exceptions. Frontend duplication check: **0 clones**. Generated page build and diff checks passed.
- A fresh independent whole-branch review found integration and coverage issues; fixes were folded into their feature commits with regression coverage. A subsequent LAN test exposed the research deadline race, now covered by a regression.

- LAN built-in MCP: all eight plugin tools exercised across live checks; inventory, color assignments and pending audits unchanged. Known Paramount intake returned 12 fields, official RAL 3020/#AB1519 and two spool designs. Sources that could not be verified stayed partial; temporary API keys were revoked. No duplicate MCP server used.
- LAN task layout passed at 320px, 390px and desktop width with login disabled for the test copy. Production was not changed.

Synthetic layout examples: [phone](../screenshots/agent-workflows/chat-tasks-390.png), [desktop](../screenshots/agent-workflows/chat-tasks-1280.png).

Qwen drafted bounded usage documentation from selected source files. Codex checked it against the implementation, corrected permissions and added missing job-fit/coverage guidance. Token savings unmeasured.

Live supplier availability varies. A denied or rate-limited source must remain visibly unverified; partial research must retain verified fields and must not claim completion. Browser phone sizes are checked in Chrome; physical iOS Safari and external ChatGPT/Claude clients are not certified by these checks.
