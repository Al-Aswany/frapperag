# FrappeRAG — Full Project Context Summary

---

### What Was Built
A custom Frappe app called `frapperag` — an AI-powered RAG assistant for ERPNext. Users ask natural language questions, the system retrieves relevant ERPNext records, and Google Gemini generates grounded answers with clickable citations. Gemini can also execute whitelisted Report Builder reports as tools and render results as formatted tables inside the chat thread.

---

### Current Status
**Phases 1–4 complete and validated. Phase 5 code complete, not yet committed.**

| Phase | Status |
|---|---|
| 1 — RAG Embedding Pipeline | ✅ Validated |
| 2 — Chat Core | ✅ Validated |
| 3 — Incremental Sync | ✅ Validated |
| 4 — DocType Coverage Expansion | ✅ Validated |
| 5 — Report Execution Mode | 🔄 Code complete, uncommitted |
| 6 — Production Hardening | Pending |

---

### Architecture

```
Frappe worker (queue=short/long)
        │  httpx (localhost only)
        ▼
RAG Sidecar (FastAPI + uvicorn, port 8100)
  /embed    — multilingual-e5-small (local, 384 dims, Arabic+English)
  /upsert   — embed + write to LanceDB
  /search   — embed query + search v4_* tables
  /chat     — Gemini 2.5 Flash (supports function calling / tool_call response)
  /record   — DELETE single vector entry
  /table    — DELETE entire table
```

Workers **never** import `lancedb` or `sentence_transformers` directly. Everything goes through `sidecar_client.py` via httpx.

---

### Constitution v3.0.1 — 7 Principles
1. **Frappe-Native Architecture** — DocTypes, `@frappe.whitelist()`, hooks.py, scheduler. One exception: the RAG sidecar.
2. **Per-Client Data Isolation** — one bench = one site; physical server isolation is sufficient.
3. **Permission-Aware RAG** — `frappe.has_permission()` at indexing, retrieval, and prompt assembly.
4. **Zero External Infrastructure** — one localhost FastAPI sidecar permitted. No Docker, no cloud vector DBs.
5. **Asynchronous-by-Default** — `frappe.enqueue` for everything heavy. HTTP handlers return immediately.
6. **Zero-Friction Installation** — `bench get-app` + `bench install-app` + API key + `bench start`.
7. **No Automated Tests** — manual acceptance only.

---

### Technology Stack
| Concern | Choice |
|---|---|
| Framework | Frappe v15+, ERPNext v15+ |
| Vector store | LanceDB (bench-level `rag/` dir, `v4_` prefix) |
| Embedding model | `multilingual-e5-small` via sentence-transformers (local, ~470 MB, 384 dims, Arabic+English) |
| Chat LLM | `gemini-2.5-flash` (paid tier, supports function calling) |
| Frontend | Vanilla JS only |
| Sidecar | FastAPI + uvicorn, `localhost:8100` |
| Key dependencies | lancedb, pyarrow, sentence-transformers, fastapi, uvicorn, httpx, google-generativeai |

---

### App Structure
```
apps/frapperag/frapperag/
├── hooks.py                        # after_install, scheduler_events, doc_events, fixtures
├── requirements.txt
├── setup/install.py                # creates rag/ dir, adds Procfile sidecar entry
├── sidecar/
│   ├── main.py                     # FastAPI app — /embed /upsert /search /chat /record /table
│   │                               # /chat supports tools= for Gemini function calling
│   └── store.py                    # LanceDB open/upsert/search/delete helpers
├── frapperag/doctype/
│   ├── ai_assistant_settings/      # Single DocType — API key, whitelist, roles, sidecar port, allowed_reports
│   ├── rag_allowed_doctype/        # Child table — whitelisted DocTypes
│   ├── rag_allowed_role/           # Child table — roles allowed to chat
│   ├── rag_allowed_report/         # Child table — whitelisted Report Builder reports (Phase 5)
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
│   ├── chat_engine.py              # generate_response() via sidecar /chat (tools= parameter supported)
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
                                    # render_report_result() renders report_result citations as HTML tables
```

---

### DocTypes Indexed (11 total — Phases 1 + 4)

