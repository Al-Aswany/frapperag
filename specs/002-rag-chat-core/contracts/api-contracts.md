# API Contracts: RAG Chat Core — Phase 2

**Branch**: `002-rag-chat-core`
**Date**: 2026-03-16
**Transport**: Frappe whitelisted methods (HTTP POST via `frappe.call()`)
**Authentication**: Frappe session cookie (standard Frappe auth)

---

## Contract 1: Create Session

**Method**: `frapperag.api.chat.create_session`
**HTTP**: `POST /api/method/frapperag.api.chat.create_session`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Caller must be authenticated. No additional role check beyond session cookie — any `RAG User` can create sessions (role gates page access; DocType permission allows create).

### Request

No parameters required.

### Success Response (HTTP 200)

```json
{
  "message": {
    "session_id": "RAG-SESS-2026-03-16-0001"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `session_id` | string | Name of the newly created `Chat Session` record |

**Synchronous**: response is returned before any background processing (FR-018).

### Error Responses

```json
{ "exc_type": "PermissionError", "exception": "Not permitted." }
```

---

## Contract 2: Send Message

**Method**: `frapperag.api.chat.send_message`
**HTTP**: `POST /api/method/frapperag.api.chat.send_message`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

1. Verify `session_id` exists and `Chat Session.owner == frappe.session.user`. Raise `PermissionError` otherwise.
2. Verify no existing `Chat Message` with `status="Pending"` exists in the session. Raise `ValidationError` otherwise (FR-019 server-side guard).

### Request

```json
{
  "session_id": "RAG-SESS-2026-03-16-0001",
  "content": "Show me unpaid invoices for ACME Corp"
}
```

| Field | Type | Required | Validation |
|---|---|---|---|
| `session_id` | string | yes | Must exist; caller must be owner |
| `content` | string | yes | Non-empty |

### Success Response (HTTP 200)

```json
{
  "message": {
    "message_id": "RAG-MSG-2026-03-16-0001",
    "status": "Pending"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `message_id` | string | Name of the newly created `Chat Message` record |
| `status` | string | Always `"Pending"` on success |

**Non-blocking**: returns immediately after enqueuing the background job.

### Error Responses

```json
{ "exc_type": "PermissionError",   "exception": "Access denied." }
{ "exc_type": "ValidationError",   "exception": "A message is already being processed in this session. Please wait." }
```

---

## Contract 3: List Sessions

**Method**: `frapperag.api.chat.list_sessions`
**HTTP**: `POST /api/method/frapperag.api.chat.list_sessions`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Returns only sessions owned by `frappe.session.user` (enforced via `permission_query_conditions` hook — no extra check needed).

### Request

```json
{
  "include_archived": 0
}
```

| Field | Type | Required | Default |
|---|---|---|---|
| `include_archived` | int (0 or 1) | no | 0 |

### Success Response (HTTP 200)

```json
{
  "message": {
    "sessions": [
      {
        "session_id": "RAG-SESS-2026-03-16-0001",
        "title": "Unpaid invoices for ACME Corp",
        "status": "Open",
        "creation": "2026-03-16 09:15:00"
      }
    ]
  }
}
```

| Field | Type | Nullable | Description |
|---|---|---|---|
| `session_id` | string | no | Chat Session name |
| `title` | string | yes | Blank until first successful response |
| `status` | string | no | `Open` or `Archived` |
| `creation` | string | no | ISO datetime |

---

## Contract 4: Get Messages

**Method**: `frapperag.api.chat.get_messages`
**HTTP**: `POST /api/method/frapperag.api.chat.get_messages`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Verify `Chat Session.owner == frappe.session.user`. Raise `PermissionError` otherwise (FR-013).

### Request

```json
{
  "session_id": "RAG-SESS-2026-03-16-0001"
}
```

### Success Response (HTTP 200)

```json
{
  "message": {
    "messages": [
      {
        "message_id": "RAG-MSG-2026-03-16-0001",
        "role": "user",
        "content": "Show me unpaid invoices for ACME Corp",
        "citations": null,
        "status": "Completed",
        "tokens_used": 0,
        "creation": "2026-03-16 09:15:02"
      },
      {
        "message_id": "RAG-MSG-2026-03-16-0002",
        "role": "assistant",
        "content": "ACME Corp has 3 outstanding invoices...",
        "citations": "[{\"doctype\": \"Sales Invoice\", \"name\": \"SINV-00123\"}, {\"doctype\": \"Sales Invoice\", \"name\": \"SINV-00124\"}]",
        "status": "Completed",
        "tokens_used": 842,
        "creation": "2026-03-16 09:15:18"
      }
    ]
  }
}
```

| Field | Type | Nullable | Description |
|---|---|---|---|
| `message_id` | string | no | Chat Message name |
| `role` | string | no | `user` or `assistant` |
| `content` | string | no | Message text |
| `citations` | string | yes | JSON array string or null |
| `status` | string | no | `Pending`, `Completed`, or `Failed` |
| `tokens_used` | int | no | 0 for user messages |
| `creation` | string | no | ISO datetime |

### Error Responses

```json
{ "exc_type": "PermissionError", "exception": "Access denied." }
```

---

## Contract 5: Archive Session

**Method**: `frapperag.api.chat.archive_session`
**HTTP**: `POST /api/method/frapperag.api.chat.archive_session`
**Decorator**: `@frappe.whitelist()`

### Permission Guard

Verify `Chat Session.owner == frappe.session.user`. Raise `PermissionError` otherwise.

### Request

```json
{
  "session_id": "RAG-SESS-2026-03-16-0001"
}
```

### Success Response (HTTP 200)

```json
{
  "message": {
    "session_id": "RAG-SESS-2026-03-16-0001",
    "status": "Archived"
  }
}
```

### Error Responses

```json
{ "exc_type": "PermissionError", "exception": "Access denied." }
```

---

## Contract 6: Realtime Event — `rag_chat_response`

**Transport**: Frappe realtime (Socket.IO over WebSocket)
**Direction**: Server → Client (background job → session owner's browser)
**Event name**: `rag_chat_response`
**Targeting**: Published to the requesting user only (`user=user` parameter)

### Success Payload (status: Completed)

```json
{
  "message_id": "RAG-MSG-2026-03-16-0002",
  "session_id": "RAG-SESS-2026-03-16-0001",
  "status": "Completed",
  "content": "ACME Corp has 3 outstanding invoices totalling £12,400...",
  "citations": [
    {"doctype": "Sales Invoice", "name": "SINV-00123"},
    {"doctype": "Sales Invoice", "name": "SINV-00124"}
  ],
  "tokens_used": 842
}
```

| Field | Type | Description |
|---|---|---|
| `message_id` | string | Identifies which message this update resolves |
| `session_id` | string | Parent session identifier |
| `status` | string | `Completed` or `Failed` |
| `content` | string | Assistant response text (present when Completed) |
| `citations` | array | List of `{doctype, name}` objects (may be empty) |
| `tokens_used` | int | Total token count from Gemini |

### Failure Payload (status: Failed)

```json
{
  "message_id": "RAG-MSG-2026-03-16-0002",
  "session_id": "RAG-SESS-2026-03-16-0001",
  "status": "Failed",
  "error": "ResourceExhausted: quota exceeded after retry."
}
```

| Field | Type | Description |
|---|---|---|
| `error` | string | Truncated error description (≤500 chars) |

### Client-side subscription (Vanilla JS)

```javascript
frappe.realtime.on("rag_chat_response", function(data) {
    if (data.message_id !== current_message_id) return;  // guard for multiple tabs
    // handle Completed or Failed
    frappe.realtime.off("rag_chat_response");             // stop after terminal event
});
```

**One event per message**: `rag_chat_response` is published exactly once per `run_chat_job()` execution — on success or failure. There are no intermediate progress events for chat (unlike Phase 1 indexing).

---

## Internal Interface: `retriever.py`

```python
def embed_query(text: str, api_key: str) -> list[float]:
    """Embed a query string. task_type=RETRIEVAL_QUERY. Returns 768-dim vector."""

def search_all_tables(query_vector: list[float]) -> list[dict]:
    """
    Search all v1_* LanceDB tables. Returns [{doctype, name, text, _distance}].
    Returns [] if no v1_* tables exist (FR-011).
    """

def filter_by_permission(candidates: list[dict], user: str) -> list[dict]:
    """Filter candidates through frappe.has_permission(). Returns allowed subset."""
```

## Internal Interface: `prompt_builder.py`

```python
def build_messages(
    question: str,
    context_records: list[dict],  # permission-filtered [{doctype, name, text}]
    history: list[dict],          # [{role: "user"|"assistant", content: str}]
) -> list[dict]:
    """
    Returns Gemini message list for start_chat(history=...) + send_message().
    Uses EMPTY_CONTEXT_NOTE when context_records is empty (FR-012).
    """
```

## Internal Interface: `chat_engine.py`

```python
def generate_response(
    messages: list[dict],
    context_records: list[dict],
    api_key: str,
) -> dict:
    """
    Returns {"text": str, "citations": [{doctype, name}], "tokens_used": int}.
    ResourceExhausted → 60s sleep → one retry.
    All other exceptions propagate immediately (non-transient failure path).
    """
```
