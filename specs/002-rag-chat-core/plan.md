# Implementation Plan: RAG Chat Core — Phase 2

**Branch**: `002-rag-chat-core` | **Date**: 2026-03-16 | **Last updated**: 2026-03-16
**Spec**: `specs/002-rag-chat-core/spec.md`
**App**: `frapperag` (`apps/frapperag/`)

---

## Summary

Build the RAG retrieval and chat pipeline for the `frapperag` app. This phase delivers:
(1) a `Chat Session` DocType and `Chat Message` DocType with row-level permission isolation,
(2) a `rag/retriever.py` module that embeds queries with `gemini-embedding-001` (`RETRIEVAL_QUERY`)
and searches all `v1_*` LanceDB tables for top-K candidates filtered by `frappe.has_permission()`,
(3) a `rag/prompt_builder.py` that assembles system persona + last 10 conversation turns +
permission-filtered context into a Gemini message list,
(4) a `rag/chat_engine.py` that calls `gemini-2.5-flash` and returns response text + citations,
(5) a `rag/chat_runner.py` with `run_chat_job()` (background entry point) and
`mark_stalled_chat_messages()` (scheduler cron),
(6) an `api/chat.py` with 5 whitelisted endpoints (create session, send message, list sessions,
get messages, archive session), and
(7) a Vanilla JS `page/rag_chat` with session list, message thread, real-time response delivery,
and clickable citation links.

---

## Technical Context

| Concern | Choice | Notes |
|---|---|---|
| **Language / Version** | Python 3.11+ | Enforced by constitution |
| **Framework** | Frappe v15+, ERPNext v15+ | Enforced by constitution |
| **Vector store** | LanceDB >= 0.8.0 (read-only in Phase 2) | Tables created by Phase 1; Phase 2 searches only |
| **Embedding model** | Google `gemini-embedding-001` | `task_type=RETRIEVAL_QUERY` for queries; 768 dims |
| **Chat LLM** | Google `gemini-2.5-flash` | via `google-generativeai` >= 0.8.0; no LangChain |
| **Embedding + LLM SDK** | `google-generativeai` >= 0.8.0 | Same dep as Phase 1; no new requirements.txt entry |
| **Storage path** | `frappe.get_site_path("private/files/rag/")` | Read-only access; never hardcoded |
| **Testing** | N/A — no automated tests per Principle VII | Manual acceptance per quickstart.md |
| **Frontend** | Vanilla JS (Frappe Desk Page) | `frappe.require`, `frappe.call`, `frappe.realtime` |
| **Async** | `frappe.enqueue(queue="short", timeout=300, ...)` | Short queue: chat jobs bounded to ~30s under normal load |
| **Top-K per table** | 5 | Per spec Assumptions; tuning parameter only |
| **System persona** | Priming exchange (synthetic user/model turn) | `system_instruction` constructor preferred if SDK supports it |
| **Citation format** | `[{"doctype": str, "name": str}]` JSON in Long Text | Deduped; assembled from permission-filtered context |
| **Target platform** | Linux (Frappe bench) | Standard bench worker + Redis Queue |

---

## Constitution Check

*GATE: Must pass before implementation begins.*

| Principle | Gate | Status | Evidence |
|---|---|---|---|
| **I. Frappe-Native Architecture** | All data as DocTypes; all APIs as `@frappe.whitelist()`; no custom web server | PASS | Chat Session + Chat Message as JSON fixtures; 5 whitelist methods in api/chat.py; hooks.py for scheduler and permission_query_conditions |
| **II. Per-Client Data Isolation** | LanceDB at `frappe.get_site_path()`; `site=frappe.local.site` in enqueue; all heavy imports inside job function | PASS | `retriever.py` opens LanceDB inside functions via `frappe.get_site_path()` exclusively; `message_id` kwarg (not `job_id`) passed; all google.generativeai imports inside `run_chat_job()` and module functions |
| **III. Permission-Aware RAG Retrieval** | `frappe.set_user(user)` at job start; `frappe.has_permission()` per candidate record before LLM call | PASS | `filter_by_permission()` called after `search_all_tables()`, before `build_messages()`; `permission_query_conditions` on both DocTypes for list-level isolation |
| **IV. Zero External Infrastructure** | Only LanceDB (file) + Google Gemini API; no Docker, no cloud DB, no separate servers | PASS | No new dependencies; `google-generativeai` already in requirements.txt from Phase 1 |
| **V. Asynchronous-by-Default** | `send_message()` creates DocType record + enqueues + returns message_id; zero blocking I/O in handler | PASS | All embedding, LanceDB search, and LLM calls are inside `run_chat_job()`; whitelist method returns in <1s |
| **VI. Zero-Friction Installation** | DocType JSON fixtures; no manual steps beyond API key | PASS | Two new DocType JSON files committed; scheduler entry added to hooks.py; no new pip dependencies; `bench migrate` creates tables automatically |
| **VII. No Automated Tests** | No test files; no test dependencies; no test tasks | PASS | quickstart.md manual acceptance checklist only |

