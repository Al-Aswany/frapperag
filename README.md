# FrappeRAG

An AI assistant for Frappe / ERPNext where live ERP querying is the primary structured-data path. FrappeRAG keeps LanceDB-backed vector indexing as a legacy/manual compatibility layer for `assistant_mode = v1` and future document-oriented sources.

## Features

- **Embedding (default: cloud)** — Google `text-embedding-004` (768-dim) by default. Opt into `multilingual-e5-small` (local, 384-dim, ~470 MB) via AI Assistant Settings → Embedding Provider for full data sovereignty on the embedding path.
- **Primary structured path** — `assistant_mode = hybrid` uses live ERP reads (`get_list` and approved analytics executors) for structured questions.
- **Vector store** — LanceDB remains in a bench-level `rag/` directory; table prefix `v5_gemini_` or `v6_e5small_` depending on the active provider.
- **Legacy incremental sync** — opt-in Frappe `doc_events` hooks can keep legacy vector tables in sync for the supported ERP DocTypes when explicitly enabled.
- **Legacy/manual bulk indexing** — Legacy Vector Index Manager lets RAG Admins trigger manual compatibility re-indexing for the supported legacy ERP DocTypes, with realtime progress updates via `frappe.realtime`.
- **Chat interface** — Vanilla JS chat page (`/rag-chat`) backed by a configurable Gemini chat model (default: `gemini-2.5-flash`) with multi-turn conversation history and per-source citations.
- **Report execution** — Gemini can call whitelisted Report Builder reports as tools; results are rendered as formatted tables inside the chat thread with a 50-row cap.
- **Query execution** — Predefined SQL templates for common analytics (top selling items, best-selling pairs, low stock, customer/supplier activity, inventory status, pending orders). Gemini selects the right template and passes validated parameters; results are returned as structured citations.
- **Permission-aware retrieval** — every candidate record is filtered through `frappe.has_permission` before it can be included in a prompt or a chat response.
- **Health monitoring** — Periodic sidecar health checks with response-time tracking via the `RAG System Health` Single DocType.
- **Roles** — two fixtures (`RAG Admin`, `RAG User`) control access. System Managers always have full access.

## Data Sovereignty

By default, indexed document text is sent to Google for embedding (text-embedding-004 cloud endpoint). To run embedding fully on your own server, set **Embedding Provider** to `e5-small` in AI Assistant Settings and click **Install Local Model**. The sidecar will download multilingual-e5-small (~470 MB, requires ≥2 GB RAM) and run all embedding locally — indexed text never leaves your server.

**Note:** Chat generation always uses Google Gemini regardless of this setting. Switching the embedding provider does NOT make the system fully on-premise.

## Architecture

```
Frappe worker (queue=short/long)
        │
        │  httpx (localhost only)
        ▼
RAG Sidecar  ── FastAPI + uvicorn ──  LanceDB (bench-level rag/)
  /embed          → Gemini cloud OR local e5-small (selectable via EMBEDDING_PROVIDER)
  /upsert         → writes to v5_gemini_* or v6_e5small_* tables
  /search         → queries active-prefix tables
  /chat           google-genai runtime (default chat model: Gemini 2.5 Flash)
  /record/:id DELETE
  /table/:name DELETE
  /tables/populated GET
  /install_local_model POST
  /install_local_model/status/:id GET
```

Workers **never** import `lancedb` or `sentence_transformers` directly. All vector and embedding operations are delegated to the sidecar via `frapperag.rag.sidecar_client`. The sidecar and LanceDB remain in place for legacy/manual vector compatibility and future document-oriented sources.

The sidecar client includes built-in retry logic (max 3 attempts with exponential backoff) for transient errors (connect failures, timeouts, HTTP 429/502/503).

## Project Structure

