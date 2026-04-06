# Research: Incremental Sync

**Branch**: `003-incremental-sync` | **Date**: 2026-04-05

---

## 1. Frappe doc_events hook — "*" wildcard

**Decision**: Use `doc_events = {"*": {...}}` in `hooks.py` with `on_update`, `on_trash`, and `after_rename` events.

**Rationale**: Frappe's `doc_events` hook natively supports `"*"` as a catch-all DocType key. The hook fires for every DocType save/trash/rename. The handler must be a lightweight gate function that checks the whitelist and returns immediately for non-whitelisted DocTypes — this is the fast path for the vast majority of saves.

**Key Frappe event semantics**:
- `on_update` — fires after every successful DB write (both new inserts and updates). Covers FR-001 (create and update in a single hook).
- `on_trash` — fires after a document is moved to trash. Covers FR-002.
- `after_rename` — fires after `frappe.rename_doc()` completes with `(doc, merge=False)` as args. Covers FR-011.
- `after_insert` is NOT needed: `on_update` already fires on first insert.

**Performance note**: The `on_update` hook on `"*"` fires for every DocType save on the site. The whitelist check is a single `frappe.get_cached_doc("AI Assistant Settings").allowed_doctypes` read — effectively O(1) with Frappe's document cache. If the DocType is not whitelisted, the function returns in microseconds with zero DB queries.

**Alternatives considered**: Custom `app_include_js` or Frappe custom scripts — rejected because they are client-side and cannot reliably fire on all save paths (API, bench, import, etc.).

---

## 2. Frappe enqueue job_name deduplication (FR-008)

**Decision**: Use `job_name=f"rag_sync_{table_key}"` where `table_key = doctype.lower().replace(' ', '_') + '_' + name` in `frappe.enqueue`.

**Rationale**: Frappe's `frappe.enqueue()` accepts a `job_name` parameter. When RQ receives a new enqueue call with a `job_name` that matches an already-queued (not yet started) job, it silently drops the duplicate. This satisfies FR-008's "queued" deduplication case without any DB-level locking.

**Behavior when job is already Running**: A running RQ job is no longer "queued" — its `job_name` slot is freed. A new save event will enqueue a fresh job for the latest state. This is the correct eventual-consistency behaviour per spec clarification (2026-04-04).

**Limitation**: Job name deduplication is best-effort across worker restarts (RQ flushes in-flight state on crash). This matches the spec assumption: "Deduplication ... is best-effort and is not guaranteed across worker restarts."

**Alternatives considered**: DB-level "Queued" check before enqueue (query `Sync Event Log` for existing Queued entry). Rejected because it introduces a TOCTOU race window and requires a DB round-trip on every save.

---

## 3. Sidecar HTTP API for per-record upsert and delete

**Decision**: The RAG sidecar (FastAPI + uvicorn, already mandated by constitution v3.0.0) will expose four endpoints used by Phase 3: `POST /upsert`, `POST /embed`, `DELETE /record/{table}/{record_id}`, and `DELETE /table/{table}`.

**Rationale**: Constitution v3.0.0 prohibits direct `import lancedb` in worker processes. All LanceDB operations must go through the sidecar HTTP API via `httpx`. Phase 3 requires:
- Embedding a single record's text → `POST /embed`
- Upserting one vector entry → `POST /upsert`
- Removing one vector entry → `DELETE /record/{table}/{record_id}`
- Dropping an entire DocType table on whitelist purge → `DELETE /table/{table}`

**Sidecar state**: The sidecar holds the LanceDB connection and the sentence-transformers model in memory across requests — this is its core value (no cold-start per worker). Phase 3 leverages the same sidecar as Phase 1/2.

**Current codebase state**: `embedder.py` and `lancedb_store.py` still use the pre-v3.0.0 pattern (direct Gemini API + direct LanceDB). Phase 3 creates `sidecar/main.py`, `sidecar/store.py`, and `rag/sidecar_client.py` as new infrastructure. Migrating the existing `indexer.py` and `retriever.py` to the sidecar is deferred as a separate concern (out of scope for Phase 3).

