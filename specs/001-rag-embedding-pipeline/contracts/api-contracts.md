# API Contracts: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline`
**Date**: 2026-03-15
**Transport**: Frappe whitelisted methods (HTTP POST via `frappe.call()`)
**Authentication**: Frappe session cookie (standard Frappe auth)

---

## Contract 1: Trigger Indexing Job

**Method**: `frapperag.api.indexer.trigger_indexing`
**HTTP**: `POST /api/method/frapperag.api.indexer.trigger_indexing`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Before enqueuing, the method MUST:
1. Verify `AI Assistant Settings.is_enabled == 1`.
2. Verify `frappe.session.user` has at least one role listed in
   `AI Assistant Settings.allowed_roles`.
3. Verify `doctype` is in `AI Assistant Settings.allowed_doctypes`.
4. Verify no other `AI Indexing Job` with `doctype_to_index == doctype`
   and `status IN ("Queued", "Running")` exists.

If any check fails → raise `frappe.PermissionError` or `frappe.ValidationError`
with a descriptive message. No job is created.

### Request

```json
{
  "doctype": "Customer"
}
```

| Field | Type | Required | Validation |
|---|---|---|---|
| `doctype` | string | yes | Must be a non-empty string matching an entry in `allowed_doctypes` |

### Success Response (HTTP 200)

