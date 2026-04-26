# FrappeRAG — Master Project Document

*Consolidated reference. Last updated: April 2026.*

---

## 1. What FrappeRAG Is

A custom Frappe app (`frapperag`) that turns ERPNext into an AI-queryable operations layer. Users ask natural-language questions in Arabic or English; the system retrieves relevant ERPNext records, filters them through Frappe permissions, and has Google Gemini 2.5 Flash generate grounded answers with clickable citations. Gemini can also call whitelisted Report Builder reports and parameterized SQL templates as tools, returning formatted tables inline in the chat thread.

Everything runs **inside the Frappe bench**. No Docker, no cloud vector DB, no data export. The only outbound call is to the Gemini API.

---

## 2. Current Status (as of v1.2)

| Phase | Name | Status |
|---|---|---|
| 1 | RAG Embedding Pipeline | ✅ Shipped |
| 2 | Chat Core | ✅ Shipped |
| 3 | Incremental Sync | ✅ Shipped |
| 4 | DocType Coverage (11 types) | ✅ Shipped |
| 5 | Report Execution Mode | ✅ Shipped |
| 6 | Production Hardening | ✅ Shipped |
| 7 | Chat Quality Test Matrix | ✅ Shipped (30/30) |
| 8 | v1.0 — ExecuteQuery + SQL Templates | ✅ Shipped |
| 8.5 | v1.1 — Parametric `aggregate_doctype` | ✅ Shipped (34/35, 97%) |
| 7a | v1.2 — Pluggable Embedding Provider | ✅ Shipped (49/50, 98%) |
| 8.75 | v1.3 — Text-to-SQL with Guardrails | 🔜 Next candidate |
| 9 | Files & Images | 📋 Planned |
| 9.5 | Google Search Grounding | 📋 Planned |
| 10 | Agentic Write Actions | 📋 Planned |
| 11 | WhatsApp/Telegram Voice | 📋 Planned |

**Latest tag:** `v1.2`
**Test matrix:** 49/50 passing (one failure: CH-05 record_lookup citation missing for PUR-ORD-2026-00077, logged to backlog)

---

## 3. Constitution — 7 Principles (v3.0.1)

1. **Frappe-Native Architecture** — DocTypes, `@frappe.whitelist()`, hooks.py, scheduler. Only one exception: the RAG sidecar.
2. **Per-Client Data Isolation** — one bench = one site; physical server isolation is sufficient.
3. **Permission-Aware RAG** — `frappe.has_permission()` at indexing, retrieval, and prompt assembly.
4. **Zero External Infrastructure** — one localhost FastAPI sidecar permitted. No Docker, no cloud vector DBs.
5. **Asynchronous-by-Default** — `frappe.enqueue` for everything heavy. HTTP handlers return immediately.
6. **Zero-Friction Installation** — `bench get-app` + `bench install-app` + API key + `bench start`.
7. **No Automated Tests** — manual acceptance only (with per-phase test matrices).

---

## 4. Architecture

```
Frappe worker (queue=short/long)
        │  httpx (localhost only)
        ▼
RAG Sidecar (FastAPI + uvicorn, port 8100)
  /embed    — active provider: gemini-embedding-001 (cloud) OR multilingual-e5-small (local)
  /upsert   — embed + write to LanceDB (v5_gemini_* or v6_e5small_*)
  /search   — embed query + search active-prefix tables
  /chat     — Gemini 2.5 Flash (supports function calling / tool_call response)
  /record   — DELETE single vector entry
  /table    — DELETE entire table
  /tables/populated  — list populated tables for active prefix
  /install_local_model        — background download of multilingual-e5-small
  /install_local_model/status — poll install progress
```

Provider is selected at sidecar startup via `EMBEDDING_PROVIDER` env var (`gemini` or `e5-small`). Changing it in AI Assistant Settings rewrites the Procfile/supervisor entry automatically.

Workers **never** import `lancedb` or `sentence_transformers` directly — everything goes through `sidecar_client.py` via httpx.

