# Data Model: RAG Chat Core — Phase 2

**Branch**: `002-rag-chat-core`
**Date**: 2026-03-16

---

## DocType 1: Chat Session

**Type**: Standard
**Module**: FrappeRAG
**Naming**: `RAG-SESS-.YYYY.-.MM.-.DD.-####`
**Is Submittable**: No
**Track Changes**: No

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `title` | Title | Data | | Auto-set from first user message on first successful response; blank until then |
| `status` | Status | Select | Open\nArchived | default: Open |

> **Note**: `owner` is a standard Frappe field (Link → User) populated automatically on `insert()`. No explicit `owner` field definition required.

### Permissions

| Role | Read | Write | Create | Delete |
|---|---|---|---|---|
| System Manager | ✅ | ✅ | ✅ | ✅ |
| RAG Admin | ✅ | ✅ | ✅ | — |
| RAG User | ✅ | ✅ | ✅ | — |

### `permission_query_conditions` Hook

Registered in `hooks.py` → `permission_query_conditions = {"Chat Session": "frapperag.frapperag.doctype.chat_session.chat_session.permission_query_conditions"}`.

```python
def permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    return f'`tabChat Session`.`owner` = {frappe.db.escape(user)}'
```

Ensures list views and `frappe.db.get_all("Chat Session", ...)` return only sessions owned by the requesting user (unless System Manager).

### Status State Machine

```
┌──────────┐  Archive button clicked  ┌────────────┐
│   Open   │ ─────────────────────── ▶│  Archived  │
└──────────┘                          └────────────┘
```

Only the Open → Archived transition exists. Archived sessions are never deleted — messages remain readable. No reverse transition in Phase 2.

---

## DocType 2: Chat Message

**Type**: Standard
**Module**: FrappeRAG
**Naming**: `RAG-MSG-.YYYY.-.MM.-.DD.-####`
**Is Submittable**: No
**Track Changes**: No (high-frequency writes during job execution)

### Fields

| Fieldname | Label | Fieldtype | Options / Config | Notes |
|---|---|---|---|---|
| `session` | Session | Link | Chat Session | required; parent session |
| `role` | Role | Select | user\nassistant | required; identifies turn author |
| `content` | Content | Long Text | required | User question or assistant response text |
| *(Section)* | Processing | Section Break | | |
| `status` | Status | Select | Pending\nCompleted\nFailed | default: Pending |
| `tokens_used` | Tokens Used | Int | default: 0 | Total tokens from Gemini usage_metadata |
| *(Section)* | Citations | Section Break | | |
| `citations` | Citations | Long Text | | JSON array: `[{"doctype": str, "name": str}]`; null for user messages |
| *(Section)* | Error Detail | Section Break | Collapsible | |
| `error_detail` | Error Detail | Long Text | | Written on Failed status; capped at 2000 chars |

### Permissions

| Role | Read | Write | Create | Delete |
|---|---|---|---|---|
| System Manager | ✅ | ✅ | ✅ | ✅ |
| RAG Admin | ✅ | ✅ | ✅ | — |
| RAG User | ✅ | ✅ | ✅ | — |

### `permission_query_conditions` Hook

Registered in `hooks.py` → `permission_query_conditions = {"Chat Message": "frapperag.frapperag.doctype.chat_message.chat_message.permission_query_conditions"}`.

```python
def permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    escaped = frappe.db.escape(user)
    return (
        f'`tabChat Message`.`session` IN '
        f'(SELECT `name` FROM `tabChat Session` WHERE `owner` = {escaped})'
    )
```

Ensures users only see messages from sessions they own. The subquery is the correct Frappe pattern for records whose ownership lives on a parent DocType.

### Status State Machine

```
              ┌──────────────────────────┐
              │          Pending         │ ← created by send_message()
              └──────┬───────────────────┘
                     │
      job succeeds   │         job fails (non-transient error)
  ┌──────────────────▼─┐     ┌──────────────────────┐
  │      Completed      │     │        Failed         │
  └─────────────────────┘     └──────────────────────┘

  Stalled detection (scheduler, every 30 min):
  Pending + creation > 10 min ago → Failed
```

---

## Additions to Existing DocTypes

### `AI Assistant Settings` (Phase 1 — read-only in Phase 2)

No schema changes. Phase 2 reads `gemini_api_key` via `get_password("gemini_api_key")` inside background jobs. No new fields added.

---

## LanceDB (Phase 1 — read-only in Phase 2)

Phase 2 reads from tables created by Phase 1. No writes to LanceDB in Phase 2.

### Tables accessed

| Table name | DocType |
|---|---|
| `v1_customer` | Customer |
| `v1_sales_invoice` | Sales Invoice |
| `v1_item` | Item |

### Search pattern (per table)

```python
rows = (
    table.search(query_vector, vector_column_name="vector")
    .limit(5)          # TOP_K = 5 per table (spec Assumption)
    .to_list()
)
# Returns rows with: id, doctype, name, text, vector, last_modified, _distance
```

---

## Python Module Map (new files in Phase 2)

```
apps/frapperag/frapperag/
│
├── hooks.py                                    # +mark_stalled_chat_messages cron
│                                               # +permission_query_conditions entries
├── frapperag/
│   └── doctype/
│       ├── chat_session/
│       │   ├── __init__.py
│       │   ├── chat_session.json               # DocType definition
│       │   └── chat_session.py                 # permission_query_conditions()
│       └── chat_message/
│           ├── __init__.py
│           ├── chat_message.json               # DocType definition
│           └── chat_message.py                 # permission_query_conditions()
│
├── rag/
│   ├── retriever.py                            # embed_query() + search_all_tables() + filter_by_permission()
│   ├── prompt_builder.py                       # build_messages() → Gemini message list
│   ├── chat_engine.py                          # generate_response() → {text, citations, tokens_used}
│   └── chat_runner.py                          # run_chat_job() + mark_stalled_chat_messages()
│
├── api/
│   └── chat.py                                 # 5 whitelist endpoints
│
└── page/
    └── rag_chat/
        ├── rag_chat.json                       # Page definition (title, roles)
        └── rag_chat.js                         # Vanilla JS chat UI
```

---

## Scheduler Events (additions to Phase 1)

```python
scheduler_events = {
    "cron": {
        "*/30 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs",          # Phase 1
            "frapperag.rag.chat_runner.mark_stalled_chat_messages",  # Phase 2 addition
        ],
    }
}
```

`mark_stalled_chat_messages()`: finds all `Chat Message` records with `status="Pending"` and `creation` older than 10 minutes, sets them to `status="Failed"` with an explanatory `error_detail`.
