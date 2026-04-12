# FrappeRAG

A Retrieval-Augmented Generation (RAG) assistant for Frappe / ERPNext. FrappeRAG indexes your business documents into a local vector store and lets users ask natural-language questions that are answered by Google Gemini, grounded in your actual data.

## Features

- **Local embedding** — `multilingual-e5-base` (sentence-transformers, ~280 MB, 768 dims) runs inside a persistent FastAPI sidecar; supports Arabic and English out of the box.
- **Vector store** — LanceDB, stored in a bench-level `rag/` directory; table prefix `v3_`.
- **Incremental sync** — Frappe `doc_events` hooks (`on_update`, `after_rename`, `on_trash`) automatically keep the vector index in sync with every save, rename, or delete for whitelisted DocTypes.
- **Bulk indexing** — RAG Index Manager page lets RAG Admins trigger a full re-index of any whitelisted DocType, with real-time progress updates via `frappe.realtime`.
- **Chat interface** — Vanilla JS chat page (`/rag-chat`) backed by Google Gemini 2.5 Flash with multi-turn conversation history and per-source citations.
- **Report execution** — Gemini can call whitelisted Report Builder reports as tools; results are rendered as formatted tables inside the chat thread with a 50-row cap.
- **Permission-aware retrieval** — every candidate record is filtered through `frappe.has_permission` before it can be included in a prompt or a chat response.
- **Roles** — two fixtures (`RAG Admin`, `RAG User`) control access. System Managers always have full access.

## Architecture

```
Frappe worker (queue=short/long)
        │
        │  httpx (localhost only)
        ▼
RAG Sidecar  ── FastAPI + uvicorn ──  LanceDB (bench-level rag/)
  /embed          multilingual-e5-base
  /upsert         sentence-transformers
  /search
  /chat           google-generativeai (Gemini 2.5 Flash)
  /record/:id DELETE
  /table/:name DELETE
```

Workers **never** import `lancedb` or `sentence_transformers` directly. All vector and embedding operations are delegated to the sidecar via `frapperag.rag.sidecar_client`.

## Project Structure

```
apps/frapperag/frapperag/
├── hooks.py                        # after_install, scheduler_events, doc_events, fixtures
├── requirements.txt
├── setup/install.py                # creates rag/ dir, adds Procfile sidecar entry
├── sidecar/
│   ├── main.py                     # FastAPI app — /embed /upsert /search /chat /record /table
│   └── store.py                    # LanceDB open/upsert/search/delete helpers
├── frapperag/doctype/
│   ├── ai_assistant_settings/      # Single DocType — API key, whitelist, roles, sidecar port
│   ├── rag_allowed_doctype/        # Child table — whitelisted DocTypes
│   ├── rag_allowed_role/           # Child table — roles allowed to chat
│   ├── rag_allowed_report/         # Child table — whitelisted Report Builder reports
│   ├── ai_indexing_job/            # Bulk index job tracking (status, progress, counters)
│   ├── chat_session/               # Chat session (Open / Archived)
│   ├── chat_message/               # Individual message (user / assistant, Pending / Completed / Failed)
│   └── sync_event_log/             # Per-record incremental sync audit log
├── rag/
│   ├── base_indexer.py             # BaseIndexer ABC (validate → check_permission → execute)
│   ├── sidecar_client.py           # httpx wrappers for every sidecar endpoint
│   ├── text_converter.py           # DocType → deterministic human-readable text (11 ERPNext DocTypes)
│   ├── indexer.py                  # DocIndexerTool + run_indexing_job() + mark_stalled_jobs()
│   ├── retriever.py                # search_candidates() + filter_by_permission()
│   ├── prompt_builder.py           # build_messages() + build_report_tool_definitions() → Gemini tool list
│   ├── chat_engine.py              # generate_response() via sidecar /chat (supports function calling)
│   ├── chat_runner.py              # run_chat_job() + _load_report_whitelist() + tool_call dispatch
│   ├── report_executor.py          # execute_report() — 3-layer permission check + Report Builder execution
│   ├── sync_hooks.py               # on_document_save/rename/trash doc_events handlers
│   └── sync_runner.py              # run_sync_job() + run_purge_job() + mark_stalled_sync_jobs() + prune_sync_event_log()
├── api/
│   ├── indexer.py                  # trigger_indexing, get_job_status, list_jobs, get_sync_health, retry_sync
│   └── chat.py                     # create_session, send_message, list_sessions, get_messages, archive_session
└── frapperag/page/
    ├── rag_admin/                  # RAG Index Manager — bulk indexing + sync health dashboard
    └── rag_chat/                   # Chat UI — session list, message thread, real-time streaming
```