```
apps/frapperag/frapperag/
├── hooks.py                        # after_install, after_migrate, scheduler_events, doc_events, fixtures
├── requirements.txt
├── setup/install.py                # creates rag/ dir, adds Procfile sidecar entry, seeds allowed doctypes
├── sidecar/
│   ├── main.py                     # FastAPI app — /embed /upsert /search /chat /record /table
│   └── store.py                    # LanceDB open/upsert/search/delete helpers
├── assistant/
│   ├── planner.py                  # Phase 3 manual planner scaffolding for safe get_list plans
│   ├── plan_validator.py           # Policy-backed validation for DocTypes, fields, filters, sort, and limits
│   ├── tool_call_log.py            # Execution log helper for planner/validator/executor runs
│   └── executors/
│       └── get_list_executor.py    # Read-only get_list executor for validated plans only
├── frapperag/doctype/
│   ├── ai_assistant_settings/      # Single DocType — API key, whitelist, roles, sidecar port
│   ├── ai_tool_call_log/           # Audit log for Phase 3 planner / validator / executor activity
│   ├── rag_allowed_doctype/        # Child table — allowed ERP DocTypes / live-query policy rows
│   ├── rag_allowed_role/           # Child table — roles allowed to chat
│   ├── rag_allowed_report/         # Child table — whitelisted Report Builder reports
│   ├── rag_aggregate_field/        # Child table — custom aggregation fields for query executor
│   ├── ai_indexing_job/            # Bulk index job tracking (status, progress, counters)
│   ├── chat_session/               # Chat session (Open / Archived)
│   ├── chat_message/               # Individual message (user / assistant, Pending / Completed / Failed)
│   ├── sync_event_log/             # Per-record incremental sync audit log
│   └── rag_system_health/          # Single DocType — sidecar health status + response time
├── rag/
│   ├── base_indexer.py             # BaseIndexer ABC (validate → check_permission → execute)
│   ├── sidecar_client.py           # httpx wrappers for every sidecar endpoint (with retry logic)
│   ├── text_converter.py           # DocType → deterministic human-readable text (11 ERPNext DocTypes)
│   ├── indexer.py                  # Legacy/manual DocIndexerTool + run_indexing_job() + mark_stalled_jobs()
│   ├── retriever.py                # Legacy v1 vector retrieval + permission filtering
│   ├── prompt_builder.py           # build_messages() + build_tool_definitions() → Gemini tool list
│   ├── chat_engine.py              # generate_response() via sidecar /chat (supports function calling)
│   ├── chat_runner.py              # run_chat_job() + tool_call dispatch (reports + queries)
│   ├── report_executor.py          # execute_report() — 3-layer permission check + Report Builder execution
│   ├── query_executor.py           # SQL templates for analytics (top sellers, stock, pairs, etc.)
│   ├── health.py                   # Periodic sidecar health check → RAG System Health DocType
│   ├── sync_hooks.py               # opt-in legacy vector doc_events handlers
│   └── sync_runner.py              # legacy sync compatibility workers + scheduler sweepers
├── api/
│   ├── indexer.py                  # trigger_indexing, trigger_full_index, get_job_status, list_jobs, get_sync_health, sidecar_health, retry_sync
│   └── chat.py                     # create_session, send_message, list_sessions, get_messages, get_message_status, archive_session
└── frapperag/page/
    ├── rag_admin/                  # Legacy Vector Index Manager — manual indexing + legacy sync health
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
| google-genai | >= 1.0.0 |

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

`after_migrate` idempotently seeds the default 11 allowed ERP DocTypes into AI Assistant Settings and backfills `Enable Legacy Transactional Vector Sync = 0` unless it has already been set.

## Configuration

1. Open **AI Assistant Settings** in Frappe Desk (System Manager or RAG Admin).
2. Enter your **Gemini API Key**.
3. Add the DocTypes you want exposed to the assistant in **Allowed ERP DocTypes**. Defaults are seeded automatically on install/migrate for the current 11 supported legacy ERP vector DocTypes and are also used by hybrid live querying and analytics policy.
4. Add the roles that may use the chat to **Allowed Roles** (default: `RAG User`).
5. Optionally whitelist **Report Builder** reports in **Allowed Reports** (see Report Execution below).
6. Optionally configure **Aggregate Fields** for custom query analytics (see Query Execution below).
7. Optionally change **Chat Model**. Leave the default `gemini-2.5-flash` for stable v1 behavior, or set `gemini-3-flash-preview` / `gemini-3.1-flash-lite-preview` for manual runtime testing.
8. Optionally enable **Google Search Grounding**. It is disabled by default and is reserved for future routed intents such as `out_of_scope`, `erpnext_help`, or explicit current-info/web questions.
9. Leave **Enable Legacy Transactional Vector Sync** off unless you explicitly want legacy per-record vector sync on normal ERP saves for v1 compatibility maintenance.
10. Optionally adjust **Sidecar Port** (default: `8100`).

## Phase 5 Structured Data Default

Phase 5 changes the structured-data default only. Transactional ERP records are no longer treated as the default vector/RAG source.

- `assistant_mode = hybrid` uses live ERP reads (`get_list` and approved analytics executors) as the main structured-data path.
- `assistant_mode = v1` still uses the legacy v1 path and can continue to retrieve already-indexed vectors.
- Transactional vector indexing remains available for manual reindexing or legacy/admin needs.
- Document, attachment, and future long-text indexing remain separate from this toggle and are not removed here.

## Phase 6 Legacy Vector Boundary Cleanup

Phase 6 keeps LanceDB, the sidecar, the `v1` path, and manual indexing intact, but narrows the old record-level vector architecture so it is clearly legacy/manual rather than the primary structured-data path.

- Live ERP querying remains primary for structured data.
- Legacy vector sync is scoped to the fixed supported ERP DocTypes and stays disabled by default.
- Legacy/manual indexing only targets the supported legacy ERP DocTypes that are still present in **Allowed ERP DocTypes**.
- Existing vectors are not purged automatically when policy rows are removed; retrieval and admin surfaces stop using disallowed targets instead.
- Phase 6 does not add file/image support, LLM Wiki, Text-to-SQL, write actions, or WhatsApp.

## Phase 2.5 Runtime Upgrade

Phase 2.5 upgrades only the Gemini runtime layer. While **Assistant Mode** stays `v1`, the existing chat contract, chat UI behavior, retriever flow, report/query tool execution, and answer path remain unchanged.

- Sidecar `/chat` now uses the `google-genai` SDK instead of `google-generativeai`.
- The chat model is configurable through **AI Assistant Settings → Chat Model** and defaults to `gemini-2.5-flash`.
- Preview models such as `gemini-3-flash-preview` and `gemini-3.1-flash-lite-preview` are supported by setting the model name directly.
- Google Search grounding support is installed behind **Enable Google Search Grounding**, but stays disabled by default and is not used by the current v1 chat path.
- Google Search is restricted to future routed intents only and is blocked when ERP context is present, so ERP data is never passed to Google Search.

### Manual verification commands

```bash
bench --site golive.site1 migrate
bench --site golive.site1 execute frapperag.rag.chat_engine.debug_chat_runtime_settings
bench --site golive.site1 execute frapperag.rag.chat_engine.debug_chat_runtime_settings --kwargs "{'intent': 'erpnext_help'}"
bench --site golive.site1 execute frapperag.rag.chat_engine.debug_chat_runtime_settings --kwargs "{'intent': 'erpnext_help', 'has_erp_context': 1}"
bench --site golive.site1 execute frapperag.rag.sidecar_client.health_check
```

Manual checks:

1. In **AI Assistant Settings**, confirm `Assistant Mode = v1`.
2. Leave `Chat Model = gemini-2.5-flash`, send a normal message through `/rag-chat`, and confirm the v1 response path still works.
3. Change `Chat Model` to `gemini-3-flash-preview`, save, rerun `debug_chat_runtime_settings`, and repeat the same v1 chat smoke test.
4. Change `Chat Model` to `gemini-3.1-flash-lite-preview`, save, rerun `debug_chat_runtime_settings`, and repeat the same v1 chat smoke test.
5. Enable **Google Search Grounding**, save, and rerun the debug command. It should report `google_search_would_be_used = true` only for an allowed intent without ERP context, and `false` when `has_erp_context = 1`.

## Phase 1B Foundation

Phase 1B adds non-behavioral v2 foundation only. While **Assistant Mode** stays `v1`, the current chat pipeline, routing, planner/executor behavior, and sidecar `/chat` flow remain unchanged.

- `RAG Allowed DocType` now carries future query-policy fields such as `enabled`, `default_date_field`, `default_title_field`, `allow_get_list`, `allow_query_builder`, `allow_child_tables`, `default_sort`, `default_limit`, and `large_table_requires_date_filter`.
- `enabled` is intentionally the new query-policy field for safe schema exposure. While `assistant_mode = v1`, legacy v1 indexing, sync, aggregate-query, and chat allowlisting still key off `allowed_doctypes` row presence plus `doctype_name`, not `enabled`.
- Safe schema-slice helpers live in `frapperag.assistant.schema_policy` and expose only enabled DocTypes plus safe-by-default fields. Hidden, password, attachment, table, code/html, long-text, and sensitive-name fields are excluded unless explicitly requested for inspection.

### Manual verification commands

```bash
bench --site golive.site1 migrate
bench --site golive.site1 execute frapperag.assistant.schema_refresh.refresh_schema_catalog --kwargs "{'reason': 'phase_1b_manual', 'requested_by': 'Administrator', 'throw': 1}"
bench --site golive.site1 execute frapperag.assistant.schema_policy.debug_query_policy_snapshot
bench --site golive.site1 execute frapperag.assistant.schema_policy.debug_safe_schema_slice --kwargs "{'doctype_names': 'Sales Invoice,Customer'}"
bench --site golive.site1 execute frapperag.assistant.schema_policy.debug_safe_schema_slice --kwargs "{'doctype_names': 'Sales Invoice', 'include_unsafe_fields': 1}"
```

## Phase 2 Shadow Routing

Phase 2 adds shadow-only intent routing. While **Assistant Mode** stays `v1`, chat answers, chat UI, retriever behavior, tool execution, and the existing Gemini `/chat` path remain unchanged.

- Added `frapperag.assistant.intent_router` with seven route classes: `structured_query`, `erpnext_help`, `document_rag`, `report_query`, `mixed_query`, `out_of_scope`, and `unclear`.
- Routing is deterministic first. Optional Gemini fallback is available only through the router function and only uses bounded safe schema slices from `frapperag.assistant.schema_policy` plus allowed report snippets. The full schema catalog is never passed to Gemini. The chat worker keeps `use_llm_fallback=False` for the shadow hook unless you opt in manually.
- `frapperag.rag.chat_runner.run_chat_job()` now runs the router in shadow mode and logs the decision only. Router failures are swallowed so the v1 chat path continues unchanged.
- Shadow logs include: question, selected intent, confidence, reason, candidate DocTypes, candidate reports, router source, and shadow-only status.

### Manual verification commands

```bash
bench --site golive.site1 execute frapperag.assistant.intent_router.debug_route_question --kwargs "{'question': 'How many overdue Sales Invoices do I have?'}"
bench --site golive.site1 execute frapperag.assistant.intent_router.debug_route_question --kwargs "{'question': 'How do I submit a Sales Invoice?'}"
bench --site golive.site1 execute frapperag.assistant.intent_router.debug_route_question --kwargs "{'question': 'Summarize the leave policy PDF'}"
bench --site golive.site1 execute frapperag.assistant.intent_router.debug_route_question --kwargs "{'question': 'Which Accounts Receivable report should I run?', 'use_llm_fallback': 1}"
tail -n 20 logs/frapperag.log | rg ROUTER_SHADOW
```

Send a normal chat message through the existing v1 UI or API to exercise the shadow hook in `run_chat_job()`. The response path stays unchanged; only an additional `ROUTER_SHADOW` log line is emitted.

## Phase 3 Foundation

Phase 3 is implemented as a manual-only foundation. While **Assistant Mode** stays `v1`, normal chat answers, final Gemini responses, sidecar `/chat`, Google Search usage, and the existing v1 tool path remain unchanged.

- Added `frapperag.assistant.planner` for structured `get_list` plan scaffolding.
- Added `frapperag.assistant.plan_validator` to enforce enabled/queryable DocTypes, safe fields, allowed filters, allowed sort fields, row limits, and large-table date-filter requirements.
- Added `frapperag.assistant.executors.get_list_executor` for read-only `frappe.get_list` execution using validated plans only.
- Added `AI Tool Call Log` plus `frapperag.assistant.tool_call_log` for planner/validator/executor audit records.
- Explicitly not added to normal chat in this phase: v2 routing, answer composition, write actions, Google Search grounding, raw SQL execution, Query Builder joins, or any change to `assistant_mode = v1`.

### Manual verification commands

```bash
bench --site golive.site1 migrate
bench --site golive.site1 execute frapperag.assistant.plan_validator.debug_describe_queryable_doctype --kwargs "{'doctype': 'Sales Invoice'}"
bench --site golive.site1 execute frapperag.assistant.planner.debug_create_get_list_plan --kwargs "{'question': 'List recent sales invoices', 'doctype': 'Sales Invoice', 'fields_json': '[\"name\", \"customer\", \"posting_date\", \"grand_total\", \"status\"]', 'filters_json': '[{\"field\": \"posting_date\", \"operator\": \">=\", \"value\": \"2026-01-01\"}]', 'order_by_json': '{\"field\": \"posting_date\", \"direction\": \"desc\"}', 'limit': 10}"
bench --site golive.site1 execute frapperag.assistant.plan_validator.debug_build_and_validate_get_list_plan --kwargs "{'question': 'List recent sales invoices', 'doctype': 'Sales Invoice', 'fields_json': '[\"name\", \"customer\", \"posting_date\", \"grand_total\", \"status\"]', 'filters_json': '[{\"field\": \"posting_date\", \"operator\": \">=\", \"value\": \"2026-01-01\"}]', 'order_by_json': '{\"field\": \"posting_date\", \"direction\": \"desc\"}', 'limit': 10}"
bench --site golive.site1 execute frapperag.assistant.executors.get_list_executor.debug_build_validate_and_execute_get_list_plan --kwargs "{'question': 'List recent sales invoices', 'doctype': 'Sales Invoice', 'fields_json': '[\"name\", \"customer\", \"posting_date\", \"grand_total\", \"status\"]', 'filters_json': '[{\"field\": \"posting_date\", \"operator\": \">=\", \"value\": \"2026-01-01\"}]', 'order_by_json': '{\"field\": \"posting_date\", \"direction\": \"desc\"}', 'limit': 10}"
bench --site golive.site1 execute frapperag.assistant.plan_validator.debug_build_and_validate_get_list_plan --kwargs "{'question': 'Try a disabled DocType', 'doctype': 'User', 'fields_json': '[\"name\"]', 'limit': 5}"
bench --site golive.site1 execute frapperag.assistant.tool_call_log.debug_get_recent_tool_logs --kwargs "{'limit': 10}"
```

Manual checks:

1. Confirm `AI Assistant Settings.assistant_mode` is still `v1` before and after the Phase 3 commands.
2. Use `debug_describe_queryable_doctype` or `debug_safe_schema_slice(... include_unsafe_fields=1)` to identify an unsafe field, then run `debug_build_and_validate_get_list_plan` with that field and confirm validation is rejected.
3. Set `large_table_requires_date_filter = 1` for a test DocType that has `default_date_field`, omit the date filter in the validation command, and confirm the plan is rejected.
4. Send a normal message through the existing `/rag-chat` UI after Phase 3 verification and confirm the chat still follows the unchanged v1 path.

## Phase 4 Controlled Hybrid Chat

Phase 4 connects the Phase 2 router and the Phase 3 `get_list` planner/validator/executor to chat only when **Assistant Mode** is set to `hybrid`. The default remains `v1`, and the existing v1 chat path remains unchanged in `v1` mode.

- Added `frapperag.assistant.chat_orchestrator` for the hybrid-only route → plan → validate → execute → compose flow.
- Added `frapperag.assistant.answer_composer` for a small grounded answer over validated `get_list` results.
- `frapperag.rag.chat_runner.run_chat_job()` still emits `ROUTER_SHADOW` in all modes, but it attempts the hybrid path only when `assistant_mode = hybrid`.
- Hybrid chat only handles safe `structured_query` routes with enough router confidence and a single supported `get_list` step.
- Hybrid chat falls back to the unchanged v1 path for non-structured questions, low-confidence routes, unsupported plans, validation rejection, executor/composer failure, and unexpected hybrid errors.
- Successful and rejected hybrid tool calls are logged in `AI Tool Call Log` with `assistant_mode = hybrid`.
- Still not introduced in this phase: Google Search grounding, raw SQL, Query Builder joins, write actions, unsafe field exposure, full schema-catalog prompts, or Phase 5 vector-index cleanup.

### Verification notes

If you change `RAG Allowed DocType` child-row policy values directly during verification, clear the cached singleton before testing live hybrid behavior:

```bash
bench --site golive.site1 execute frappe.clear_document_cache --args '["AI Assistant Settings", "AI Assistant Settings"]'
```

Suggested checks:

1. Leave `assistant_mode = v1`, send a normal `/rag-chat` message, and confirm the existing v1 response path still works with no Phase 3 tool usage.
2. Switch to `assistant_mode = hybrid`, send one simple structured query such as `List the 5 most recent Sales Invoices since 2026-01-01`, and confirm the answer is produced from live `get_list`.
3. Review `AI Tool Call Log` and confirm successful hybrid entries such as `planner.plan_structured_query`, `validator.validate_plan`, `executor.get_list.execute_validated_plan`, and `composer.compose_structured_answer` show `assistant_mode = hybrid`.
4. Send an unsupported or unclear query in `hybrid` mode and confirm chat safely falls back to the normal v1 answer path.
5. Switch back to `assistant_mode = v1` and confirm the unchanged v1 behavior returns immediately without hybrid tool activity.

## Phase 4B Hybrid Hardening Approved

Phase 4B is approved. It adds a manual structured-query hardening matrix for the approved hybrid path without changing the default runtime. `assistant_mode` stays `v1` by default, hybrid remains limited to validated read-only `get_list`, full schema catalogs are still never passed to Gemini, and no Google Search, raw SQL, Query Builder joins, write actions, vector-index cleanup, or v1 file removal are introduced here.

- Added [phase4b_hybrid_matrix.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_matrix.json) with 11 manual cases covering:
- customer list
- sales invoice list with date filter
- sales invoice latest records
- item list
- supplier list
- unsafe field rejection
- disabled DocType rejection
- excessive limit rejection
- child-table query rejection/fallback
- unclear query fallback
- non-structured query fallback
- Added [phase4b_hybrid_runner.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_runner.py) so the full matrix or a single case can be run from `bench`.
- Added `frapperag.assistant.chat_orchestrator.debug_probe_hybrid_path(...)` as a debug-only probe for hybrid validation/execution and fail-closed fallback checks without changing the live `v1` chat path.

### Manual commands

```bash
bench --site golive.site1 execute frapperag.tests.phase4b_hybrid_runner.run_matrix
bench --site golive.site1 execute frapperag.tests.phase4b_hybrid_runner.run_case --kwargs "{'case_id': 'unsafe_field_rejection'}"
bench --site golive.site1 execute frapperag.assistant.tool_call_log.debug_get_recent_tool_logs --kwargs "{'limit': 20}"
```

### Recorded result

Recorded run on `golive.site1` at `2026-05-07T22:11:17Z`:

- `11 / 11` matrix cases passed.
- `assistant_mode` was `v1` before the run and `v1` after the run.
- Safe `get_list` cases passed for `Customer`, `Sales Invoice`, `Item`, and `Supplier`.
- Unsafe field, disabled DocType, excessive limit, and child-table requests were rejected during validation and marked as hybrid fallback outcomes.
- Unclear and non-structured questions resolved to fail-closed hybrid fallback outcomes before any live hybrid execution.
- `AI Tool Call Log` rows emitted by the probe are tagged with `assistant_mode = hybrid` for diagnosis even though the site default stayed `v1`.
- Results file written to [phase4b_hybrid_results_20260507T221117Z.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_results_20260507T221117Z.json).

## Phase 4C Self-Serve Analytics Foundation

Phase 4C foundation is implemented as unused/import-safe scaffolding only. It adds a structured analytics DSL and fail-closed validation without changing normal chat, hybrid runtime behavior, vector sync, or adding any analytics executor.

- Added `frapperag.assistant.analytics.analytics_plan_schema` with Option A only: structured JSON analytics plan shapes for `single_doctype_aggregate`, `parent_child_aggregate`, `time_bucket_aggregate`, `period_comparison`, `co_occurrence`, `top_n`, `bottom_n`, `ratio`, and `trend`.
- Added `frapperag.assistant.analytics.relationship_graph` with approved ERPNext relationship edges such as `Sales Invoice -> Sales Invoice Item`, `Customer -> Territory`, `Customer -> Customer Group`, `Item -> Item Group`, `Payment Entry -> Party`, and `Stock Ledger Entry -> Item` / `Warehouse`.
- Added `frapperag.assistant.analytics.metric_registry` with curated safe metrics such as `sales_amount`, `sales_qty`, `invoice_count`, `avg_invoice_value`, `outstanding_amount`, `purchase_amount`, `purchase_qty`, `stock_qty`, and `movement_qty`.
- Added `frapperag.assistant.analytics.analytics_validator` to fail closed on unsupported analysis types, DocTypes, fields, metrics, relationships, unsafe limits, missing required date filters for large tables, SQL strings, write-like payloads, and unsupported child-table traversal.
- The validator reuses `RAG Allowed DocType` query-policy fields where available, especially `enabled`, `default_date_field`, `allow_child_tables`, `default_limit`, and `large_table_requires_date_filter`.
- Explicit non-goals in this foundation pass:
- no raw LLM SQL
- no guarded text-to-SQL
- no analytics query execution
- no write actions
- no change to default `assistant_mode = v1`
- no change to current v1 chat answers or approved hybrid `get_list` behavior

### Manual verification commands

```bash
python -m py_compile \
  apps/frapperag/frapperag/assistant/analytics/__init__.py \
  apps/frapperag/frapperag/assistant/analytics/analytics_plan_schema.py \
  apps/frapperag/frapperag/assistant/analytics/relationship_graph.py \
  apps/frapperag/frapperag/assistant/analytics/metric_registry.py \
  apps/frapperag/frapperag/assistant/analytics/analytics_validator.py