**All 7 principles pass. Implementation may proceed.**

---

## Project Structure

### Documentation (this feature)

```
specs/002-rag-chat-core/
├── spec.md               ← feature specification (Validated)
├── plan.md               ← this file
├── research.md           ← technical decisions and rationale
├── data-model.md         ← DocType definitions and LanceDB search pattern
├── quickstart.md         ← manual acceptance validation guide
├── contracts/
│   └── api-contracts.md  ← whitelist method and realtime event contracts
└── checklists/
    └── requirements.md   ← spec quality checklist (all passed)
```

### Source Code (`apps/frapperag/frapperag/`)

```
apps/frapperag/frapperag/
│
├── hooks.py                                  # +mark_stalled_chat_messages cron entry
│                                             # +permission_query_conditions for both DocTypes
│
├── frapperag/
│   └── doctype/
│       ├── chat_session/
│       │   ├── __init__.py
│       │   ├── chat_session.json             # Standard DocType; RAG-SESS- naming series
│       │   └── chat_session.py               # permission_query_conditions()
│       └── chat_message/
│           ├── __init__.py
│           ├── chat_message.json             # Standard DocType; RAG-MSG- naming series
│           └── chat_message.py               # permission_query_conditions()
│
├── rag/
│   ├── retriever.py                          # embed_query() + search_all_tables() + filter_by_permission()
│   ├── prompt_builder.py                     # build_messages() → Gemini message list
│   ├── chat_engine.py                        # generate_response() → {text, citations, tokens_used}
│   └── chat_runner.py                        # run_chat_job() + mark_stalled_chat_messages()
│
├── api/
│   └── chat.py                               # 5 @frappe.whitelist() endpoints
│
└── page/
    └── rag_chat/
        ├── rag_chat.json                     # Page definition (title, module, roles)
        └── rag_chat.js                       # Vanilla JS: session list + thread + realtime
```

---

## Module Design

### `hooks.py` — additions

```python
# Append mark_stalled_chat_messages to the existing cron list:
scheduler_events = {
    "cron": {
        "*/30 * * * *": [
            "frapperag.rag.indexer.mark_stalled_jobs",               # Phase 1
            "frapperag.rag.chat_runner.mark_stalled_chat_messages",  # Phase 2
        ],
    }
}

# Row-level permission filters for Phase 2 DocTypes:
permission_query_conditions = {
    "Chat Session": "frapperag.frapperag.doctype.chat_session.chat_session.permission_query_conditions",
    "Chat Message": "frapperag.frapperag.doctype.chat_message.chat_message.permission_query_conditions",
}
```

---

### `frapperag/doctype/chat_session/chat_session.py`

```python
import frappe


def permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    return f'`tabChat Session`.`owner` = {frappe.db.escape(user)}'
```

---

### `frapperag/doctype/chat_message/chat_message.py`

```python
import frappe


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

---

### `rag/retriever.py` — Query Embedding + Vector Search + Permission Filter

```python
import frappe

EMBEDDING_MODEL  = "models/gemini-embedding-001"
EMBEDDING_DIMS   = 768
TOP_K            = 5
RATE_LIMIT_SLEEP = 60.0
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 2.0