## Requirements

| Dependency | Version |
|---|---|
| Python | 3.11+ |
| Frappe | v15+ |
| ERPNext | v15+ |
| lancedb | >= 0.8.0 |
| pyarrow | >= 14.0.0 |
| sentence-transformers | >= 2.7.0 |
| fastapi | >= 0.110.0 |
| uvicorn | >= 0.29.0 |
| httpx | >= 0.27.0 |
| google-generativeai | >= 0.8.0 |

## Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app https://github.com/your-org/frapperag
bench --site <site> install-app frapperag
bench --site <site> migrate
```

> **PyTorch (CPU-only)** — `requirements.txt` cannot force pip to use the CPU wheel index, so install torch separately before the rest of the dependencies:
>
> ```bash
> # Run these from your bench's Python environment
> ./env/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu
> ./env/bin/pip install -r apps/frapperag/frapperag/requirements.txt
> ```
>
> Omitting the first step causes pip to pull the default (CUDA) wheel, which is ~2 GB and unnecessary on CPU-only servers.

`after_install` automatically:
1. Creates the bench-level `rag/` directory for LanceDB data.
2. Appends a `rag_sidecar:` entry to the bench `Procfile`.

## Configuration

1. Open **AI Assistant Settings** in Frappe Desk (System Manager or RAG Admin).
2. Enter your **Gemini API Key**.
3. Add the DocTypes you want indexed to **Allowed Document Types** (11 supported — see Supported DocTypes below).
4. Add the roles that may use the chat to **Allowed Roles** (default: `RAG User`).
5. Optionally adjust **Sidecar Port** (default: `8100`).

## Running

```bash
bench start
# The Procfile launches:
#   web          — Frappe web server
#   worker:short — chat + incremental sync jobs
#   worker:long  — bulk indexing jobs
#   rag_sidecar  — FastAPI sidecar on localhost:8100
```

The sidecar loads `multilingual-e5-base` on startup (first run downloads ~280 MB). Subsequent starts reuse the cached model.

## Indexing

### Bulk indexing

Navigate to **RAG Index Manager** (`/rag-admin`) in Frappe Desk, select a DocType, and click **Start Indexing**. Progress is streamed via `frappe.realtime`. Only one active job per DocType is allowed at a time.

### Incremental sync

Every save, rename, or delete on a whitelisted DocType automatically queues a lightweight `short`-queue job that upserts or removes the single record's vector. The `Sync Event Log` DocType records the outcome of every sync attempt. RAG Admins can inspect and retry failures from the **Sync Health** section in **AI Assistant Settings**.

### Scheduler tasks

| Schedule | Task |
|---|---|
| Every 5 minutes | Mark stalled indexing jobs, chat messages, and sync log entries as Failed |
| Daily | Prune `Sync Event Log` entries older than 30 days |

## Chat

Navigate to **RAG Chat** (`/rag-chat`) in Frappe Desk. Each conversation is a `Chat Session`. Messages are sent asynchronously — the UI locks input while a response is pending and unlocks on the `rag_chat_response` realtime event.

The pipeline per message:
1. Embed the question (sidecar `/search`).
2. Retrieve top-5 candidates across all `v3_*` tables.
3. Filter by `frappe.has_permission` for the calling user.
4. Load the last 10 conversation turns.
5. Build the Gemini message list (system context + history + retrieved snippets + question).
6. Call sidecar `/chat` → Gemini 2.5 Flash.
7. Save user message (Completed) and assistant reply to `Chat Message`.
8. Publish `rag_chat_response` realtime event.

## API Reference

All endpoints require authentication via Frappe session or API key.

### Indexing (`frapperag.api.indexer`)

| Method | Description |
|---|---|
| `trigger_indexing(doctype)` | Enqueue a bulk indexing job; returns `{job_id, status}` |
| `get_job_status(job_id)` | Return current progress and counters for a job |
| `list_jobs(limit, page)` | Paginated list of AI Indexing Jobs |
| `get_sync_health()` | Per-DocType success/failure counts (last 24 h) + failed entries list |
| `retry_sync(sync_log_id)` | Re-queue a failed sync log entry |

### Chat (`frapperag.api.chat`)

| Method | Description |
|---|---|
| `create_session()` | Create a new Open Chat Session; returns `{session_id}` |
| `send_message(session_id, content)` | Queue a chat message; returns `{message_id, status}` |
| `list_sessions(include_archived)` | List caller's sessions (newest first) |
| `get_messages(session_id)` | Return all messages in a session (oldest first) |
| `archive_session(session_id)` | Transition session to Archived |

## Sidecar API

The sidecar runs on `localhost` only and is not exposed to the internet.

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check — returns `{status, model}` |
| `/embed` | POST | Embed a list of texts; returns 768-dim vectors |
| `/upsert` | POST | Embed and upsert one record into its `v3_` table |
| `/search` | POST | Embed a query and search all `v3_*` tables |
| `/chat` | POST | Call Gemini with a conversation history |
| `/record/{table}/{record_id}` | DELETE | Remove one vector entry (idempotent) |
| `/table/{table}` | DELETE | Drop an entire LanceDB table (idempotent) |

## Supported DocTypes

FrappeRAG converts the following 11 ERPNext DocTypes to text for indexing:

| DocType | Key fields indexed |
|---|---|
| **Customer** | name, type, group, territory, email, outstanding amount |
| **Item** | item name, group, UOM, standard rate, description, stock flag |
| **Sales Invoice** | invoice number, date, customer, grand total, due date, line items, outstanding amount |
| **Purchase Invoice** | bill number, date, supplier, grand total, bill date, status, line items |
| **Sales Order** | order number, date, customer, grand total, delivery date, status, line items |
| **Purchase Order** | order number, date, supplier, grand total, schedule date, status, line items |
| **Delivery Note** | DN number, date, customer, posting date, status, line items |
| **Purchase Receipt** | PR number, date, supplier, posting date, status, line items |
| **Item Price** | item code, price list, currency, rate, effective dates |
| **Stock Entry** | entry number, type, posting date, source/target warehouse, line items |
| **Supplier** | supplier name, type, group, country, email, outstanding amount |

To add more DocTypes, extend [frapperag/rag/text_converter.py](frapperag/rag/text_converter.py) with a converter function and register the DocType in `SUPPORTED_DOCTYPES`, then add it to the allowed list in **AI Assistant Settings**.

## Report Execution (Phase 5)

Administrators can whitelist **Report Builder** reports in **AI Assistant Settings → Allowed Reports**. When a user's question is best answered by running a report, Gemini selects the appropriate report as a tool call and FrappeRAG:

1. Validates the report name against the whitelist (guards against hallucinated names).
2. Confirms `report_type == "Report Builder"` at runtime.
3. Checks `frappe.has_permission` for the calling user.
4. Executes `report_doc.get_data(filters=..., limit=50)`.
5. Returns a `report_result` citation rendered as a formatted table in the chat UI (50-row cap with truncation note).

Each whitelisted report entry supports an optional `description` (fed to Gemini as the tool description) and `default_filters` (JSON, pre-populated as tool argument defaults).

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/frapperag
pre-commit install
```

Pre-commit tools configured: **ruff**, **eslint**, **prettier**, **pyupgrade**.

## License

MIT