```json
{
  "message": {
    "job_id": "AI-INDX-2026-03-15-0001",
    "status": "Queued"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `job_id` | string | Name of the newly created `AI Indexing Job` record |
| `status` | string | Always `"Queued"` on success |

### Error Responses

```json
{ "exc_type": "PermissionError", "exception": "You do not have permission to trigger indexing." }
{ "exc_type": "ValidationError", "exception": "Document type 'Quotation' is not in the allowed list." }
{ "exc_type": "ValidationError", "exception": "An indexing job for 'Customer' is already running." }
{ "exc_type": "ValidationError", "exception": "AI Assistant is disabled. Enable it in AI Assistant Settings." }
```

---

## Contract 2: Get Job Status

**Method**: `frapperag.api.indexer.get_job_status`
**HTTP**: `POST /api/method/frapperag.api.indexer.get_job_status`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

The caller must have `read` permission on `AI Indexing Job` (RAG Admin or RAG User
role). Frappe's standard permission check via `frappe.has_permission()` applies.

### Request

```json
{
  "job_id": "AI-INDX-2026-03-15-0001"
}
```

| Field | Type | Required | Validation |
|---|---|---|---|
| `job_id` | string | yes | Must be a non-empty string; validated via `frappe.db.exists()` |

### Success Response (HTTP 200)

```json
{
  "message": {
    "job_id":           "AI-INDX-2026-03-15-0001",
    "doctype_to_index": "Customer",
    "status":           "Running",
    "progress_percent": 42.5,
    "total_records":    200,
    "processed_records":84,
    "skipped_records":  2,
    "failed_records":   0,
    "start_time":       "2026-03-15 09:00:00",
    "end_time":         null,
    "error_detail":     null
  }
}
```

| Field | Type | Nullable | Description |
|---|---|---|---|
| `job_id` | string | no | AI Indexing Job name |
| `doctype_to_index` | string | no | The DocType being indexed |
| `status` | string | no | One of: Queued / Running / Completed / Completed with Errors / Failed / Failed (Stalled) |
| `progress_percent` | float | no | 0.0–100.0 |
| `total_records` | int | no | Total records at job start (0 if not yet determined) |
| `processed_records` | int | no | Successfully embedded |
| `skipped_records` | int | no | Excluded by permission |
| `failed_records` | int | no | Failed to embed |
| `start_time` | string | yes | ISO datetime or null if not started |
| `end_time` | string | yes | ISO datetime or null if not finished |
| `error_detail` | string | yes | Last error message or null |

### Error Responses

```json
{ "exc_type": "DoesNotExistError", "exception": "AI Indexing Job AI-INDX-2026-03-15-9999 not found." }
{ "exc_type": "PermissionError",   "exception": "Not permitted to view this job." }
```

---

## Contract 3: List Indexing Jobs

**Method**: `frapperag.api.indexer.list_jobs`
**HTTP**: `POST /api/method/frapperag.api.indexer.list_jobs`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Standard Frappe list permission on `AI Indexing Job`.

### Request

```json
{
  "limit": 20,
  "page": 1
}
```

| Field | Type | Required | Default |
|---|---|---|---|
| `limit` | int | no | 20 |
| `page` | int | no | 1 |

### Success Response (HTTP 200)

```json
{
  "message": {
    "jobs": [
      {
        "job_id":           "AI-INDX-2026-03-15-0001",
        "doctype_to_index": "Customer",
        "status":           "Completed",
        "progress_percent": 100.0,
        "total_records":    200,
        "processed_records":198,
        "failed_records":   2,
        "triggered_by":     "admin@example.com",
        "start_time":       "2026-03-15 09:00:00",
        "end_time":         "2026-03-15 09:04:22"
      }
    ],
    "total": 5,
    "page":  1
  }
}
```

---

## Contract 4: Realtime Event — `rag_index_progress`

**Transport**: Frappe realtime (Socket.IO over WebSocket)
**Direction**: Server → Client (background job → triggering user's browser session)
**Event name**: `rag_index_progress`
**Targeting**: Published to the triggering user only (`user=user` parameter)

### Payload

```json
{
  "job_id":           "AI-INDX-2026-03-15-0001",
  "status":           "Running",
  "progress_percent": 55.0,
  "processed_records":110,
  "total_records":    200,
  "skipped_records":  2,
  "failed_records":   1
}
```

| Field | Type | Description |
|---|---|---|
| `job_id` | string | Identifies which job this update belongs to |
| `status` | string | Current job status |
| `progress_percent` | float | 0.0–100.0 |
| `processed_records` | int | Cumulative successfully processed |
| `total_records` | int | Total to process (0 if not yet determined) |
| `skipped_records` | int | Cumulative skipped by permission |
| `failed_records` | int | Cumulative failed |

### Publication frequency

Published after every batch of 20 documents (or fewer for the final batch).
Also published on job start (status=Running) and job end (terminal status).

### Terminal payload example (job completed)

```json
{
  "job_id":           "AI-INDX-2026-03-15-0001",
  "status":           "Completed",
  "progress_percent": 100.0,
  "processed_records":198,
  "total_records":    200,
  "skipped_records":  2,
  "failed_records":   0
}
```

### Client-side subscription (Vanilla JS)

```javascript
frappe.realtime.on("rag_index_progress", function(data) {
    if (data.job_id !== current_job_id) return;  // guard for multiple open tabs
    update_progress_ui(data);
    if (["Completed", "Completed with Errors", "Failed", "Failed (Stalled)"].includes(data.status)) {
        frappe.realtime.off("rag_index_progress");  // stop listening after terminal state
    }
});
```

---

## Contract 5: Realtime Event — `rag_index_error`

**Transport**: Frappe realtime
**Direction**: Server → Client
**Event name**: `rag_index_error`
**Targeting**: Published to triggering user only

### Payload (fatal error that terminates the job)

```json
{
  "job_id":      "AI-INDX-2026-03-15-0001",
  "status":      "Failed",
  "error":       "Gemini API key is invalid. Embedding generation aborted.",
  "processed_records": 40,
  "total_records":     200
}
```

---

## Internal Interface: `BaseIndexer` → `DocIndexerTool`

This is not an HTTP API but documents the internal Python interface contract
between the base class and the concrete indexer, following the BaseTool pattern
from `frappe_assistant_core`.

```python
class BaseIndexer(ABC):
    """
    Lifecycle:
      1. validate_arguments(args)   — check doctype, settings
      2. check_permission(user)     — role check against allowed_roles
      3. execute(args)              — enqueue job, return job_id
      4. log_execution(result)      — write to Frappe Error Log or custom log
    """

    @abstractmethod
    def validate_arguments(self, args: dict) -> None:
        """Raise frappe.ValidationError if args are invalid."""

    @abstractmethod
    def check_permission(self, user: str) -> None:
        """Raise frappe.PermissionError if user is not permitted."""

    @abstractmethod
    def execute(self, args: dict) -> dict:
        """Enqueue job. Return {"job_id": ..., "status": "Queued"}."""

    def log_execution(self, args: dict, result: dict, duration: float) -> None:
        """Write a Frappe Error Log entry (info level) for audit purposes."""
```

```python
class DocIndexerTool(BaseIndexer):
    """Concrete indexer for a Frappe DocType."""

    name = "rag_doc_indexer"
    source_app = "frapperag"
    requires_permission = None  # custom check in check_permission()

    def validate_arguments(self, args: dict) -> None: ...
    def check_permission(self, user: str) -> None: ...
    def execute(self, args: dict) -> dict: ...
```

```python
def run_indexing_job(job_id: str, doctype: str, user: str) -> None:
    """
    Entry point for the Frappe background worker.
    Called by frappe.enqueue. Site context already initialised by the worker.
    """
```