| DocType | Key fields indexed |
|---|---|
| Customer | name, type, group, territory, email, outstanding amount |
| Item | item name, group, UOM, standard rate, description, stock flag |
| Sales Invoice | invoice number, date, customer, grand total, due date, line items, outstanding amount |
| Purchase Invoice | bill number, date, supplier, grand total, bill date, status, line items |
| Sales Order | order number, date, customer, grand total, delivery date, status, line items |
| Purchase Order | order number, date, supplier, grand total, schedule date, status, line items |
| Delivery Note | DN number, date, customer, posting date, status, line items |
| Purchase Receipt | PR number, date, supplier, posting date, status, line items |
| Item Price | item code, price list, currency, rate, effective dates |
| Stock Entry | entry number, type, posting date, source/target warehouse, line items |
| Supplier | supplier name, type, group, country, email, outstanding amount |

---

### Phase 5 — Report Execution Mode (code complete, not yet committed)

**What was built:**
- `rag_allowed_report` DocType — child table on AI Assistant Settings with `report` (Link→Report), `description` (Data), `default_filters` (Code/JSON)
- `ai_assistant_settings.py validate()` — Block A: rejects non-Report-Builder report types; Block B: validates `default_filters` as a JSON object
- `report_executor.py` — 3-layer guard: whitelist membership check → `report_type == "Report Builder"` live check → `frappe.has_permission`; executes via `report_doc.get_data(filters, limit=50)`; normalises list-of-dicts and list-of-lists rows; returns `{"text", "citations": [{"type": "report_result", ...}]}`
- `build_report_tool_definitions()` in `prompt_builder.py` — one Gemini function-declaration per whitelisted report; slugifies report names to valid function identifiers; builds parameter schema from `Report Filter` meta in a single query (no N+1); returns `(tool_list, slug_to_name)`
- `_load_report_whitelist()` in `chat_runner.py` — loads whitelist + filter meta at job start; passes `tools=report_tools` to `generate_response()`
- Tool call dispatch in `chat_runner.py` — detects `"tool_call"` key in Gemini response, resolves slug → real report name via `slug_to_name` map, calls `execute_report()`
- `sidecar/main.py` — `/chat` endpoint accepts `tools: list[dict] | None`; builds `genai.types.Tool` objects per-request when tools are present; detects `function_call` in response parts and returns `{"tool_call": {"name", "args"}}`
- `render_report_result()` in `rag_chat.js` — renders report citation as HTML table with column headers, row data, and 50-row truncation note

**Not yet committed** — 12 files changed (467 insertions, 231 deletions). `embedder.py` and `lancedb_store.py` were deleted (consolidated into sidecar).

---

### Key Technical Decisions Made

- `frappe.cache().get_doc("AI Assistant Settings")` in sync hooks — not `frappe.get_doc()` — avoids DB hit on every document save
- `INTERNAL_DOCTYPES` early-exit guard in sync_hooks prevents Chat Message/Session saves from triggering sync jobs
- Direct SQL (`frappe.db.sql()`) for Chat Message writes bypasses Frappe ORM lifecycle overhead
- `frappe.db.commit()` before Chat Message UPDATE to release web worker row lock
- Report whitelist loaded once per chat job at job start — O(1) membership checks via `set`
- Per-request `GenerativeModel` instance in sidecar when tools are present (bypasses cached model that has no tools configured)
- `embedder.py` and `lancedb_store.py` deleted — all vector and embedding operations consolidated in sidecar

**Timing benchmark (current):**

| Step | Time |
|---|---|
| settings_read | ~0.4s |
| search_candidates | ~1.0s |
| generate_response | ~1.4s |
| DB writes | ~2.3s |
| **Total** | **~5s** |

The 2.3s DB write is a known MariaDB row lock issue — accepted as the current floor.

---

### Workflow: GitHub Spec Kit
Every phase follows: `/speckit.constitution` → `/speckit.specify` → `/speckit.plan` → `/speckit.tasks` → `/speckit.analyze` → implement.

All specs live in `specs/00N-feature-name/` with `spec.md`, `plan.md`, `tasks.md`, `quickstart.md`.

---

### Future Phases Planned
- **Phase 6** — Production Hardening
- **Phase 7** — Reports and Dashboards via chat
- **Phase 8** — Files/Images → ERPNext documents
- **Phase 9** — Agentic Write Actions (create/edit/delete via chat)
- **Phase 10** — WhatsApp/Telegram voice integration

---

### Three Apps Studied (installed in bench)
- `apps/frappe_assistant_core` — best permission model, `BaseTool` ABC pattern
- `apps/changai` — only real RAG pipeline but violates isolation; `build_match_conditions()` pattern reused
- `apps/next_ai` — `frappe.publish_realtime` progress pattern reused