bench --site golive.site1 execute frapperag.assistant.analytics.analytics_plan_schema.debug_describe_supported_plan_shapes
bench --site golive.site1 execute frapperag.assistant.analytics.metric_registry.debug_describe_metric_registry --kwargs "{'source_doctype': 'Sales Invoice'}"
bench --site golive.site1 execute frapperag.assistant.analytics.relationship_graph.debug_describe_relationship_graph --kwargs "{'source_doctype': 'Sales Invoice'}"
```

## Running

```bash
bench start
# The Procfile launches:
#   web          — Frappe web server
#   worker:short — chat + incremental sync jobs
#   worker:long  — bulk indexing jobs
#   rag_sidecar  — FastAPI sidecar on localhost:8100
```

The sidecar loads the configured embedding provider on startup. With the default `gemini` provider there is no local model download. With `e5-small`, the first run downloads multilingual-e5-small (~470 MB); subsequent starts reuse the HuggingFace cache.

## Indexing

### Bulk indexing

Navigate to **Legacy Vector Index Manager** (`/rag-admin`) in Frappe Desk, select a DocType, and click **Start Legacy Indexing**. Progress is streamed via `frappe.realtime`. Only one active job per DocType is allowed at a time. This path remains available for manual/legacy reindexing even when transactional auto-sync is disabled.

### Incremental sync

Legacy transactional vector sync is disabled by default. When **Enable Legacy Transactional Vector Sync** is off, normal saves, renames, and deletes on the supported ERP transactional DocTypes do not enqueue per-record vector sync jobs. Existing vectors are left in place; no automatic purge runs in Phase 6.

If you explicitly enable **Enable Legacy Transactional Vector Sync**, the legacy per-record `short`-queue upsert/delete flow resumes for the supported transactional DocTypes. The `Sync Event Log` DocType continues to record sync outcomes for enabled/manual flows, and RAG Admins can inspect and retry failures from the **Legacy Vector Sync Health** section in **AI Assistant Settings**.

### Scheduler tasks

| Schedule | Task |
|---|---|
| Every run | Sidecar health check → updates `RAG System Health` DocType |
| Every 5 minutes | Mark stalled indexing jobs (>2 h), chat messages (>5 min), and sync log entries (>1 h) as Failed |
| Daily | Prune `Sync Event Log` entries older than 30 days |

## Chat

Navigate to **RAG Chat** (`/rag-chat`) in Frappe Desk. Each conversation is a `Chat Session`. Messages are sent asynchronously — the UI locks input while a response is pending and unlocks on the `rag_chat_response` realtime event.

The pipeline per message:
1. Embed the question (sidecar `/search`).
2. Retrieve top-5 candidates across all active-prefix tables (`v5_gemini_*` or `v6_e5small_*`).
3. Filter by `frappe.has_permission` for the calling user.
4. Load the last 10 conversation turns.
5. Build the Gemini message list (system context + history + retrieved snippets + question).
6. Call sidecar `/chat` → configured Gemini chat model (default: `gemini-2.5-flash`).
7. Save user message (Completed) and assistant reply to `Chat Message`.
8. Publish `rag_chat_response` realtime event.

## API Reference

All endpoints require authentication via Frappe session or API key.

### Indexing (`frapperag.api.indexer`)

| Method | Description |
|---|---|
| `trigger_indexing(doctype)` | Enqueue a legacy/manual bulk indexing job; returns `{job_id, status}` |
| `trigger_full_index()` | Enqueue jobs for all legacy/manual indexing targets (skips those with an active job) |
| `get_job_status(job_id)` | Return current progress and counters for a job |
| `list_jobs(limit, page)` | Paginated list of AI Indexing Jobs |
| `get_manual_indexing_targets_snapshot()` | Return backend-filtered legacy/manual indexing targets for admin surfaces |
| `get_sync_health()` | Per-DocType success/failure counts (last 24 h) + failed entries list |
| `sidecar_health()` | Check sidecar liveness; returns status and response time |
| `retry_sync(sync_log_id)` | Re-queue a failed sync log entry |

### Chat (`frapperag.api.chat`)

| Method | Description |
|---|---|
| `create_session()` | Create a new Open Chat Session; returns `{session_id}` |
| `send_message(session_id, content)` | Queue a chat message; returns `{message_id, status}` |
| `list_sessions(include_archived)` | List caller's sessions (newest first) |
| `get_messages(session_id)` | Return all messages in a session (oldest first) |
| `get_message_status(message_id)` | Poll message status (Pending / Completed / Failed) |
| `archive_session(session_id)` | Transition session to Archived |

## Sidecar API

The sidecar runs on `localhost` only and is not exposed to the internet.

| Endpoint | Method | Description |
|---|---|---|
| `/health` | GET | Liveness check — returns `{status, provider, dim, table_prefix}` |
| `/embed` | POST | Embed a list of texts using the active provider |
| `/upsert` | POST | Embed and upsert one record into its active-prefix table |
| `/search` | POST | Embed a query and search all active-prefix tables |
| `/chat` | POST | Call Gemini with a conversation history; request contract remains compatible, with optional Google Search grounding reserved for future routed intents |
| `/record/{table}/{record_id}` | DELETE | Remove one vector entry (idempotent) |
| `/table/{table}` | DELETE | Drop an entire LanceDB table (idempotent) |
| `/tables/populated` | GET | List populated tables under a prefix |
| `/install_local_model` | POST | Start background download of multilingual-e5-small |
| `/install_local_model/status/{id}` | GET | Poll install progress |

## Supported DocTypes

FrappeRAG currently converts the following 11 ERPNext DocTypes to text for legacy/manual indexing and `v1` compatibility:

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

## Query Execution

FrappeRAG exposes predefined SQL analytics templates as Gemini tool calls. When a user asks a question best answered by structured data (e.g. "what are our top-selling items this month?"), Gemini selects the appropriate query template and provides validated parameters.

### Available templates

| Template | Description |
|---|---|
| `top_selling_items` | Top-selling items by quantity or amount within a date range |
| `best_selling_pairs` | Frequently co-purchased item pairs |
| `low_stock_recent_sales` | Items with low stock that had recent sales activity |
| `customer_recent_sales` | Recent sales activity for a specific customer |
| `supplier_recent_purchases` | Recent purchase activity for a specific supplier |
| `inventory_status` | Current stock levels across warehouses |
| `pending_orders` | Open sales/purchase orders pending fulfillment |

Each template enforces parameter validation and per-DocType permission checks before execution. Results are returned as structured JSON citations in the chat thread.

Administrators can configure custom aggregation fields via **AI Assistant Settings → Aggregate Fields** (`RAG Aggregate Field` child table).

## Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/frapperag
pre-commit install
```

Pre-commit tools configured: **ruff**, **eslint**, **prettier**, **pyupgrade**.

## License

MIT