**Table naming**: Sidecar uses `v3_` prefix + doctype.lower().replace(" ", "_"). Record composite ID: `{doctype}:{name}` (same pattern as existing `lancedb_store.py` but with `v3_` prefix). The sidecar is responsible for computing table names from doctype strings.

**Alternatives considered**: Direct `httpx.delete` calls with body payloads for delete operations. Rejected in favour of REST-idiomatic URL-path DELETE for record/table removal, since the composite IDs and table names are URL-safe after transformation.

---

## 4. Whitelist-removal detection (FR-005)

**Decision**: Detect removed DocTypes by comparing the pre-save and post-save `allowed_doctypes` lists inside `AIAssistantSettings.on_update()`, using `before_save` to snapshot the old set via `frappe.flags`.

**Rationale**: Frappe's Single DocType `on_update` fires after the new data is already committed to the DB. To diff old vs. new, capture the previous allowed-DocType set in `before_save` (where the DB still holds the old values) and store it on `frappe.flags._rag_old_allowed_doctypes`. In `on_update`, compute `removed = old_set - new_set` and queue a purge job for each removed DocType.

**Purge semantics per spec**: Drop the entire LanceDB table for the removed DocType in one atomic `DELETE /table/{table}` sidecar call. The table is recreated from scratch only if the DocType is re-added to the whitelist and "Index Now" is triggered (FR-006).

**Edge case — adding a new DocType**: Adding to the whitelist does NOT trigger auto-indexing (FR-006). `on_update` checks for removals only; additions are silently ignored.

**Alternatives considered**: A scheduled periodic reconciliation that compares the whitelist against existing LanceDB tables. Rejected because it introduces latency (up to the schedule interval) before sensitive data is purged — a governance concern.

---

## 5. Sync Event Log — storage and pruning

**Decision**: Create a new `Sync Event Log` DocType (non-single, regular DocType) with autoname `SYNC-LOG-{YYYYMMDD}-{####}`. Prune entries older than 30 days via a daily scheduled cron job.

**Rationale**: The log is the persistence layer for FR-009 (sync health summary) and FR-010 (failure list with retry). It stores one lightweight row per sync attempt — DocType name, record name, trigger type, outcome, error message, and timestamp. Storage is bounded by event frequency × 30-day retention window, not document content size.

**Health summary query**: `frappe.db.get_all("Sync Event Log", filters={...}, group_by="doctype_name", fields=["doctype_name", "count(*)", ...])` with a 24-hour window filter. This is a simple aggregate query; no external system query is needed.

**Retry semantics**: When the admin clicks Retry, a new `Sync Event Log` entry is created with `trigger_type="Retry"` and `outcome="Queued"`. The original failed entry is NOT updated — it stays as a history record. A new background sync job is enqueued. This matches the spec clarification (2026-04-04).

**Pruning**: Add a `daily` scheduler entry for `frapperag.rag.sync_runner.prune_sync_event_log`. Uses `frappe.db.delete("Sync Event Log", {"creation": ["<", cutoff]})` with a 30-day cutoff.

**Alternatives considered**: Storing sync outcomes in `AI Indexing Job` (reuse existing DocType). Rejected because `AI Indexing Job` tracks full-index batch jobs, not per-record events. Mixing the two would make both harder to query and display correctly.

---

## 6. Admin panel sync health display (FR-009, FR-010)

**Decision**: Extend `AI Assistant Settings` with an HTML section rendered by a new `@frappe.whitelist()` method `get_sync_health()`. The JS in the settings form calls this method on load and renders a table of per-DocType counts + a failure list with Retry buttons.

**Rationale**: The settings form already displays DocType and role configuration. Adding a collapsible "Sync Health" section (HTML field + JS hook) keeps all admin controls in one place without introducing a new page.

**Retry flow**: Admin clicks Retry → `frappe.call("frapperag.api.indexer.retry_sync", {sync_log_id})` → server creates new log entry + enqueues sync job → client refreshes the health panel.

**Alternatives considered**: A dedicated `rag_admin` Frappe Page for sync health. Considered (a `rag_admin` page already exists from Phase 1), but extending the existing settings form is simpler and keeps governance controls co-located with configuration.
