# Implementation Plan: Incremental Sync

**Branch**: `003-incremental-sync` | **Date**: 2026-04-05 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/workspace/specs/003-incremental-sync/spec.md`

---

## Summary

Implement event-driven incremental sync for the FrappeRAG vector index: automatically re-index whitelisted Frappe documents on save/delete/rename without blocking the document lifecycle, purge vector entries when a DocType is removed from the whitelist, and surface sync health in the AI Assistant Settings form. Built on the constitution v3.0.0 sidecar architecture (FastAPI + uvicorn, `multilingual-e5-base`, v3_ table prefix).

---

## Technical Context

**Language/Version**: Python 3.11+, Vanilla JS (Frappe Desk)
**Primary Dependencies**: Frappe v15+, ERPNext v15+, FastAPI >= 0.110.0, uvicorn >= 0.29.0, httpx >= 0.27.0, lancedb >= 0.8.0, sentence-transformers >= 2.7.0
**Storage**: LanceDB (bench-level `rag/` directory, accessed exclusively via sidecar HTTP API); MariaDB via Frappe ORM for DocTypes
**Testing**: None — manual acceptance per spec acceptance scenarios (Constitution Principle VII)
**Target Platform**: Linux server (standard Frappe v15 bench)
**Project Type**: Frappe app extension (server-side Python + Vanilla JS page/form extension)
**Performance Goals**: Sync hook adds < 1ms overhead to any document save (whitelist check only; all heavy work enqueued). Individual sync job completes in < 5s under normal sidecar load.
**Constraints**: Must not block document save (FR-003, SC-006). Sidecar unavailability must not block or rollback saves (FR-007). All worker code MUST NOT import lancedb or sentence-transformers directly (Constitution Principle IV / Development Workflow).
**Scale/Scope**: Per-record events (not batch). Sync Event Log grows at event frequency; pruned to 30-day window (FR-013).

---

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Status | Notes |
|---|---|---|
| **I. Frappe-Native Architecture** | ✅ PASS | All app code uses DocTypes, `@frappe.whitelist()`, `hooks.py` doc_events, and Frappe scheduler. The sidecar (FastAPI) is the one permitted exception. |
| **II. Per-Client Data Isolation** | ✅ PASS | v3_ LanceDB tables are bench-local; Sync Event Log is in the site DB. Background jobs call `frappe.set_user()` before any data access. |
| **III. Permission-Aware RAG Retrieval** | ✅ PASS | FR-004: sync job verifies `frappe.has_permission()` at execution time. Skipped (not failed) on denial. Chat retrieval permission filter unchanged. |
| **IV. Zero External Infrastructure** | ✅ PASS | No new external service. The sidecar is the one permitted localhost exception (already mandated by v3.0.0). No Docker, no cloud, no additional sidecar. |
| **V. Asynchronous-by-Default** | ✅ PASS | All sync hooks enqueue to `queue="short"`; handlers return immediately. No blocking I/O in request path. |
| **VI. Zero-Friction Installation** | ✅ PASS | New DocType committed as JSON fixture. Sidecar Procfile entry added by `after_install`. No new pip steps beyond existing requirements.txt (sentence-transformers, fastapi, uvicorn already listed). |
| **VII. No Automated Tests** | ✅ PASS | No test files or framework dependencies introduced. |

**Post-design re-check (Phase 1)**: No violations introduced by the data model or contracts. Sidecar client uses `httpx` (already required). Sync Event Log is a standard Frappe DocType. All new API methods are `@frappe.whitelist()`.

**Pre-existing technical debt noted** (out of scope for Phase 3):
- `rag/embedder.py` still imports `google.generativeai` for Gemini embedding (pre-v3.0.0 pattern).
- `rag/lancedb_store.py` still imports `lancedb` directly in workers (pre-v3.0.0 pattern).
- `rag/retriever.py` still uses `import lancedb` (pre-v3.0.0 pattern).
- Phase 3 new code (`sync_runner.py`) is fully v3.0.0-compliant; migrating Phase 1/2 code is deferred.

---

## Project Structure

### Documentation (this feature)

```text
specs/003-incremental-sync/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── sidecar-api.md   # Phase 1 output — FastAPI sidecar HTTP contract
│   └── api-contracts.md # Phase 1 output — whitelisted Python endpoint contracts
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (new and modified files)