### Technology Stack

| Concern | Choice |
|---|---|
| Framework | Frappe v15+, ERPNext v15+ |
| Python | 3.10+ |
| Vector store | LanceDB (bench-level `rag/` dir, `v5_gemini_*` or `v6_e5small_*` prefix) |
| Embedding (default) | `gemini-embedding-001` via REST v1beta, 768-dim, cloud (Google AI Studio key) |
| Embedding (opt-in) | `multilingual-e5-small` (sentence-transformers, ~470 MB, 384-dim, fully local) |
| Chat LLM | `gemini-2.5-flash` (paid tier, function calling enabled) |
| Sidecar | FastAPI + uvicorn on `localhost:8100` |
| Frontend | Vanilla JS only |
| Key deps | lancedb, pyarrow, sentence-transformers, fastapi, uvicorn, httpx, google-generativeai |

---

## 5. App Structure

```
apps/frapperag/frapperag/
├── hooks.py                        # after_install, scheduler_events, doc_events, fixtures
├── requirements.txt
├── setup/install.py                # creates rag/ dir, adds Procfile sidecar entry
├── sidecar/
│   ├── main.py                     # /embed /upsert /search /chat /record /table
│   └── store.py                    # LanceDB helpers
├── frapperag/doctype/
│   ├── ai_assistant_settings/      # API key, whitelist, roles, sidecar port
│   ├── rag_allowed_doctype/        # + date_field column (v1.1)
│   ├── rag_allowed_role/
│   ├── rag_allowed_report/         # Phase 5
│   ├── rag_aggregate_field/        # v1.1 — fail-closed group_by/aggregate perms
│   ├── ai_indexing_job/
│   ├── chat_session/
│   ├── chat_message/
│   └── sync_event_log/
├── rag/
│   ├── base_indexer.py
│   ├── sidecar_client.py
│   ├── text_converter.py           # 11 DocType converters
│   ├── indexer.py
│   ├── retriever.py
│   ├── prompt_builder.py           # + build_report_tool_definitions()
│   ├── chat_engine.py
│   ├── chat_runner.py              # tool_call dispatch
│   ├── report_executor.py          # Phase 5 — 3-layer permission guard
│   ├── query_executor.py           # v1.0 templates + v1.1 aggregate_doctype
│   ├── sync_hooks.py
│   └── sync_runner.py
├── api/
│   ├── indexer.py
│   └── chat.py
└── frapperag/page/
    ├── rag_admin/                  # RAG Index Manager
    └── rag_chat/                   # Chat UI with report_result table rendering
```

---

## 6. DocTypes Indexed (11)

Customer, Item, Sales Invoice, Purchase Invoice, Sales Order, Purchase Order, Delivery Note, Purchase Receipt, Item Price, Stock Entry, Supplier.

Add more by extending `rag/text_converter.py` with a converter function, registering in `SUPPORTED_DOCTYPES`, then whitelisting in AI Assistant Settings.

---

## 7. Phase History & Key Decisions

### Phase 5 — Report Execution Mode
Gemini can call whitelisted Report Builder reports as tools. 3-layer guard: whitelist → `report_type == "Report Builder"` live check → `frappe.has_permission`. Results normalised, capped at 50 rows, rendered as HTML tables. Script Reports deliberately blocked.

### Phase 6 — Production Hardening
Health dashboard, retry logic, structured logging, config validation, stalled-job sweeper, sync event log pruning.

### Phase 7 — Chat Quality Testing
30-question formal matrix (10 categories × 3). Initial run: NOT READY — 0% on English Lookups, Aggregations, Cross-DocType, Stock. All "honesty" categories (Out-of-Scope, Vague, Capability, Permissions) at 100% — the AI never hallucinated or leaked restricted data. Root cause: no record-level lookup tool existed.

