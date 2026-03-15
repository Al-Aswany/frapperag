# Data Model: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline`
**Date**: 2026-03-15

---

## DocType 1: AI Assistant Settings

**Type**: Single (one record per site)
**Module**: FrappeRAG
**Naming**: `AI Assistant Settings` (fixed, single)

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `is_enabled` | Enabled | Check | default: 1 | Master on/off switch |
| `gemini_api_key` | Gemini API Key | Password | required | Encrypted at rest via Frappe's password field |
| `sync_schedule` | Sync Schedule Preference | Select | Manual Only\nDaily\nWeekly | Phase 1: stored preference only; auto-sync deferred |
| *(Section)* | Allowed Document Types | Section Break | | |
| `allowed_doctypes` | Allowed Document Types | Table | RAG Allowed DocType | Child table; which DocTypes may be indexed |
| *(Section)* | Allowed Roles | Section Break | | |
| `allowed_roles` | Allowed Roles | Table | RAG Allowed Role | Child table; which roles may trigger indexing |

### Permissions

| Role | Read | Write | Create | Delete |
|---|---|---|---|---|
| System Manager | ✅ | ✅ | ✅ | — |
| RAG Admin | ✅ | ✅ | — | — |

### Validation Rules

- `gemini_api_key` must not be blank when `is_enabled = 1`. Raise `frappe.ValidationError` on save.
- `allowed_doctypes` must contain at least one entry when `is_enabled = 1`.
- `allowed_roles` must contain at least one entry when `is_enabled = 1`.

---

## DocType 2: RAG Allowed DocType *(child table)*

**Type**: Child Table (parent: AI Assistant Settings, fieldname: `allowed_doctypes`)
**Module**: FrappeRAG

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `doctype_name` | Document Type | Link | DocType | `in_list_view: 1`; the DocType name to index |

---

## DocType 3: RAG Allowed Role *(child table)*

**Type**: Child Table (parent: AI Assistant Settings, fieldname: `allowed_roles`)
**Module**: FrappeRAG

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `role` | Role | Link | Role | `in_list_view: 1`; role permitted to trigger indexing |

---

## DocType 4: AI Indexing Job

**Type**: Standard
**Module**: FrappeRAG
**Naming**: `naming_series: AI-INDX-.YYYY.-.MM.-.DD.-####`
**Is Submittable**: No
**Track Changes**: No (high-frequency updates would bloat version history)

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `doctype_to_index` | Document Type | Data | required | Stored as Data (not Link) to avoid DocType rename issues |
| `status` | Status | Select | Queued\nRunning\nCompleted\nCompleted with Errors\nFailed\nFailed (Stalled) | default: Queued |
| `triggered_by` | Triggered By | Link | User | required; set from `frappe.session.user` at enqueue time |
| *(Column Break)* | | Column Break | | |
| `progress_percent` | Progress | Percent | default: 0 | Updated during job execution |
| *(Section)* | Timing | Section Break | | |
| `start_time` | Start Time | Datetime | | Set when job begins executing |
| `end_time` | End Time | Datetime | | Set when job reaches a terminal state |
| `last_progress_update` | Last Progress Update | Datetime | | Updated after each batch; used for stalled job detection |
| *(Section)* | Record Counts | Section Break | | |
| `total_records` | Total Records | Int | default: 0 | Determined at job start |
| `processed_records` | Processed | Int | default: 0 | Successfully embedded and stored |
| `skipped_records` | Skipped | Int | default: 0 | Excluded by permission check |
| `failed_records` | Failed | Int | default: 0 | Errors during text conversion or embedding |
| `tokens_used` | Tokens Used (est.) | Int | default: 0 | Estimated embedding tokens consumed; accumulated after each batch |
| *(Section)* | Error Detail | Section Break | | Collapsible |
| `error_detail` | Error Detail | Long Text | | Last error message; appended for per-document errors |
| *(Section)* | Internal | Section Break | | Collapsible |
| `queue_job_id` | Queue Job ID | Data | read_only | RQ job identifier from `frappe.enqueue` |

### Status State Machine

```
               ┌─────────────────────────┐
               │         Queued          │ ← created by whitelist method
               └────────────┬────────────┘
                            │ job starts executing
               ┌────────────▼────────────┐
               │         Running         │
               └───┬───────┬─────────────┘
                   │       │
    all ok         │       │  fatal error
  ┌────────────────▼──┐  ┌─▼──────────────┐
  │     Completed     │  │     Failed      │
  └───────────────────┘  └────────────────┘
                   │
    some doc errors│
  ┌────────────────▼──┐
  │ Completed with    │
  │     Errors        │
  └───────────────────┘

  Stalled detection (scheduler, every 30 min):
  Running + last_progress_update > 2h ago → Failed (Stalled)
```