def embed_query(text: str, api_key: str) -> list:
    """
    Embed a single query string using gemini-embedding-001 with task_type=RETRIEVAL_QUERY.
    All imports inside function — no module-level state.
    ResourceExhausted → 60s sleep → retry. Other errors → exponential back-off → raise.
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    delay    = RETRY_BASE_DELAY
    last_exc = None

    for attempt in range(MAX_RETRIES):
        try:
            response = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=EMBEDDING_DIMS,
            )
            return response["embedding"]
        except ResourceExhausted as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RATE_LIMIT_SLEEP)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2

    raise RuntimeError(
        f"Query embedding failed after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def search_all_tables(query_vector: list) -> list:
    """
    Search all v1_* LanceDB tables for top-K results.
    Returns list of dicts: {doctype, name, text, _distance}.
    Returns [] if no v1_* tables exist (FR-011: friendly message path, not exception).
    All lancedb imports inside function — no module-level state.
    Path always via frappe.get_site_path() — never hardcoded.
    """
    import lancedb

    rag_path = frappe.get_site_path("private", "files", "rag")
    db       = lancedb.connect(rag_path)

    table_names = [t for t in db.table_names() if t.startswith("v1_")]
    if not table_names:
        return []

    results = []
    for table_name in table_names:
        table = db.open_table(table_name)
        rows  = (
            table.search(query_vector, vector_column_name="vector")
            .limit(TOP_K)
            .to_list()
        )
        for row in rows:
            results.append({
                "doctype":   row["doctype"],
                "name":      row["name"],
                "text":      row["text"],
                "_distance": row.get("_distance", 0),
            })

    results.sort(key=lambda r: r["_distance"])  # ascending: lower = more relevant
    return results


def filter_by_permission(candidates: list, user: str) -> list:
    """
    Filter retrieval candidates through frappe.has_permission() for the calling user.
    Returns only records the user is authorised to read (Principle III).
    Called after search_all_tables(), before build_messages().
    """
    allowed = []
    for candidate in candidates:
        if frappe.has_permission(
            candidate["doctype"],
            doc=candidate["name"],
            ptype="read",
            user=user,
        ):
            allowed.append(candidate)
    return allowed
```

---

### `rag/prompt_builder.py` — Gemini Message Assembly

```python
SYSTEM_PERSONA = (
    "You are a helpful business assistant with access to the company's ERP data. "
    "Answer questions based only on the provided context. "
    "If the context is empty or insufficient, say so clearly — do not fabricate information. "
    "When referencing source documents, mention them by type and identifier."
)

EMPTY_CONTEXT_NOTE = (
    "[No accessible context was found for this query. "
    "The user may not have permission to view relevant records, "
    "or no data has been indexed yet. Respond helpfully but do not invent information.]"
)


def build_messages(question: str, context_records: list, history: list) -> list:
    """
    Assemble the Gemini message list for start_chat(history=...) + send_message().

    Args:
        question:        The user's current question.
        context_records: Permission-filtered retrieval results [{doctype, name, text}].
                         May be empty (FR-012: EMPTY_CONTEXT_NOTE injected instead).
        history:         Last <= 10 prior turns [{role: "user"|"assistant", content: str}].

    Returns: list of {"role": "user"|"model", "parts": [str]} dicts.
    The last item is the current user turn (passed to send_message()).
    """
    messages = []

    # Priming exchange: sets system persona (synthetic user/model opening turn)
    messages.append({"role": "user",  "parts": [SYSTEM_PERSONA]})
    messages.append({"role": "model", "parts": ["Understood. I will answer based only on provided context."]})

    # Conversation history (oldest-first, max 10 turns)
    for turn in history[-10:]:
        role = "model" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [turn["content"]]})

    # Context block + current question (final user turn)
    if context_records:
        context_text = "\n\n".join(
            f"[{r['doctype']} / {r['name']}]\n{r['text']}"
            for r in context_records
        )
        user_turn = f"Context from ERP data:\n{context_text}\n\nQuestion: {question}"
    else:
        user_turn = f"{EMPTY_CONTEXT_NOTE}\n\nQuestion: {question}"

    messages.append({"role": "user", "parts": [user_turn]})
    return messages
```

---

### `rag/chat_engine.py` — Gemini 2.5 Flash Caller

```python
CHAT_MODEL       = "gemini-2.5-flash"
RATE_LIMIT_SLEEP = 60.0  # seconds; matches Phase 1 embedder pattern (FR-015)


def generate_response(messages: list, context_records: list, api_key: str) -> dict:
    """
    Call gemini-2.5-flash with the assembled message list.
    Returns {"text": str, "citations": [{doctype, name}], "tokens_used": int}.

    Rate-limit handling (FR-015):
      ResourceExhausted → 60s flat sleep → one retry.
      All other exceptions propagate immediately (non-transient failure — fail fast).

    All google.generativeai imports inside function — no module-level state.
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(CHAT_MODEL)

    # history = all messages except the final user turn
    history      = messages[:-1]
    last_message = messages[-1]["parts"][0]
    chat         = model.start_chat(history=history)

    response = None
    for attempt in range(2):   # one retry on rate-limit only
        try:
            response = chat.send_message(last_message)
            break
        except ResourceExhausted:
            if attempt == 0:
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            raise
        # All other exceptions propagate immediately — non-transient

    text        = response.text
    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = getattr(response.usage_metadata, "total_token_count", 0)

    # Build deduplicated citation list from permission-filtered context records
    seen      = set()
    citations = []
    for r in context_records:
        key = (r["doctype"], r["name"])
        if key not in seen:
            seen.add(key)
            citations.append({"doctype": r["doctype"], "name": r["name"]})

    return {"text": text, "citations": citations, "tokens_used": tokens_used}
