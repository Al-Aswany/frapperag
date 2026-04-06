# Quickstart: Incremental Sync (Phase 3)

**Branch**: `003-incremental-sync` | **Date**: 2026-04-05

---

## Prerequisites

- Phase 1 (`001-rag-embedding-pipeline`) is installed and at least one DocType is indexed.
- Phase 2 (`002-rag-chat-core`) is installed and functional.
- The RAG sidecar is running (see below — this phase introduces it).

---

## Running the RAG Sidecar

Phase 3 introduces `frapperag/sidecar/main.py`. The sidecar is managed by `bench start` via the bench Procfile. The `after_install` hook writes the Procfile entry automatically.

**Verify the sidecar is running**:
```bash
curl http://localhost:8100/health
# → {"status": "ok", "model": "multilingual-e5-base"}
```

The port defaults to 8100 and is configurable in AI Assistant Settings → RAG Sidecar → Sidecar Port.

**Manual start (development)**:
```bash
cd /path/to/bench
env/bin/python apps/frapperag/frapperag/sidecar/main.py --port 8100
```

---

## Enabling Incremental Sync

No new configuration is required. Incremental sync is active as soon as Phase 3 is installed and the bench is restarted.

**What happens automatically**:
- Every save, trash, or rename of a whitelisted DocType record triggers a background sync job.
- Removing a DocType from the whitelist in AI Assistant Settings triggers a vector purge job.

---

## Verifying Sync Activity

1. Edit and save a whitelisted record (e.g. a Customer).
2. Open AI Assistant Settings → Sync Health section.
3. Confirm a Success entry appears for `Customer` within the last 24 hours.

**Console verification** (bench Python console):
```python
import frappe
frappe.init(site="your.site"); frappe.connect()

# Count recent sync events
events = frappe.db.get_all(
    "Sync Event Log",
    filters={"doctype_name": "Customer", "outcome": "Success"},
    order_by="creation desc",
    limit=5,
)
print(events)

# Verify vector entry via sidecar
import httpx
r = httpx.get("http://localhost:8100/health")
print(r.json())
```

---

## Retrying a Failed Sync

1. Open AI Assistant Settings → Sync Health → Failed entries.
2. Click **Retry** next to a failed entry.
3. A new `Sync Event Log` entry is created with `trigger_type=Retry`.
4. The original Failed entry remains visible in history.

---

## Testing the Purge Flow (whitelist removal)

1. Index a DocType (e.g. Item) using "Index Now" from Phase 1.
2. Ask the RAG chat a question that matches an Item record — confirm it appears in citations.
3. Open AI Assistant Settings → remove "Item" from Allowed Document Types → Save.
4. Wait for the background purge job to complete (check Sync Health for a Purge entry).
5. Ask the same question again — the Item record should no longer appear.

---

## Scheduler Jobs Added

| Cron | Function | Purpose |
|---|---|---|
| `*/5 * * * *` | `sync_runner.mark_stalled_sync_jobs` | Fail Running entries with no update > 10 min |
| `daily` | `sync_runner.prune_sync_event_log` | Delete log entries older than 30 days |

---

## Key Files (Phase 3)

```
frapperag/
├── sidecar/
│   ├── main.py              ← NEW: FastAPI sidecar (/embed, /upsert, /record, /table)
│   └── store.py             ← NEW: LanceDB wrapper (v3_ prefix, initialized once)
├── rag/
│   ├── sidecar_client.py    ← NEW: httpx wrapper for sidecar HTTP calls
│   ├── sync_hooks.py        ← NEW: lightweight doc_events handlers
│   └── sync_runner.py       ← NEW: background job functions
├── frapperag/doctype/
│   └── sync_event_log/      ← NEW: DocType (one row per sync attempt)
├── api/
│   └── indexer.py           ← EXTENDED: retry_sync(), get_sync_health()
└── hooks.py                 ← EXTENDED: doc_events + daily scheduler
```