### Permissions

| Role | Read | Write | Create | Delete |
|---|---|---|---|---|
| System Manager | ✅ | ✅ | ✅ | ✅ |
| RAG Admin | ✅ | ✅ | ✅ | — |
| RAG User | ✅ | — | — | — |

### `permission_query_conditions` hook

RAG Admin and RAG User see all jobs (no row-level filter needed for Phase 1; all
administrators share visibility of all indexing jobs on the site).

---

## Semantic Index Store (LanceDB, not a Frappe DocType)

**Location**: `{site_path}/private/files/rag/`
**One LanceDB table per indexed DocType.**

### Table naming convention

`{doctype.lower().replace(" ", "_")}` — e.g.:
- `Sales Invoice` → `sales_invoice`
- `Customer` → `customer`
- `Item` → `item`

### Arrow Schema (per table)

| Column | Arrow Type | Description |
|---|---|---|
| `id` | `pa.string()` | Composite key: `"{doctype}:{name}"` — unique per document |
| `doctype` | `pa.string()` | Frappe DocType name |
| `name` | `pa.string()` | Frappe document name (primary key of the source record) |
| `text` | `pa.string()` | Human-readable text summary used for embedding |
| `vector` | `pa.list_(pa.float32(), 768)` | 768-dim float32 vector from `text-embedding-004` |
| `last_modified` | `pa.string()` | ISO 8601 datetime string from source document's `modified` field |

### Upsert strategy

Use LanceDB `merge_insert("id")` for incremental updates:
- Records with matching `id` → update all columns
- New `id` values → insert new row
- No deletes in Phase 1 (deleted documents remain in index; handled in Phase 2)

---

## Python Module Map

```
apps/frapperag/frapperag/
├── hooks.py                          # after_install, scheduler_events, fixtures
├── requirements.txt                  # lancedb, pyarrow, google-generativeai
├── modules.txt                       # FrappeRAG
│
├── frapperag/                        # Frappe module
│   ├── doctype/
│   │   ├── ai_assistant_settings/
│   │   │   ├── ai_assistant_settings.json    # DocType definition
│   │   │   └── ai_assistant_settings.py      # validate() hook
│   │   ├── rag_allowed_doctype/
│   │   │   └── rag_allowed_doctype.json
│   │   ├── rag_allowed_role/
│   │   │   └── rag_allowed_role.json
│   │   └── ai_indexing_job/
│   │       ├── ai_indexing_job.json          # DocType definition
│   │       └── ai_indexing_job.py            # get_job_status() helper
│
├── rag/                              # Core RAG utilities (Python package)
│   ├── __init__.py
│   ├── base_indexer.py               # BaseIndexer ABC (BaseTool lifecycle)
│   ├── lancedb_store.py              # LanceDB open/write/upsert (no module-level state)
│   ├── text_converter.py             # DocType → human-readable text
│   ├── embedder.py                   # Gemini text-embedding-004 caller
│   └── indexer.py                    # DocIndexerTool + run_indexing_job()
│
└── api/
    ├── __init__.py
    └── indexer.py                    # @frappe.whitelist() HTTP endpoints
```

---

## Role Fixtures

Two custom roles shipped as fixtures in `hooks.py`:

| Role | Purpose |
|---|---|
| `RAG Admin` | Can configure settings, trigger indexing, view all jobs |
| `RAG User` | Can view indexing job history (read-only); will gain query access in Phase 2 |

---

## Scheduler Events

```python
scheduler_events = {
    "cron": {
        "*/30 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs"
        ],
    }
}
```

`mark_stalled_jobs()`: finds all `AI Indexing Job` records with
`status="Running"` and `last_progress_update` older than 2 hours, sets them
to `status="Failed (Stalled)"` and appends an error message.

---

## `after_install` Hook

```python
after_install = "frapperag.setup.install.after_install"
```

`after_install()` creates the LanceDB base directory:
```python
import os
import frappe

def after_install():
    rag_path = frappe.get_site_path("private", "files", "rag")
    os.makedirs(rag_path, exist_ok=True)
    frappe.db.commit()
```

No manual directory creation required from the administrator.