```text
apps/frapperag/frapperag/
├── hooks.py                                    ← EXTENDED: doc_events + daily scheduler
├── sidecar/                                    ← NEW directory
│   ├── __init__.py
│   ├── main.py                                 ← NEW: FastAPI app (5 endpoints)
│   └── store.py                                ← NEW: LanceDB wrapper (v3_ prefix)
├── rag/
│   ├── sidecar_client.py                       ← NEW: httpx wrapper (upsert/delete/drop)
│   ├── sync_hooks.py                           ← NEW: doc_events handlers (lightweight)
│   └── sync_runner.py                          ← NEW: background job functions
├── frapperag/doctype/
│   └── sync_event_log/                         ← NEW DocType directory
│       ├── __init__.py
│       ├── sync_event_log.json                 ← NEW: DocType definition fixture
│       └── sync_event_log.py                   ← NEW: Document class (minimal)
│   └── ai_assistant_settings/
│       ├── ai_assistant_settings.json          ← EXTENDED: sidecar_port + health section
│       └── ai_assistant_settings.py            ← EXTENDED: before_save + on_update
└── api/
    └── indexer.py                              ← EXTENDED: retry_sync + get_sync_health
```

**Structure Decision**: Single Frappe app extension. No additional projects. The sidecar is a subdirectory within the app (not a separate package) — it is launched by Procfile and has no Frappe imports.

---

## Complexity Tracking

> No constitution violations. No complexity justification needed.

---

## Implementation Sequence

The following ordering respects the constitution's "DocType first" and "async contract first" development workflow rules.

### Step 1 — Sidecar infrastructure (blocking prerequisite)

Create `sidecar/store.py` and `sidecar/main.py`. This is required before any sync job can run.

- `store.py`: `lancedb.connect()` once at sidecar startup; `get_or_create_table()`, `upsert_rows()`, `delete_row()`, `drop_table()`.
- `main.py`: FastAPI lifespan loads `multilingual-e5-base` once. Endpoints: `GET /health`, `POST /embed`, `POST /upsert`, `DELETE /record/{table}/{record_id}`, `DELETE /table/{table}`.
- Table naming: `v3_` + doctype.lower().replace(" ", "_"). Record ID: `{doctype}:{name}`.
- Update `setup/install.py` to write the Procfile sidecar entry (if not already present).

### Step 2 — Sidecar client

Create `rag/sidecar_client.py`. Exposes `upsert_record()`, `delete_record()`, `drop_table()`. Raises `SidecarError` on HTTP/connection failure. Reads `sidecar_port` from `AI Assistant Settings`.

### Step 3 — Sync Event Log DocType

Commit `sync_event_log.json` fixture with all fields documented in data-model.md. Minimal `sync_event_log.py` (no custom logic needed). Run `bench migrate`.

### Step 4 — AI Assistant Settings extensions

- Add `sidecar_port` Int field (default 8100) and `sync_health_html` HTML field to `ai_assistant_settings.json`.
- Add `section_sidecar` and `section_sync_health` section breaks.
- Extend `ai_assistant_settings.py`:
  - `on_update`: call `self.get_doc_before_save()` to retrieve the pre-save state (Frappe stores this automatically); diff old vs. new `allowed_doctypes`; for each removed DocType: create Sync Event Log (Purge/Queued) + enqueue `run_purge_job`. No `before_save` hook or `frappe.flags` needed — avoids stale flag state across concurrent worker requests.

### Step 5 — sync_hooks.py (doc_events)

