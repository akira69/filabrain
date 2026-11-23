# Agent workflow verification

Release verification: **0.1.28 development preview**. This remains a preview, not a finished 1.0 release.

ZIP SHA-256: `a6097eb9cb32ca266c0fc12f9f53e394e98104f03a94613d72edf5f0a7311d8d`.

- Full backend: **632 passed**. New regressions cover native print-consumption events, unknown weights, linked spool profiles, exact publisher previews, shared swatch conflicts, denied/revoked access, incomplete sources, input bounds, duplicate SKUs and preserving verified fields when a network budget expires.
- All **12 browser suites passed**, including 320px, phone, desktop, editor/standalone separation, history/late replies, model-free tasks, pagination, blocked-source feedback and computed intake transfer. The intake test accepts product fields, reuses a swatch, confirms a spool design and reaches final confirmation without saving.
- Exact ZIP install, upgrade, rollback, native MCP read tools and grounded chat passed on stock **FilaMan 2.2.2** and **2.4.0**.
- Changed Python passed Ruff. The existing spool helper retains its unrelated FURB167/SIM905/SIM102 style exceptions. Frontend duplication check: **0 clones**. Generated page build and diff checks passed.
- A fresh independent whole-branch review found integration and coverage issues; fixes were folded into their feature commits with regression coverage. A subsequent LAN test exposed the research deadline race, now covered by a regression.

- Native color assistance has a read-only exact-reference/product discovery service and a shared Chat review client. Contract fixtures cover current/proposed swatches, source links, explicit staging, preserved alpha, existing-match selection, stale rejection and no automatic inventory writes. A fresh review found long product-URL truncation and a confirmation timeout shorter than human response time; both have RED→GREEN regressions. Empty evidence sections are hidden and current form colors are labelled as drafts. Product/catalog links and quoted evidence remain visible when a standard code is embedded in a color name; unrelated filament fields stay hidden. RAL and Pantone regressions preserve the distinction between catalog claims and publisher previews.
- A fresh review of the squashed feature found no further issues and independently reran the focused backend and Chrome color workflow.
- The launcher declaration and color context/stage/open contract match the coordinated host design. The host drawer/launcher implementation is owned by the separate **Design plugin UI launchers** workstream; its final integrated color-form deployment is not claimed by these plugin-only fixtures. Minimum hosts retain existing workflows without the new color launcher.

- Earlier LAN verification (before the evidence-display correction): all inventory and source checks below used the prior candidate. The corrected ZIP is verified locally and has not been redeployed to LAN.
- LAN built-in MCP: all eight plugin tools exercised across live checks; inventory, color assignments and pending audits unchanged. Known Paramount intake returned 12 fields, official RAL 3020/#AB1519 and two spool designs. Sources that could not be verified stayed partial; temporary API keys were revoked. No duplicate MCP server used.
- LAN task layout passed at 320px, 390px and desktop width with login disabled for the test copy, including a fresh automatic no-credentials entry. The served page matches the exact packaged source. New live color reads returned RAL 3020/#AB1519 and Pantone 2118 C/#353579 from publisher sources; checked records and pending audit suggestions stayed unchanged. The restart ZIP is aligned; the host image and production were not changed.

Synthetic layout examples: [phone](../screenshots/agent-workflows/chat-tasks-390.png), [desktop](../screenshots/agent-workflows/chat-tasks-1280.png).

Qwen drafted bounded usage documentation from selected source files. Codex checked it against the implementation, corrected permissions and added missing job-fit/coverage guidance. Token savings unmeasured.

Live supplier availability varies. A denied or rate-limited source must remain visibly unverified; partial research must retain verified fields and must not claim completion. Browser phone sizes are checked in Chrome; physical iOS Safari and external ChatGPT/Claude clients are not certified by these checks.