```

---

### `rag/chat_runner.py` — Background Job + Scheduler

```python
import frappe
from frappe.utils import now_datetime, add_to_date
import json


def run_chat_job(message_id: str, session_id: str, user: str, **kwargs):
    """
    Background job entry point. Called by frappe.enqueue (queue="short").
    Site context already initialised by the Frappe worker.

    api_key is read from AI Assistant Settings here — NOT passed via enqueue kwargs
    (keeps credential out of Redis serialisation — same pattern as Phase 1).

    message_id kwarg name avoids collision with Frappe/RQ reserved 'job_id' kwarg.
    """
    from frapperag.rag.retriever      import embed_query, search_all_tables, filter_by_permission
    from frapperag.rag.prompt_builder import build_messages
    from frapperag.rag.chat_engine    import generate_response

    # Read api_key from Settings — never from enqueue kwargs
    api_key = frappe.get_doc("AI Assistant Settings").get_password("gemini_api_key")

    # Enforce the calling user's permission context (Principle III)
    frappe.set_user(user)

    try:
        msg      = frappe.get_doc("Chat Message", message_id)
        question = msg.content

        # 1. Embed the query (RETRIEVAL_QUERY task type)
        query_vector = embed_query(question, api_key)

        # 2. Search all v1_* LanceDB tables
        candidates = search_all_tables(query_vector)

        # 3. Filter by user permissions per-record (Principle III)
        filtered = filter_by_permission(candidates, user)

        # 4. Load last 10 conversation turns (excluding the current Pending message)
        history_docs = frappe.db.get_all(
            "Chat Message",
            filters={"session": session_id, "name": ["!=", message_id]},
            fields=["role", "content"],
            order_by="creation desc",
            limit=10,
            ignore_permissions=False,
        )
        history = [{"role": d.role, "content": d.content} for d in reversed(history_docs)]

        # 5. Build Gemini message list
        messages = build_messages(question, filtered, history)

        # 6. Generate response (gemini-2.5-flash)
        result = generate_response(messages, filtered, api_key)

        # 7. Update the user's Pending message to Completed
        frappe.db.set_value("Chat Message", message_id, {"status": "Completed"})
        frappe.db.commit()

        # 8. Insert assistant reply message
        reply = frappe.get_doc({
            "doctype":     "Chat Message",
            "session":     session_id,
            "role":        "assistant",
            "content":     result["text"],
            "citations":   json.dumps(result["citations"]),
            "status":      "Completed",
            "tokens_used": result["tokens_used"],
        })
        reply.insert(ignore_permissions=True)
        frappe.db.commit()

        # 9. Set session title from first user question (FR-002)
        #    Only written when title is blank — idempotent on retry.
        session = frappe.get_doc("Chat Session", session_id)
        if not session.title:
            frappe.db.set_value("Chat Session", session_id, "title", question[:80].strip())
            frappe.db.commit()

        # 10. Publish realtime response to the user (FR-014)
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id":  message_id,
                "session_id":  session_id,
                "status":      "Completed",
                "content":     result["text"],
                "citations":   result["citations"],
                "tokens_used": result["tokens_used"],
            },
            user=user,
            after_commit=False,
        )

    except Exception:
        import traceback
        tb = traceback.format_exc()
        frappe.db.set_value(
            "Chat Message",
            message_id,
            {"status": "Failed", "error_detail": tb[:2000]},
        )
        frappe.db.commit()
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id": message_id,
                "session_id": session_id,
                "status":     "Failed",
                "error":      tb[:500],
            },
            user=user,
            after_commit=False,
        )
        frappe.log_error(
            title=f"RAG Chat Job Failed [{message_id}]",
            message=tb,
        )