**Critical bugs caught:**
- **7-B-001:** JSON date serialization fail in `chat_runner.py` — fixed with `default=str`.
- **7-B-002:** `DocIndexerTool.execute()` ignored `name` arg and enqueued full-DocType jobs instead of indexing single records. Bulk script silently created 5,546 background jobs instead of indexing 5,546 records. Fixed by switching to direct `upsert_record` calls.
- **7-D-002:** Vector retrieval cannot find records by exact alphanumeric ID (e.g. `SINV-IR-00657`). Known limitation of dense embeddings. Masked by `record_lookup` template; long-term fix is hybrid retrieval.

### Phase 8 — v1.0 (ExecuteQuery)
Added `query_executor.py` with four parameterized SQL templates: `record_lookup`, `top_selling_items`, `best_selling_pairs`, `low_stock_recent_sales`. Answered both stakeholder example questions ("two most-selling items together", "top 10 missing items based on 6 months of sales").

Chat quality debugged: system prompt + `EMPTY_CONTEXT_NOTE` were telling the LLM to decline on empty context instead of calling tools. Rewrote both. Added Arabic decline keywords to AR-02 grader. Result: **30/30 (100%)**. Shipped as v1.0.

### Phase 8.5 — v1.1 (Parametric Aggregation)
One generic `aggregate_doctype` template replaces what would have been ~15 hand-written templates. Parameters: `doctype`, `group_by`, `aggregate_field`, `aggregate_fn`, `filters`, `order_by`, `limit`.

**Safety model:**
- New `RAG Aggregate Field` child DocType with fail-closed defaults (`allow_group_by=0`, `allow_aggregate=0`)
- `date_field` column on `RAG Allowed DocType` — no guessing; reject date filters if unset
- Numeric-fieldtype validation in `ai_assistant_settings.py` blocks `SUM(status)` at config time
- `_execute_aggregate_doctype` uses allowlist-validated identifiers + parameterized values + per-DocType `has_permission`

Test matrix bumped 30→35. AG-03 and ST-03 converted from decline to tool_call. **Result: 34/35 (97%)**. One skip: EM-03 timeout on deliberately-fake item ID — logged to backlog. Shipped as v1.1.

---

## 8. Timing Benchmark (current)

| Step | Time |
|---|---|
| settings_read | ~0.4s |
| search_candidates | ~1.0s |
| generate_response | ~1.4s (local) / 2–5s typical / up to 79s observed on staging |
| DB writes | ~2.3s |
| **Total** | **~5s** baseline |

The 2.3s DB write is a known MariaDB row lock — accepted as current floor. Gemini latency is the dominant variable.

---

## 9. Future Roadmap

### Phase 7a — v1.2 Pluggable Embedding Provider (shipped 2026-04-26)
Decoupled the hardcoded `multilingual-e5-small` model and introduced a pluggable provider system. Default is now `gemini-embedding-001` (Google cloud, 768-dim, `v5_gemini_*` tables) with opt-in `e5-small` for full local embedding (384-dim, `v6_e5small_*`). Provider is set via `EMBEDDING_PROVIDER` env var and selectable in AI Assistant Settings without code changes. Migrated from deprecated `text-embedding-004` (EOL Jan 2026) to `gemini-embedding-001`. Test matrix expanded to 50 questions: **49/50 (98%)**. One failure (CH-05: record_lookup citation missing for a specific PO) logged to backlog.

### Phase 8.75 — v1.3 Text-to-SQL with Guardrails
Let Gemini generate SELECT statements directly against a whitelisted schema. Safety layer: `sqlparse` validation, single-SELECT only, table allowlist, forced LIMIT, read-only DB user, permission post-filter, audit log to `Generated Query Log` DocType. Build only after v1.2 data reveals which question shapes still fail. This is the ceiling for analytical questions.

### Phase 9 — Files & Images
OCR for scanned invoices, vision model for product photos, attachment content indexed alongside parent documents. Likely a second sidecar endpoint or `/embed` extension.

### Phase 9.5 — Google Search Grounding
Migrate `google-generativeai` → `google-genai` SDK. Add Google Search as a tool for out-of-scope questions. Clearly label external results so users see what's grounded in their data vs the web.

