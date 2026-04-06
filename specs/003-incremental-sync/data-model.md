# Data Model: Incremental Sync

**Branch**: `003-incremental-sync` | **Date**: 2026-04-05

---

## New DocType: Sync Event Log

**Purpose**: Persistent record of every incremental sync attempt. Feeds FR-009 (health summary) and FR-010 (failure list + retry). One row per attempt; retries produce new rows.

**Autoname**: `SYNC-LOG-{YYYYMMDD}-{####}`

| Field | Fieldname | Type | Options / Constraints | Notes |
|---|---|---|---|---|
| Document Type | `doctype_name` | Data | reqd, maxlen 140 | DocType of the record being synced |
| Record Name | `record_name` | Data | reqd, maxlen 140 | Name/ID of the Frappe record |
| Trigger Type | `trigger_type` | Select | Create \| Update \| Delete \| Rename \| Purge \| Retry | What event caused this sync attempt |
| Outcome | `outcome` | Select | Queued \| Running \| Success \| Skipped \| Failed | Current state of this sync attempt |
| Error Message | `error_message` | Long Text | optional | Populated only on Failed outcome |
| — | `creation` | Datetime | auto (Frappe) | When the log entry was created; used for 24-hour window and 30-day pruning |

**Permissions**:
- `System Manager`: create, read, write, delete
- `RAG Admin`: create, read, write (no delete — preserves audit trail)
- `RAG User`: no access

**Indices needed**: `(outcome, creation)` for health summary queries; `(doctype_name, outcome, creation)` for per-DocType filtering.

**State transitions**:
```
[trigger fires]
     │
     ▼
  Queued ──(job starts)──► Running ──(done)──► Success
                                    ├──(permission denied)──► Skipped
                                    └──(exception)──► Failed
```
Retries create a new row starting from Queued; the Failed row is never mutated.

---

## Extended DocType: AI Assistant Settings (Single)

**Purpose**: Gains Phase 3 additions — sidecar port configuration, sync health display section, and the before_save/on_update logic for whitelist-removal detection.

**New fields added**:

| Field | Fieldname | Type | Default | Notes |
|---|---|---|---|---|
| — | `section_sidecar` | Section Break | — | Label: "RAG Sidecar" |
| Sidecar Port | `sidecar_port` | Int | 8100 | Port the FastAPI sidecar listens on (localhost only) |
| — | `section_sync_health` | Section Break | — | Label: "Sync Health", collapsible |
| Sync Health | `sync_health_html` | HTML | — | Rendered by JS via `get_sync_health()` API call |

**Logic additions** (`ai_assistant_settings.py`):
- `on_update(self)`: call `self.get_doc_before_save()` to retrieve the pre-save state; diff old vs. new `allowed_doctypes`; for each removed DocType, create a `Sync Event Log` entry with `trigger_type="Purge"` and enqueue `run_purge_job`. No `before_save` hook needed.

**Validation note**: Existing `validate()` logic is unchanged. The new `sidecar_port` field has a default of 8100 — no additional validation needed beyond Frappe's Int type guard.

---

## New Source Module: `rag/sidecar_client.py`

**Purpose**: Single `httpx`-based client used by `sync_runner.py` (and eventually `indexer.py`) to communicate with the FastAPI sidecar. Wraps all sidecar HTTP calls.

**Public functions** (all raise `SidecarError` on failure):

```python
def upsert_record(doctype: str, name: str, text: str, port: int) -> None:
    """POST /upsert — embed text via sidecar and store in v3_ table."""

def delete_record(doctype: str, name: str, port: int) -> None:
    """DELETE /record/{table}/{record_id} — remove one vector entry."""

def drop_table(doctype: str, port: int) -> None:
    """DELETE /table/{table} — drop entire v3_{doctype} table for whitelist purge."""
```

**Error type**: `SidecarError(Exception)` — raised on HTTP error, connection refused, or timeout. Callers (sync_runner.py) catch this and mark the sync job as Failed.

**Connection**: All calls target `http://localhost:{port}`. No auth header — sidecar is localhost-only (Principle IV).

---