def mark_stalled_chat_messages():
    """
    Scheduler (every 30 min): transition Pending messages older than 10 minutes to Failed.
    Covers worker crashes and jobs that never dequeued (FR-016, SC-005).
    """
    cutoff  = add_to_date(now_datetime(), minutes=-10)
    stalled = frappe.db.get_all(
        "Chat Message",
        filters={"status": "Pending", "creation": ["<", cutoff]},
        pluck="name",
    )
    for name in stalled:
        frappe.db.set_value(
            "Chat Message",
            name,
            {
                "status":       "Failed",
                "error_detail": "Message exceeded 10-minute processing timeout. Worker may have crashed.",
            },
        )
    if stalled:
        frappe.db.commit()
```

---

### `api/chat.py` — Whitelisted HTTP Endpoints

```python
import frappe
import json


def _assert_session_owner(session_id: str):
    """
    Raises frappe.PermissionError immediately if the caller does not own the session.
    Returns the session doc on success (FR-013).
    """
    if not frappe.db.exists("Chat Session", session_id):
        frappe.throw(f"Chat Session '{session_id}' not found.", frappe.DoesNotExistError)
    session = frappe.get_doc("Chat Session", session_id)
    if session.owner != frappe.session.user:
        frappe.throw("Access denied.", frappe.PermissionError)
    return session


@frappe.whitelist()
def create_session() -> dict:
    """
    Create a new Open Chat Session and return its ID synchronously (FR-018).
    title is left blank — set by run_chat_job() after first successful response (FR-002).
    """
    session = frappe.get_doc({
        "doctype": "Chat Session",
        "status":  "Open",
        "title":   "",
    })
    session.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"session_id": session.name}


@frappe.whitelist()
def send_message(session_id: str, content: str) -> dict:
    """
    Create a Pending Chat Message, enqueue run_chat_job, return message_id immediately.
    Server-side guard: rejects if any Pending message already exists in this session (FR-019).
    message_id kwarg avoids Frappe/RQ reserved 'job_id' collision.
    """
    _assert_session_owner(session_id)

    # Server-side enforcement of FR-019 (mirrors UI input lock)
    if frappe.db.exists("Chat Message", {"session": session_id, "status": "Pending"}):
        frappe.throw(
            "A message is already being processed in this session. Please wait.",
            frappe.ValidationError,
        )

    msg = frappe.get_doc({
        "doctype": "Chat Message",
        "session": session_id,
        "role":    "user",
        "content": content,
        "status":  "Pending",
    })
    msg.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.enqueue(
        "frapperag.rag.chat_runner.run_chat_job",
        queue="short",
        timeout=300,
        site=frappe.local.site,   # explicit site: per-client isolation (Principle II)
        message_id=msg.name,      # NOT job_id — reserved by Frappe/RQ
        session_id=session_id,
        user=frappe.session.user,
    )
    return {"message_id": msg.name, "status": "Pending"}


@frappe.whitelist()
def list_sessions(include_archived: int = 0) -> dict:
    """Return the current user's chat sessions, newest first."""
    filters = {"owner": frappe.session.user}
    if not int(include_archived):
        filters["status"] = "Open"
    sessions = frappe.db.get_all(
        "Chat Session",
        filters=filters,
        fields=["name", "title", "status", "creation"],
        order_by="creation desc",
        ignore_permissions=False,
    )
    return {"sessions": [dict(s, session_id=s.name) for s in sessions]}


@frappe.whitelist()
def get_messages(session_id: str) -> dict:
    """Return all messages for a session the caller owns, ordered oldest-first."""
    _assert_session_owner(session_id)
    messages = frappe.db.get_all(
        "Chat Message",
        filters={"session": session_id},
        fields=["name", "role", "content", "citations", "status", "tokens_used", "creation"],
        order_by="creation asc",
        ignore_permissions=False,
    )
    return {"messages": [dict(m, message_id=m.name) for m in messages]}


