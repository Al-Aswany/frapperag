# Whitelisted API Contracts: Incremental Sync

**Module**: `frapperag.api.indexer` (additions to existing file)

All methods are decorated with `@frappe.whitelist()` and return JSON-serialisable dicts.
Caller must be authenticated via Frappe session. Permission checks occur at the earliest possible point (Constitution development workflow rule).

---

## retry_sync

Re-queue a failed sync attempt. Creates a new `Sync Event Log` entry; the original Failed entry is preserved as history.

**Python call**:
```python
frappe.call("frapperag.api.indexer.retry_sync", { sync_log_id: "SYNC-LOG-20260405-0001" })
```

**Parameters**:
| Name | Type | Required | Description |
|---|---|---|---|
| `sync_log_id` | string | yes | Name of the original Sync Event Log entry to retry |

**Permission check**: Caller must have `RAG Admin` or `System Manager` role.

**Server logic**:
1. Load the original `Sync Event Log` entry; throw if not found or outcome is not `Failed`.
2. Create a new `Sync Event Log` entry with same `doctype_name`, `record_name`, and `trigger_type="Retry"`, `outcome="Queued"`.
3. `frappe.db.commit()`
4. Enqueue `frapperag.rag.sync_runner.run_sync_job` on queue `"short"`.
5. Return `{sync_log_id: <new_entry_name>, status: "Queued"}`.

**Success response**:
```json
{ "sync_log_id": "SYNC-LOG-20260405-0042", "status": "Queued" }
```

**Error responses**:
- `frappe.ValidationError` — original entry not found or not in Failed state
- `frappe.PermissionError` — caller lacks required role

---

## get_sync_health

Return the data needed to render the sync health panel in AI Assistant Settings.

**Python call**:
```python
frappe.call("frapperag.api.indexer.get_sync_health")
```

**Parameters**: None.

**Permission check**: Caller must have `RAG Admin` or `System Manager` role.

**Server logic**:
1. Compute `cutoff = now - 24 hours`.
2. Query `Sync Event Log` grouped by `doctype_name` and `outcome` where `creation >= cutoff`.
3. Query `Sync Event Log` for `outcome = "Failed"` (all time, not just 24h) for the failure list — limited to 100 most recent entries.
4. Return structured dict (see below).

**Success response**:
```json
{
  "summary": [
    {
      "doctype_name": "Customer",
      "success_count": 42,
      "failed_count": 2,
      "last_success": "2026-04-05 14:23:00"
    }
  ],
  "failures": [
    {
      "sync_log_id": "SYNC-LOG-20260405-0007",
      "doctype_name": "Customer",
      "record_name":  "CUST-0042",
      "trigger_type": "Update",
      "error_message": "SidecarError: Connection refused",
      "creation":     "2026-04-05 12:01:33"
    }
  ]
}
```

`summary` is empty `[]` when no sync activity has occurred. `failures` is empty `[]` when all jobs succeeded.

---

## trigger_index (existing, unchanged)

The existing `trigger_index(doctype)` endpoint from Phase 1 is unmodified. It remains the manual "Index Now" path for a full re-index of a DocType. Adding a DocType to the whitelist does NOT automatically call this endpoint (FR-006).

---

## Enqueue contracts (internal — not HTTP endpoints)

These are the `frappe.enqueue` signatures used by sync_hooks.py. Documented here for implementer reference.

### Upsert sync job (Create / Update / Rename trigger)
```python
frappe.enqueue(
    "frapperag.rag.sync_runner.run_sync_job",
    queue="short",
    timeout=120,
    job_name=f"rag_sync_{doctype.lower().replace(' ', '_')}_{name}",
    site=frappe.local.site,
    sync_log_id=log_doc.name,
    doctype=doctype,
    name=name,
    trigger_type="Create" | "Update" | "Rename",
    user=frappe.session.user,
)
```

### Delete sync job (Trash / Delete trigger)
```python
frappe.enqueue(
    "frapperag.rag.sync_runner.run_sync_job",
    queue="short",
    timeout=60,
    job_name=f"rag_sync_{doctype.lower().replace(' ', '_')}_{name}",
    site=frappe.local.site,
    sync_log_id=log_doc.name,
    doctype=doctype,
    name=name,
    trigger_type="Delete",
    user=frappe.session.user,
)
```

### Purge job (whitelist removal)
```python
frappe.enqueue(
    "frapperag.rag.sync_runner.run_purge_job",
    queue="short",
    timeout=120,
    job_name=f"rag_purge_{doctype.lower().replace(' ', '_')}",
    site=frappe.local.site,
    sync_log_id=log_doc.name,
    doctype=doctype,
    user=frappe.session.user,
)
```

**Queue choice**: `"short"` (timeout 120s) — per-record operations are fast (one HTTP call to sidecar). The `"long"` queue is reserved for full-batch indexing jobs.