Create `rag/sync_hooks.py`:
- `on_document_save(doc, method=None)`: whitelist check using `frappe.cache().get_doc("AI Assistant Settings")` (not `frappe.get_doc()`) to avoid a DB hit on every document save across the entire site → create log entry (Queued) → enqueue `run_sync_job` (trigger_type=Create or Update based on `doc.is_new()`).
- `on_document_trash(doc, method=None)`: same cache-based whitelist check → create log entry → enqueue `run_sync_job` (trigger_type=Delete).
- `on_document_rename(doc, merge=False)`: same cache-based whitelist check → enqueue delete-old-name + upsert-new-name pair (two log entries, two jobs — or a single Rename job that does both).

Wire into `hooks.py`:
```python
doc_events = {
    "*": {
        "on_update":    "frapperag.rag.sync_hooks.on_document_save",
        "on_trash":     "frapperag.rag.sync_hooks.on_document_trash",
        "after_rename": "frapperag.rag.sync_hooks.on_document_rename",
    }
}
```

### Step 6 — sync_runner.py (background jobs)

Create `rag/sync_runner.py`:
- `run_sync_job(sync_log_id, doctype, name, trigger_type, user, **kwargs)`:
  - `frappe.set_user(user)`
  - Update log entry to Running
  - For Create/Update/Rename: `frappe.has_permission(...)` check → `to_text()` → `sidecar_client.upsert_record()`
  - For Delete: `sidecar_client.delete_record()`
  - Update log entry to Success/Skipped/Failed
- `run_purge_job(sync_log_id, doctype, user, **kwargs)`: `sidecar_client.drop_table()` → update log entry.
- `mark_stalled_sync_jobs()`: transition Running entries with `modif
ied < (now - 10min)` to Failed.
- `prune_sync_event_log()`: delete entries where `creation < (now - 30 days)`.

### Step 7 — API additions

Extend `api/indexer.py`:
- `retry_sync(sync_log_id)`: permission check → load original entry → create new Queued entry → enqueue `run_sync_job`.
- `get_sync_health()`: permission check → query Sync Event Log → return summary + failures dict.

### Step 8 — Admin panel JS

Extend the `AI Assistant Settings` form JS (or add `ai_assistant_settings.js` to the doctype directory):
- On form `refresh`: call `get_sync_health()` → render HTML table into `sync_health_html` field.
- Per-row Retry button → call `retry_sync(sync_log_id)` → refresh panel.

### Step 9 — Scheduler additions

Add to `hooks.py`:
```python
scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs",
            "frapperag.rag.chat_runner.mark_stalled_chat_messages",
            "frapperag.rag.sync_runner.mark_stalled_sync_jobs",
        ],
    },
    "daily": [
        "frapperag.rag.sync_runner.prune_sync_event_log",
    ],
}
```

---

## Key Design Decisions

### Deduplication strategy (FR-008)
`frappe.enqueue(..., job_name=f"rag_sync_{table_key}")` where `table_key = doctype.lower().replace(' ', '_') + '_' + name`. RQ silently drops duplicate jobs with the same job_name that are already queued. If the job is already Running, a new job IS enqueued — giving eventual consistency without coordination overhead.

### Sidecar port configuration
`sidecar_port` Int field on AI Assistant Settings (default 8100). The sidecar client reads this at job execution time: `frappe.get_doc("AI Assistant Settings").sidecar_port`. This avoids hardcoding and allows port changes without a deploy.

### Rename handling (FR-011)
`after_rename` receives `(doc, merge)` where `doc` is the document in its **new** name state. The old name is available via `doc._doc_before_save` or passed as part of Frappe's rename callback. Simplest approach: enqueue one Rename-typed sync job that does `delete_record(doctype, old_name)` + `upsert_record(doctype, new_name, text)` as two sequential sidecar calls. One Sync Event Log entry covers the rename.

### Sync Event Log permissions
RAG Admin cannot delete log entries (audit trail preservation). Only System Manager has delete permission. This is intentional — retries add rows, they do not replace existing Failed rows.

### Sidecar startup in Procfile
The `after_install` hook in `setup/install.py` appends a Procfile line:
```
rag_sidecar: {bench_path}/env/bin/python {app_path}/sidecar/main.py --port {port}
```
Port defaults to 8100. The sidecar reads the port from a CLI argument (not from Frappe settings) because it runs as a separate process without Frappe context.