@frappe.whitelist()
def archive_session(session_id: str) -> dict:
    """Transition a session from Open to Archived (FR-020)."""
    _assert_session_owner(session_id)
    frappe.db.set_value("Chat Session", session_id, "status", "Archived")
    frappe.db.commit()
    return {"session_id": session_id, "status": "Archived"}
```

---

### `page/rag_chat/rag_chat.js` — Vanilla JS Chat UI

```javascript
frappe.pages["rag-chat"].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "AI Assistant",
        single_column: true,
    });

    $(`
        <div class="rag-chat-layout" style="display:flex; height:calc(100vh - 100px);">
            <div class="rag-sessions" style="width:260px; border-right:1px solid #eee; overflow-y:auto; padding:12px;">
                <button id="rag-new-session" class="btn btn-sm btn-primary" style="width:100%; margin-bottom:8px;">New Chat</button>
                <div id="rag-session-list"></div>
            </div>
            <div class="rag-thread" style="flex:1; display:flex; flex-direction:column;">
                <div id="rag-messages" style="flex:1; overflow-y:auto; padding:16px;"></div>
                <div style="padding:12px; border-top:1px solid #eee; display:flex; gap:8px;">
                    <input id="rag-input" type="text" class="form-control"
                           placeholder="Ask a question about your data…" style="flex:1;" disabled />
                    <button id="rag-send" class="btn btn-primary" disabled>Send</button>
                </div>
            </div>
        </div>
    `).appendTo(page.main);

    var current_session_id = null;
    var current_message_id = null;

    // ── Session list ──────────────────────────────────────────────────────────

    function load_sessions() {
        frappe.call({
            method: "frapperag.api.chat.list_sessions",
            args: { include_archived: 0 },
            callback: function(r) {
                var sessions = r.message.sessions || [];
                var $list = $("#rag-session-list").empty();
                sessions.forEach(function(s) {
                    var active = s.session_id === current_session_id ? "background:#f0f4ff;" : "";
                    $(`
                        <div class="rag-session-item" data-id="${s.session_id}"
                             style="padding:8px; cursor:pointer; border-radius:4px; margin-bottom:4px;
                                    display:flex; justify-content:space-between; align-items:center; ${active}">
                            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:180px;">
                                ${frappe.utils.escape_html(s.title || "New Chat")}
                            </span>
                            <button class="btn btn-xs btn-default rag-archive-btn"
                                    data-id="${s.session_id}" title="Archive">⋯</button>
                        </div>
                    `).appendTo($list);
                });
            }
        });
    }

    // ── Message thread ────────────────────────────────────────────────────────

    function load_messages(session_id) {
        frappe.call({
            method: "frapperag.api.chat.get_messages",
            args: { session_id: session_id },
            callback: function(r) {
                var messages = r.message.messages || [];
                var $msgs = $("#rag-messages").empty();
                messages.forEach(function(m) { render_message(m, $msgs); });
                $msgs.scrollTop($msgs[0].scrollHeight);
                // Re-lock if a Pending message exists (e.g., page reload mid-job)
                var pending = messages.find(function(m) { return m.status === "Pending"; });
                set_input_locked(!!pending);
                if (pending) {
                    current_message_id = pending.message_id;
                    subscribe_realtime(current_message_id);
                }
            }
        });
    }

    function render_message(m, $container) {
        var is_user     = m.role === "user";
        var status_note = m.status === "Pending" ? " <span style='color:#aaa;font-size:11px;'>(thinking…)</span>"
                        : m.status === "Failed"  ? " <span style='color:red;font-size:11px;'>(failed)</span>"
                        : "";
        var citations_html = "";
        if (m.citations) {
            try {
                var cites = typeof m.citations === "string" ? JSON.parse(m.citations) : m.citations;
                if (cites && cites.length) {
                    citations_html = "<div style='margin-top:6px; font-size:11px;'>" +
                        cites.map(function(c) {
                            var slug = frappe.router.slug(c.doctype);
                            return "<a href='/app/" + slug + "/" + c.name + "' target='_blank' "
                                 + "style='margin-right:8px; color:#5e64ff;'>"
                                 + frappe.utils.escape_html(c.doctype) + ": "
                                 + frappe.utils.escape_html(c.name) + "</a>";
                        }).join("") + "</div>";
                }
            } catch(e) {}
        }
        $(`
            <div class="rag-msg rag-msg-${m.role}" data-id="${m.message_id || ''}"
                 style="margin-bottom:12px; text-align:${is_user ? 'right' : 'left'};">
                <div style="display:inline-block; max-width:75%; padding:10px 14px; border-radius:12px;
                            background:${is_user ? '#5e64ff' : '#f5f5f5'};
                            color:${is_user ? '#fff' : '#333'};">
                    ${frappe.utils.escape_html(m.content || "")}${status_note}
                    ${citations_html}
                </div>
            </div>
        `).appendTo($container);
    }

    function set_input_locked(locked) {
        $("#rag-input, #rag-send").prop("disabled", !!locked);
    }

    // ── New session ───────────────────────────────────────────────────────────

    $("#rag-new-session").on("click", function() {
        frappe.call({
            method: "frapperag.api.chat.create_session",
            callback: function(r) {
                current_session_id = r.message.session_id;
                current_message_id = null;
                frappe.realtime.off("rag_chat_response");
                $("#rag-messages").empty();
                set_input_locked(false);
                load_sessions();
                $("#rag-input").focus();
            }
        });
    });

    // ── Session click ─────────────────────────────────────────────────────────

    $(document).on("click", ".rag-session-item", function(e) {
        if ($(e.target).hasClass("rag-archive-btn")) return;
        var sid = $(this).data("id");
        if (sid === current_session_id) return;
        current_session_id = sid;
        current_message_id = null;
        frappe.realtime.off("rag_chat_response");
        load_sessions();
        load_messages(sid);
    });

    // ── Archive ───────────────────────────────────────────────────────────────

    $(document).on("click", ".rag-archive-btn", function(e) {
        e.stopPropagation();
        var sid = $(this).data("id");
        frappe.confirm("Archive this chat session?", function() {
            frappe.call({
                method: "frapperag.api.chat.archive_session",
                args: { session_id: sid },
                callback: function() {
                    if (sid === current_session_id) {
                        current_session_id = null;
                        current_message_id = null;
                        frappe.realtime.off("rag_chat_response");
                        $("#rag-messages").empty();
                        set_input_locked(true);
                    }
                    load_sessions();
                }
            });
        });
    });

    // ── Send message ──────────────────────────────────────────────────────────

    function send_message() {
        var content = $("#rag-input").val().trim();
        if (!content || !current_session_id) return;
        set_input_locked(true);
        $("#rag-input").val("");

        // Optimistic user bubble
        var $msgs = $("#rag-messages");
        render_message({role: "user", content: content, status: "Completed"}, $msgs);

        // Pending assistant bubble
        var $pending = $(
            "<div class='rag-msg rag-msg-assistant rag-pending-bubble' style='margin-bottom:12px;'>" +
            "<div style='display:inline-block; padding:10px 14px; border-radius:12px; background:#f5f5f5; color:#aaa;'>" +
            "Thinking…</div></div>"
        ).appendTo($msgs);
        $msgs.scrollTop($msgs[0].scrollHeight);

        frappe.call({
            method: "frapperag.api.chat.send_message",
            args: { session_id: current_session_id, content: content },
            callback: function(r) {
                current_message_id = r.message.message_id;
                subscribe_realtime(current_message_id);
            },
            error: function() {
                $pending.remove();
                set_input_locked(false);
            }
        });
    }

    $("#rag-send").on("click", send_message);
    $("#rag-input").on("keydown", function(e) {
        if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send_message(); }
    });

    // ── Realtime subscription ─────────────────────────────────────────────────

    function subscribe_realtime(message_id) {
        frappe.realtime.off("rag_chat_response");
        frappe.realtime.on("rag_chat_response", function(data) {
            // Guard: ignore events not belonging to the current in-flight message (FR-014)
            if (data.message_id !== message_id) return;

            $(".rag-pending-bubble").remove();
            var $msgs = $("#rag-messages");

            if (data.status === "Completed") {
                var citations_html = "";
                if (data.citations && data.citations.length) {
                    citations_html = "<div style='margin-top:6px; font-size:11px;'>" +
                        data.citations.map(function(c) {
                            var slug = frappe.router.slug(c.doctype);
                            return "<a href='/app/" + slug + "/" + c.name + "' target='_blank' "
                                 + "style='margin-right:8px; color:#5e64ff;'>"
                                 + frappe.utils.escape_html(c.doctype) + ": "
                                 + frappe.utils.escape_html(c.name) + "</a>";
                        }).join("") + "</div>";
                }
                $(`
                    <div class="rag-msg rag-msg-assistant" style="margin-bottom:12px;">
                        <div style="display:inline-block; max-width:75%; padding:10px 14px;
                                    border-radius:12px; background:#f5f5f5; color:#333;">
                            ${frappe.utils.escape_html(data.content || "")}
                            ${citations_html}
                        </div>
                    </div>
                `).appendTo($msgs);
            } else if (data.status === "Failed") {
                frappe.msgprint({
                    message: "The AI assistant encountered an error. Please try again.",
                    indicator: "red",
                });
            }

            $msgs.scrollTop($msgs[0].scrollHeight);
            set_input_locked(false);
            frappe.realtime.off("rag_chat_response");
            current_message_id = null;
            load_sessions();  // refresh sidebar title after first completed response
        });
    }

    // ── Init ──────────────────────────────────────────────────────────────────
    load_sessions();
};
```

---

## Complexity Tracking

> No constitution violations requiring justification.

| Potential Concern | Resolution |
|---|---|
| `message_id` vs `job_id` kwarg collision | Frappe/RQ reserves `job_id` as an internal enqueue parameter. Phase 2 uses `message_id` in `frappe.enqueue(...)` kwargs — same solution as Phase 1's `indexing_job_id`. |
| `api_key` security in background jobs | `api_key` is NOT passed via `frappe.enqueue` kwargs (serialised to Redis). Read from `AI Assistant Settings` at the start of `run_chat_job()` — keeps credential out of the queue. Identical pattern to Phase 1. |
| `queue="short"` vs `queue="long"` | Chat jobs use `queue="short"` (timeout 300s); Phase 1 indexing uses `queue="long"` (7200s). Workers for both queues run independently — short-queue chat jobs do not compete with long-running indexing jobs for the same worker slot. |
| Session title written in background job | `frappe.db.set_value("Chat Session", ...)` is called only when `session.title` is blank — idempotent on retry. Title is not overwritten if the job is re-executed for any reason. |
| `permission_query_conditions` subquery for Chat Message | The subquery `tabChat Message.session IN (SELECT name FROM tabChat Session WHERE owner = ...)` is the correct Frappe row-level pattern. For Phase 2 data volumes (hundreds of rows), performance is acceptable. A compound index or JOIN optimisation can be added in a future phase if profiling shows regression. |
| No indexed tables → friendly message | `search_all_tables()` returns `[]` when no `v1_*` tables exist. `build_messages()` detects empty `context_records` and injects `EMPTY_CONTEXT_NOTE`. The LLM produces a helpful "no data available" response — no exception raised (FR-011). |
| `frappe.set_user(user)` inside job | Each Frappe short-queue worker handles one job at a time (RQ semantics). `set_user` is process-local state, not shared across concurrent workers. Identical reasoning to Phase 1. |
| Concurrent Pending message guard | `send_message()` performs a `frappe.db.exists()` check for Pending messages before inserting. The UI also locks the input (FR-019). Both guards are required: the server-side check protects against direct API calls that bypass the UI lock. |

---

## Post-Design Constitution Check

Re-verified after all modules designed:

| Principle | Status |
|---|---|
| I. Frappe-Native Architecture | PASS — 2 new DocTypes as JSON fixtures; 5 whitelist methods; hooks.py only |
| II. Per-Client Data Isolation | PASS — `frappe.get_site_path()` for LanceDB; `site=frappe.local.site` in enqueue; all imports inside job/module functions |
| III. Permission-Aware RAG Retrieval | PASS — `frappe.set_user(user)` at job start; `filter_by_permission()` per-record before LLM call; `permission_query_conditions` on both DocTypes |
| IV. Zero External Infrastructure | PASS — no new pip dependencies; `google-generativeai` already in requirements.txt |
| V. Asynchronous-by-Default | PASS — `send_message()` returns `message_id` in <1s; all LLM/embedding/LanceDB work inside `run_chat_job()` |
| VI. Zero-Friction Installation | PASS — DocType JSON fixtures; `bench migrate` creates tables; scheduler entry in hooks.py; no new pip packages |
| VII. No Automated Tests | PASS — no test files, no pytest, no test tasks |