## New Source Module: `sidecar/main.py` (FastAPI app)

**Purpose**: FastAPI + uvicorn sidecar process. Holds the LanceDB connection and sentence-transformers model in memory. Accepts HTTP requests from Frappe workers.

**Lifespan**: Started once by `bench start` via Procfile. Initialises `multilingual-e5-base` model on startup (~280 MB, one-time load).

**Endpoints** (full contract in `contracts/sidecar-api.md`):

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check |
| POST | `/embed` | Embed a list of texts → vectors |
| POST | `/upsert` | Embed one record and upsert into LanceDB |
| DELETE | `/record/{table}/{record_id}` | Remove one vector row by composite ID |
| DELETE | `/table/{table}` | Drop entire LanceDB table |

---

## New Source Module: `sidecar/store.py`

**Purpose**: Thin LanceDB wrapper, imported only inside the sidecar process. Owns the `lancedb.connect()` call and all table operations.

**Key functions**:
```python
def get_or_create_table(table_name: str) -> lancedb.Table
def upsert_rows(table_name: str, rows: list[dict]) -> None
def delete_row(table_name: str, record_id: str) -> bool
def drop_table(table_name: str) -> bool
```

**Schema**: Same as existing `lancedb_store.py` but with `v3_` prefix and connection to the bench-level `rag/` directory.

---

## New Source Module: `rag/sync_hooks.py`

**Purpose**: Ultra-lightweight Frappe doc_events handlers. Each function checks the whitelist and returns immediately for non-whitelisted DocTypes. All heavy work is deferred to background jobs.

**Functions**:
```python
def on_document_save(doc, method=None) -> None:
    """on_update hook — queue upsert sync job if DocType is whitelisted."""

def on_document_trash(doc, method=None) -> None:
    """on_trash hook — queue delete sync job if DocType is whitelisted."""

def on_document_rename(doc, merge=False) -> None:
    """after_rename hook — queue delete-old + upsert-new pair if whitelisted."""
```

**Whitelist check**: Uses `frappe.cache().get_doc("AI Assistant Settings")` to avoid a DB hit on every save across the entire site. Returns immediately if DocType is not in `allowed_doctypes` or if AI Assistant is disabled.

---

## New Source Module: `rag/sync_runner.py`

**Purpose**: Background job functions. All heavy imports inside functions (constitution Principle I pattern).

**Functions**:
```python
def run_sync_job(sync_log_id: str, doctype: str, name: str, trigger_type: str, user: str, **kwargs) -> None:
    """Worker: embed + upsert (Create/Update/Rename) or delete (Delete) one record."""

def run_purge_job(sync_log_id: str, doctype: str, user: str, **kwargs) -> None:
    """Worker: drop entire v3_ table for a removed-from-whitelist DocType."""

def mark_stalled_sync_jobs() -> None:
    """Scheduler cron: transition Running sync log entries with no recent update to Failed."""

def prune_sync_event_log() -> None:
    """Scheduler daily: delete Sync Event Log entries older than 30 days."""
```

---

## New API endpoints: `api/indexer.py` additions

```python
@frappe.whitelist()
def retry_sync(sync_log_id: str) -> dict:
    """Create a new Sync Event Log entry and queue a new sync job. Original entry preserved."""

@frappe.whitelist()
def get_sync_health() -> dict:
    """Return per-DocType success/fail counts (last 24h) and list of Failed entries."""
```

---

## Hooks additions (`hooks.py`)

```python
doc_events = {
    "*": {
        "on_update":    "frapperag.rag.sync_hooks.on_document_save",
        "on_trash":     "frapperag.rag.sync_hooks.on_document_trash",
        "after_rename": "frapperag.rag.sync_hooks.on_document_rename",
    }
}

scheduler_events = {
    "cron": {
        "*/5 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs",
            "frapperag.rag.chat_runner.mark_stalled_chat_messages",
            "frapperag.rag.sync_runner.mark_stalled_sync_jobs",  # NEW
        ],
    },
    "daily": [
        "frapperag.rag.sync_runner.prune_sync_event_log",  # NEW — FR-013
    ],
}
```