### Phase 10 — Agentic Write Actions
The leap from "useful" to "transformative." "Create a purchase order for 500 units of item X from supplier Y" actually creates the PO with confirmation. "Reorder items below minimum stock from their usual suppliers" becomes a real workflow. Requires: write-action tool registry, mandatory confirmation UI, full audit log, rollback capability, tight permission checks. Business-rule judgment lives here (reorder points, lead times, seasonality).

### Phase 11 — WhatsApp & Telegram Voice
Warehouse manager asks "what's the stock balance for item 6956?" by voice while walking the floor, gets audio reply. Needs: WhatsApp Business API / Telegram Bot API, Whisper or Gemini audio STT, TTS for replies, phone→Frappe user session linking, Arabic voice handling.

### Cross-cutting work (lands somewhere along the way)
- **Hybrid retrieval** (dense + BM25) to fix exact-ID matching. Probably in 8.5–8.75.
- **Multi-turn tool calls** so Gemini can reason over one tool result and call another. Needed for "show me the top customer, then their last 5 invoices."
- **Script Report support** with sandboxing, if parametric/text-to-SQL don't cover enough ground.
- **Test runner stays in sync** with every new tool. v1_runner pattern is part of definition-of-done.
- **`PHASE_9_BACKLOG.md` hygiene** — log mid-phase ideas, don't build them.

---

## 10. Positioning & Competitive Reality

### What distinguishes FrappeRAG
1. **Frappe-native, not a SaaS wrapper.** Vectors in `bench/rag/`, local embedding model, only Gemini is external. For Middle East businesses with sensitive financial data, "your data never leaves your server" is a requirement, not a feature.
2. **Real permission-aware retrieval.** Enforced at indexing, retrieval, and prompt assembly — not just the UI layer. A sales rep cannot get answers about records they can't see in ERPNext.
3. **Bilingual Arabic+English from day one.** `multilingual-e5-small` handles both in the same vector space. Ask in Arabic, get results from English-named items. Table stakes for Jordan/Gulf market; nobody delivers it natively.
4. **Report and SQL tool calling bridges the RAG ceiling.** Vector search can't answer "count all X where Y." Tool calls can. Most RAG-over-ERP projects miss this entirely.
5. **Zero infrastructure beyond the bench.** One `bench get-app` + one API key. No DevOps required.

### Competitors studied
- **ChangAI** — only real RAG pipeline but violates data isolation.
- **Frappe Assistant Core** — best permission model, `BaseTool` ABC pattern, no vector search.
- **Next AI** — nice UI patterns, `frappe.publish_realtime` progress, no retrieval pipeline.
- **Sema4.ai / Relevance AI** — powerful but require data export and cloud infra.

No direct competitor combines all five differentiators in the Frappe ecosystem.

### Honest limitations
- Not a general-purpose assistant. Without Google Search (9.5), "what's the weather?" returns nothing useful.
- Not real-time for large datasets. 2–5s baseline, up to 79s on bad networks. Value is for questions that would take 5–10 minutes of manual navigation — not for "open customer X."
- Not a replacement for reports/dashboards. 50-row cap and top-K retrieval mean it's a discovery tool, not a reporting engine.

### The arc, in one sentence
v1.0 makes ERPNext data answerable in natural language; v1.1–1.2 make it answerable for *any* analytical question; Phase 9 makes it answerable across attached files and the open web; Phase 10 makes it actionable; Phase 11 makes it accessible from a phone in Arabic by voice. At Phase 11, FrappeRAG isn't a chatbot — it's the AI operations layer for ERPNext.

---

## 11. Workflow & Discipline
- **Daily commits**, daily smoke test of baseline questions, `PHASE_N_JOURNAL.md` for decision tracking.
- **No feature creep** — mid-phase ideas go to `PHASE_9_BACKLOG.md`, not the code.
- **Manual acceptance only** per Constitution principle 7, but every phase ships with its own test matrix kept in sync with new tools.
