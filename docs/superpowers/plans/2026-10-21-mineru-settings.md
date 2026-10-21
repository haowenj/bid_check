# MinerU settings implementation plan

**Goal:** Bring the PDF Trans MinerU settings workflow into bid_check, including live GPUStack instance discovery and use of saved settings without a restart.

**Approved scope:** The user requested the same behavior as PDF Trans: probe and test are drafts; only Save persists the endpoint; Reset restores environment defaults. No per-task discovery.

**Architecture:** Reuse the reference connection validation, paginated instance/route lookup, and health check. Store overrides under the `mineru` key in an `application_settings` SQLite table in the existing app database. Build a fresh parser from the current saved configuration at each parse/extraction operation rather than mutating parsers serving concurrent operations. Use existing LLM_BASE_URL and LLM_API_KEY for GPUStack discovery; keep MINERU_API_KEY separate for direct parsing.

**Constraints:** Current main agent only; no platform CLI or new connector. Keep missing MinerU configuration missing. API keys must never appear in HTML, diagnostic responses, or logs. Preserve custom URL prefixes and parser dependency injection. Work in the existing clean checkout on branch codex/mineru-settings. Leave changes uncommitted for the user to review.

## Task 1: Connection and storage

Files: app/mineru_connection.py, app/mineru_settings.py, tests/test_mineru_settings.py.

- [x] Write failing tests for persisted overrides, restart/reset, blank-key retention, invalid URLs, missing defaults, name/route discovery, changing ports, pagination and upstream failures.
- [x] Run tests and verify failures are due to missing functionality.
- [x] Implement MinerUConnection, discover_mineru_endpoint(base_url, model_name, api_key, client), check_mineru_health(connection, client), and MinerUSettingsRepository(database_path, defaults).
- [x] Run the focused tests (16 passed).

## Task 2: Page, routes and live configuration

Files: app/settings_routes.py, app/api.py, app/templates/base.html, app/templates/settings.html, app/static/settings.js, app/static/bid-check.css, README.md, tests/test_mineru_settings.py.

- [x] Write failing tests for settings entry/page, save/reset, secret handling, same-origin writes, probe/test without persistence, and later workflow operations using updated settings.
- [x] Implement /settings plus /settings/mineru save/probe/test/reset, using existing LLM settings for discovery. Copy the reference browser draft/revision handling; use existing page styles.
- [x] Add parser factories to build_default_workflow for all tender extraction/evaluation/retry and bid parsing/fallback paths. Preserve injected parser objects.
- [x] Document discovery prerequisites and manual save semantics.
- [x] Run focused tests (116 passed), then full pytest (486 passed), diff checks and browser layout verification. Browser used an isolated temporary database and dummy URLs; missing-key feedback and save confirmation verified. No live intranet validation.

## Review focus

- An unconfigured app still opens settings and does not invent a localhost endpoint.
- Model-route aliases, pagination, IPv6 and worker port changes resolve correctly.
- Failed probes/tests and invalid/cross-origin saves leave persistent configuration untouched.
- Saved keys remain private and blank inputs retain the current key.
- An already-created workflow reads new settings without mutating an active parser; caches continue to distinguish endpoint/model.

## Execution record

- Initial reference audit complete; user explicitly chose PDF Trans manual probe/test/save semantics.
- Author implements and reviews independently, following the user's no-subagent rule.
- Evaluation workflow RED test exposed an existing cache-key bug: property-valued parser descriptors were ignored, so changing endpoint/model could return old evaluation results. Include non-callable descriptors in that key; the failing workflow test covers the fix.
- Final author review: checked reference parity, secret-safe public settings and error responses, transactional key retention, all workflow parser call sites, cache identity, and browser behavior. No subagents used; no outstanding findings.
