# FrappeRAG v2 Redesign Plan

## 1. Executive Summary
- FrappeRAG v2 should stop treating ERPNext as a document corpus and start treating it as a live business system: structured questions use live ERPNext reads, unstructured questions use document RAG, and schema knowledge becomes the assistant’s main planning context.
- The current app already has useful foundations: chat/session persistence, role-gated settings, whitelisted report execution, allowlist-based aggregate querying, and a deployable sidecar/runtime split ([api/chat.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/api/chat.py:18), [report_executor.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/report_executor.py:13), [query_executor.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/query_executor.py:691)).
- The redesign should preserve the current chat experience with minor API drift allowed, keep Gemini `/chat` in the sidecar during the first v2 refactor, and move business planning/execution logic into `frapperag/assistant/` while continuing to call Gemini through the existing sidecar client.
- Default v2 decision: Gemini remains the planning/composition model, LanceDB remains optional and only for document sources or optional schema-semantic lookup, and record-level transactional vector indexing becomes disabled by default.

## Phase 1A Status — Completed / Approved
- Completed work:
- Created `frapperag/assistant/` package.
- Added `schema_catalog.py`.
- Added `schema_refresh.py`.
- Added schema catalog refresh API in `frapperag/api/settings.py`.
- Added `assistant_mode` to AI Assistant Settings.
- Added schema refresh status fields to AI Assistant Settings.
- Added Refresh Schema Catalog button to `ai_assistant_settings.js`.
- Updated install/migrate bootstrap to seed `assistant_mode = v1`.
- Changed schema bootstrap refresh to enqueue in background instead of running synchronously during migrate.
- Confirmed Phase 1A does not touch or bypass the existing v1 chat pipeline.
- Confirmed existing chat remains on v1 behavior while `assistant_mode = v1`.
- Verification notes:
- `py_compile` passed for touched Python files.
- `json.tool` passed for `ai_assistant_settings.json`.
- Real authenticated permission test for `refresh_schema_catalog` passed:
- System Manager: allowed.
- RAG Admin: allowed.
- RAG User only: denied with `403` / `PermissionError`.
- Guest/unauthenticated: denied with `403` before method execution.
- Manual synchronous schema refresh succeeded.
- Schema catalog file was generated under the private site path: `sites/golive.site1/private/frapperag/schema_catalog.json`.
- Observed catalog output:
- DocTypes: 1100
- Reports: 308
- Workflows: 5
- Size: about 2.6 MB
- Status: Ready
- Remaining open items:
- `P1A-08` remains open because schema refresh logs are still not visible in standard checked log files.
- The schema catalog currently includes all installed DocTypes, not only allowed/queryable DocTypes.
- Sensitive fieldtypes and hidden fields are currently present in the Phase 1A metadata catalog:
- `Password`
- `Code`
- `Attach`
- `Attach Image`
- `HTML Editor`
- `Text Editor`
- hidden fields
- This is acceptable for Phase 1A because the catalog is private and not passed to Gemini yet.
- Phase 2 must enforce schema slicing and field safety before any schema context is used in prompts or tools.
- Phase 2 guardrails:
- Never pass the full schema catalog to Gemini.
- Retrieve only relevant schema slices.
- Expose only enabled/queryable DocTypes.
- Expose only safe fields by default.
- Hidden, Password, sensitive, attachment, HTML/code-like, and long-text fields must be excluded or explicitly marked unsafe unless manually allowed.
- Planner/composer prompts must use schema snippets, not the full catalog.
- Keep `assistant_mode = v1` until router and live-query execution are tested.
- Do not disable record-level vector indexing until v2 structured query answers are proven.

## 2. Current Architecture
- App wiring is centralized in [hooks.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/hooks.py:15): scheduler health/stall jobs, wildcard `doc_events` sync hooks, install/migrate hooks, and permission query conditions for chat records.
- Installation/runtime setup is sidecar-centric in [setup/install.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/setup/install.py:8): it creates bench-level `rag/`, injects Procfile/supervisor entries, seeds defaults, and manages embedding-provider switching.
- Settings are concentrated in the `AI Assistant Settings` singleton and child tables for allowed DocTypes, roles, reports, and aggregate fields ([ai_assistant_settings.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.json:7), [rag_allowed_doctype.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/rag_allowed_doctype/rag_allowed_doctype.json:7), [rag_allowed_report.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/rag_allowed_report/rag_allowed_report.json:7), [rag_aggregate_field.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/rag_aggregate_field/rag_aggregate_field.json:7)).
- Chat flow is `api/chat.py` → background `run_chat_job()` → retrieval → prompt build → Gemini → optional report/query tool execution → raw SQL writes back to `Chat Message` ([chat_runner.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/chat_runner.py:65), [prompt_builder.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/prompt_builder.py:34), [retriever.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/retriever.py:7)).
- Indexing flow is `api/indexer.py` → `DocIndexerTool` → `run_indexing_job()` → `text_converter.py` summaries → sidecar upsert batch into LanceDB ([indexer.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/indexer.py:49), [text_converter.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/text_converter.py:1)).
- Incremental sync is driven by wildcard transactional hooks in [sync_hooks.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/sync_hooks.py:47) and executed by [sync_runner.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/sync_runner.py:16), with audit in `Sync Event Log`.
- The sidecar owns embedding, vector search, chat proxying, and LanceDB storage ([sidecar/main.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/sidecar/main.py:166), [store.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/sidecar/store.py:23)).
- Valuable parts to keep: `Chat Session`/`Chat Message`, report whitelist execution, aggregate allowlist concept, sidecar client retry logic, health monitoring, and the current permission-first posture.
- Heavy/complex parts: wildcard `doc_events`, hard-coded text conversion for transactional records, per-record duplication into LanceDB, “vector search first” chat orchestration, and mixed concerns inside one settings model.

## 3. Problems in Current Design
- Structured ERPNext data is duplicated into LanceDB row-by-row even though MariaDB is already the source of truth; this inflates storage and makes freshness expensive.
- Every save/rename/delete on allowed DocTypes can enqueue sync work from the transaction path ([hooks.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/hooks.py:31), [sync_hooks.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/sync_hooks.py:47)); even when technically “lightweight,” this couples assistant behavior to core business writes.
- Chat always starts with vector retrieval before deciding whether exact live data access is better ([chat_runner.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/chat_runner.py:124)); that is the core “RAG wall.”
- Record lookup and analytics already show the system wants live execution, but those tools are bolted onto a RAG-first pipeline rather than being the primary path ([query_executor.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/query_executor.py:198), [report_executor.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/rag/report_executor.py:38)).
- The sidecar currently owns too much runtime responsibility, but `/chat` should remain in place during the first v2 refactor while business routing/planning/execution moves into Frappe code.
- `AI Assistant Settings` currently mixes indexing scope, query permissions, model config, and health UI; v2 needs a clearer policy/data-source split.

## 4. Target Architecture
- Request flow should become: `send_message` → `chat_orchestrator` → `intent_router` → `schema_engine` lookup → `query_planner` → `plan_validator` → `safe_executor` and/or `document_rag_engine` → `answer_composer` → save response.
- Intent classes should be exactly: `structured_query`, `erpnext_help`, `document_rag`, `report_query`, `mixed_query`, with `mixed_query` allowed to execute both live structured steps and document retrieval.
- Structured questions should default to live execution against ERPNext via `get_list`, Query Builder, allowed reports, or curated query templates.
- ERPNext process/help questions should use schema/workflow/report metadata first, with document snippets only when they add real operating guidance.
- Document RAG should only cover approved sources: PDFs, attachments, SOPs, support notes, long text, client requirements, API docs, and optional selected long-text fields.
- Schema awareness should be primary context: DocTypes, modules, fields, labels, fieldtypes, select options, links, child tables, custom fields, property setters, workflows, reports, and permission summaries.

## 5. Component Design
- Create a new `frapperag/assistant/` package and leave `frapperag/rag/` in compatibility mode until phase 5.
- `assistant/chat_orchestrator.py`: add a new worker orchestration entrypoint while keeping `api/chat.py`, the current sidecar `/chat` call path, and `rag.chat_runner` behavior intact until later phases.
- `assistant/intent_router.py`: lightweight first-pass classifier using heuristics plus Gemini fallback; route confidently before any vector search.
- `assistant/schema_catalog.py`: extract and normalize metadata from `DocType`, `DocField`, `Custom Field`, `Property Setter`, `Workflow`, `Workflow State`, `Report`, and permission metadata into a cached catalog.
- `assistant/schema_refresh.py`: build/refresh the catalog on install, migrate, manual admin action, metadata change hooks, and nightly verification.
- `assistant/planner.py`: produce a structured JSON query plan; no arbitrary SQL, no direct tool execution, and no LLM-generated SQL execution under any path.
- `assistant/plan_validator.py`: enforce allowed DocTypes, fields, operators, row caps, timeout class, child-table access rules, and tool eligibility before any execution.
- `assistant/executors/get_list_executor.py`: simple single-DocType reads with approved fields/filters/order/limit via `frappe.get_list`/`frappe.db.get_list` only.
- `assistant/executors/query_builder_executor.py`: joins, child-table traversals, grouped reads, and relationship-aware plans using reviewed Query Builder/templates plus explicit permission checks and row post-filtering when record names are returned.
- `assistant/executors/report_executor.py`: migrate current report whitelist logic from `rag/report_executor.py`.
- `assistant/executors/template_executor.py`: curated analytics and approved read-only SQL/query-builder templates; migrate current `query_executor.py` logic here.
- `assistant/documents/ingestion.py` and `assistant/documents/retriever.py`: maintain document chunking, embedding, and snippet retrieval; reuse sidecar client and LanceDB.
- `assistant/answer_composer.py`: send Gemini the original question, validated plan, result rows, column meanings, schema context, and optional document snippets through the existing sidecar client for the first refactor; answers must explain returned data, not infer unseen facts.

## 6. Data Model Changes
- Keep `AI Assistant Settings`, `Chat Session`, `Chat Message`, and `AI Indexing Job`; they are already aligned with current UX and operations ([chat_session.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/chat_session/chat_session.json:7), [chat_message.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/chat_message/chat_message.json:7), [ai_indexing_job.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/frapperag/doctype/ai_indexing_job/ai_indexing_job.json:7)).
- Change `AI Assistant Settings` from “what to index” to “global policy and defaults”: assistant mode, planner model, sidecar enabled, document RAG enabled, schema refresh schedule, default row limit, max row limit, query timeout seconds, prompt-injection protection toggle, and fallback behavior.
- Change `RAG Allowed DocType` into a live-query policy table: `doctype_name`, `enabled`, `default_date_field`, `default_title_field`, `allow_get_list`, `allow_query_builder`, `allow_child_tables`, `default_sort`, `default_limit`, `large_table_requires_date_filter`.
- Keep `RAG Aggregate Field` in Phase 1 and through the migration period; add `AI Queryable DocType` with child tables `AI Queryable Field` and `AI Child Table Policy`, then remove/replace `RAG Aggregate Field` only after the new policy model is implemented and migrated.
- Keep `RAG Allowed Report` short term but add `max_rows`, `semantic_description`, and `enabled`; long term it can be renamed without changing semantics.
- Deprecate `Sync Event Log` for structured record sync; keep it only if document-source ingestion still needs per-source audit.
- Repurpose `AI Indexing Job` to support `job_type`: `schema_refresh`, `document_ingest`, `document_reindex`, `document_purge`.
- Add `AI Tool Call Log` as a first-class DocType for every tool/query/report/document-search execution, including route, plan, permission, row-count, and outcome metadata.
- Add `AI Query Template` plus child `AI Query Template Param` for approved live-query templates; support handler types `get_list`, `query_builder`, `readonly_sql`, `python_handler`.
- Add `AI Document Source` for document RAG sources; fields should cover `source_type`, `doctype_or_path`, `fieldname`, `filter_json`, `chunking_profile`, `sync_mode`, `enabled`, and `last_indexed_at`.
- Start with cached `AI Schema Catalog` data plus refresh status/metadata that admins can inspect; add `AI Schema Entity` only if the catalog proves too opaque after Phase 1.

## 7. Query/Tool Design
- `get_schema_info`: returns normalized schema entries for a DocType/report/workflow/module; validation allows only catalog-backed entities and caps related fields/relationships returned.
- `find_relevant_doctypes`: internal retrieval over the schema catalog; validation restricts output to enabled queryable DocTypes and reports.
- `run_get_list`: uses `frappe.db.get_list`/`frappe.get_list`; validation restricts DocType, field list, operators, order-by, limit, and optional date filters based on policy.
- `run_query_template`: executes only admin-authored reviewed templates from `AI Query Template`; validation enforces template key existence, parameter schema, row limit, execution mode, and explicit permission checks plus row post-filtering when record names are returned.
- `run_allowed_report`: reuses the current whitelist pattern; validation requires enabled report, `Report Builder` type, user read permission, allowed filters, and max row cap.
- `search_documents`: calls the sidecar only for enabled document sources; validation restricts scope, `top_k`, chunk length, and snippet return size.
- Safe operators for live plans should be limited to `=`, `in`, `between`, `>=`, `<=`, `like_prefix`, and boolean flags; free-form SQL operators stay forbidden.
- Read-only SQL remains optional and only inside reviewed templates; no raw SQL generated by the LLM may execute, and any allowed SQL must be code- or DocType-defined, parameterized, capped, and executed with a per-statement timeout.
- The planner output format should be structured JSON with `intent`, `confidence`, `steps`, `final_answer_shape`, `needs_clarification`, and `clarification_question`; the executor never accepts plain-text instructions.

## 8. Security Model
- Enforce `frappe.has_permission` at three levels: plan-time DocType eligibility, execution-time DocType/report permission, and parent-row checks for any child-table expansion.
- Restrict fields through explicit allowlists, not “all readable fields”; sensitive/private/hidden fields must be deny-by-default and only exposed via explicit opt-in.
- Permission summaries in the schema catalog should be role-based metadata only; never precompute per-user access snapshots.
- Child tables should only be queryable when the parent DocType is allowed, the parent row is readable, and child fields are explicitly allowed in policy.
- Prompt injection defense should treat document chunks, report cells, and database strings as untrusted data; composer prompts must label them as data, not instructions, and strip HTML/script-like content before inclusion.
- Planner and composer prompts should be separated: planner sees schema/policy and minimal user context, composer sees validated results and snippets, not raw execution privileges.
- All structured tools must be read-only, row-capped, timeout-capped, and logged in `AI Tool Call Log` with plan ID, tool name, doctype/report/template, and caller.

## 9. Performance Strategy
- Expected load reduction: remove the dominant source of assistant-related write amplification by eliminating structured DocType sync jobs on normal transactional saves; in a typical ERPNext deployment this should cut assistant background job volume and embedding calls dramatically.
- Expected storage reduction: LanceDB should shrink from “many transactional records per allowed DocType” to “documents plus optional schema summaries,” with an 80–95% vector-row reduction as a realistic target for ERP-heavy sites.
- Jobs to remove: wildcard structured sync jobs, structured purge jobs, bulk record reindex jobs for transactional DocTypes, and most of the `Sync Event Log` churn.
- Jobs to keep: schema refresh, document ingestion/reindex, optional long-text source sync, health checks, and stalled-job cleanup.
- Sidecar should remain for document embedding/search, optional local model installation, and Gemini `/chat` during the first v2 refactor; business routing/planning/execution should move into Frappe code while Gemini calls continue through the existing sidecar client.
- LanceDB should remain optional; if no document sources are configured, the assistant should still function in schema/live-query mode without any vector store dependency.
- Latency strategy: route before retrieval, keep schema catalog cached in MariaDB/Redis, cap structured results at 20–50 rows, use one planner call plus one composer call for structured answers, and avoid document search unless the router or planner explicitly requests it.

## 10. Migration Phases
1. Phase 1: preserve current app behavior exactly, add v2 scaffolding, and introduce feature flags only. Chat answers must not change; keep `api/chat.py`, `rag_chat`, chat DocTypes, current sidecar `/chat`, existing RAG path, and all v1 files untouched. Add `assistant_mode = v1|hybrid|v2`, schema catalog refresh/status, `AI Tool Call Log`, and `AI Indexing Job.job_type`.
2. Phase 2: build schema indexing and intent routing in shadow mode. Add schema catalog refresh, metadata-specific hooks for `DocType`/`Custom Field`/`Property Setter`/`Workflow`/`Report`, and a router that classifies every incoming question; log route decisions without changing answers.
3. Phase 3: shift structured questions to live execution. Introduce planner, validator, `run_get_list`, `run_query_template`, and `run_allowed_report`; keep Gemini calls through the existing sidecar client, keep document RAG as fallback for `document_rag`, `erpnext_help`, and `mixed_query`, and leave v1 files in place.
4. Phase 4: connect the shadow router and validated live-query path to chat behind `assistant_mode = hybrid` only. Keep `assistant_mode = v1` as the default, preserve the existing v1 answer path in `v1` mode, and fail closed back to v1 for unsupported or rejected hybrid requests.
5. Phase 4B: harden the approved hybrid path with a repeatable structured-query matrix, debug probes, and tool-log verification while keeping the runtime default at `v1` and the hybrid scope limited to validated read-only `get_list`.
6. Phase 4C: add the self-serve analytics foundation. Introduce curated analytics building blocks for reusable metrics, dimensions, filters, and saved question definitions so broader analytics can be planned safely without opening raw SQL or write paths.
7. Phase 5: disable record-level vector indexing by default. Remove transactional DocTypes from default indexing, stop wildcard structured sync hooks, convert long-text indexing to explicit `AI Document Source` policies, and keep only document/attachment ingestion jobs; do not delete legacy v1 files yet.
8. Phase 6: clean up old architecture. After migration is complete, retire `rag/text_converter.py` for structured records, narrow or remove `rag/query_executor.py` and `rag/chat_runner.py`, deprecate `Sync Event Log` if unused, evaluate whether sidecar `/chat` can be reduced, and remove superseded v1 components.

## 11. Acceptance Criteria
- Functional: the assistant correctly routes structured questions to live queries, process/help questions to schema-aware answers, document questions to RAG, and mixed questions to combined execution without relying on record-level vector context.
- Functional: the system can answer live questions about allowed DocTypes, reports, relationships, child tables, and workflows using validated plans and permission-safe reads.
- Performance: normal transactional saves on structured DocTypes no longer enqueue vector sync work by default; document indexing remains the only steady-state embedding workload.
- Performance: first-answer latency for structured queries is lower and more consistent than the current RAG-first path because vector search is skipped unless needed.
- Safety: no tool can execute arbitrary SQL, write data, bypass `frappe.has_permission`, or expose non-allowlisted fields; blocked requests return explicit clarifications or safe denials.
- Operability: admins can see schema refresh status, document source status, and the execution path used for each answer.
- Testing: manual acceptance tests cover route decisions, permission enforcement, field allowlists, and verification that normal transactional saves do not trigger background structured sync.

## 12. Risks and Tradeoffs
- Moving away from record-level RAG makes fuzzy recall of obscure record text harder; the tradeoff is correctness, lower cost, and simpler operations.
- Live querying can fail when the user underspecifies filters or uses business language that maps to multiple DocTypes; the planner must prefer clarification over guessing.
- Query Builder and `get_list` will not cover every analytics case elegantly; curated templates and allowed reports remain necessary for cross-table or high-value business questions, and any Query Builder path returning record names needs explicit permission checks plus row post-filtering.
- Schema catalogs can drift if metadata refresh is missed; nightly verification plus metadata-specific hooks is required.
- Document RAG remains useful for policies, contracts, SOPs, implementation notes, ticket narratives, and long text fields; it should remain a secondary but first-class path.
- Minor API drift is allowed, but preserving current chat storage and page flow reduces migration risk and avoids unnecessary user retraining.

## 13. Recommended First Implementation Step
- Implement the non-behavioral foundation before changing answer behavior: add `assistant_mode`, introduce the new query-policy DocTypes alongside existing v1 tables, build cached `AI Schema Catalog` refresh/status, add `AI Tool Call Log`, and add a manual “Refresh Schema Catalog” job that reads current ERPNext/Frappe metadata.
- This is the highest-leverage first step because it creates the source of truth and observability needed by the router, planner, executor, admin UI, and migration metrics while leaving current chat answers and the `rag` pipeline untouched.
- Assumptions locked for v2: preserve current chat UX with minor API drift, keep Gemini `/chat` in the sidecar during the first refactor, move business logic into `frapperag/assistant/`, target Frappe/ERPNext v15, and disable structured record vector indexing by default once live-query paths are stable.

Phase 1A:
Done. Added `assistant_mode`, schema catalog cache/refresh scaffolding, refresh API/UI/status, and migrate/install bootstrap updates while keeping the existing v1 chat pipeline unchanged.

Phase 1B:
Implemented. Added query-policy fields on `RAG Allowed DocType`, safe schema-slice helpers under `frapperag/assistant/schema_policy.py`, migrate-time/default backfills for policy defaults, and manual verification commands while keeping `assistant_mode = v1` and the existing v1 chat path unchanged.
- Verification results:
- `bench --site golive.site1 migrate` passed on the site.
- `assistant_mode` remains `v1`.
- `refresh_schema_catalog(reason='phase_1b_manual')` returned `Ready` with 1100 DocTypes, 308 reports, 5 workflows, and catalog path `./golive.site1/private/frapperag/schema_catalog.json`.
- `debug_query_policy_snapshot` confirms `enabled` is the new query-policy field. Legacy v1 indexing/sync/aggregate/chat paths still key off `allowed_doctypes` row presence and `doctype_name`, not `enabled`.
- `debug_safe_schema_slice` for `Sales Invoice` and `Customer` excludes unsafe fields by default and marks them explicitly when `include_unsafe_fields=1`.
- Reversible live verification on `golive.site1` confirmed deny-by-default exclusion when a policy row is disabled: setting `Customer.enabled = 0` removed `Customer` from `debug_safe_schema_slice`, and restoring `enabled = 1` restored the slice.
- Deny-by-default coverage was confirmed for hidden fields, `Password`, `Attach`, `Attach Image`, `Code`, `HTML Editor`, `Text Editor`, long-text fields, and `Table`/child-table fields unless explicitly included for inspection.
- Full schema catalog usage remains isolated to `frapperag.assistant.schema_policy` and schema refresh/catalog helpers; the existing Gemini prompt/tool path still uses retriever context plus the existing v1 report/query tools and does not pass the full catalog.
- No Phase 2 runtime code was introduced: no router, planner, executor, answer-composer, or chat-path switch is present, and the existing v1 chat entrypoints remain unchanged.
No live end-to-end Gemini chat smoke test was run. v1 chat unchanged was verified by assistant_mode remaining v1, unchanged v1 entrypoints/code path, and no runtime switch to v2/hybrid behavior.

Phase 1C:
Add manual Refresh Schema Catalog job.
Phase 1C: completed / absorbed into Phase 1A and verified during Phase 1B.

Phase 2:
Live smoke blocker investigation on `golive.site1`:
- Blocker observed: a real API chat message reached the unchanged v1 worker, logged `ROUTER_SHADOW`, then failed in the existing retrieval step after repeated sidecar `/search` `HTTP503` responses.
- Root cause: the site is configured with `embedding_provider = "gemini"`, and the Gemini sidecar embedding path requires an API key for `/search`. `run_chat_job()` already reads `gemini_api_key`, but the v1 retrieval path dropped that key: `chat_runner.py` called `search_candidates(question)` and `retriever.py` called `sidecar_client.search(...)` without forwarding `api_key`. Indexing and sync paths already passed the key correctly, so the bug was isolated to v1 query-time retrieval.
- Sidecar/config checks:
- Sidecar health remained reachable on `http://127.0.0.1:8100/health`.
- `assistant_mode` remained `v1`.
- Active embedding provider remained `gemini`, with active prefix `v5_gemini_`.
- On-disk `rag/v5_gemini_*` tables exist. `tables_populated('v5_gemini_')` returned empty during this check, but that was not the cause of the `HTTP503`; after the fix, `/search` returned normally and the chat smoke completed with zero candidates instead of failing.
- Minimal fix applied: thread the already-loaded `api_key` from `run_chat_job()` into `search_candidates(question, api_key=api_key)`, and forward that through `retriever.py` to `sidecar_client.search(..., api_key=api_key)`. No router logic, prompt logic, planner/executor code, or answer behavior was changed.
- Final smoke result: a fresh API message `RAG-MSG-2026-05-070003` in session `RAG-SESS-2026-05-070003` completed successfully on `2026-05-07 18:28:24 UTC` with assistant reply `Hello! How can I help you today?`
- Runtime evidence from the successful smoke:
- Existing v1 path was used: `api/chat.py` → queued `frapperag.rag.chat_runner.run_chat_job` → `search_candidates` → `build_messages` → `generate_response`.
- `ROUTER_SHADOW` was logged for that same message before retrieval.
- `/search` no longer returned `HTTP503`; log line showed `search_candidates 0.632s → 0 candidates`.
- `generate_response` succeeded and the message ended with `[CHAT_SUCCESS]`.
- `use_llm_fallback` remains `False` in the normal v1 worker path.
- Router failure remains non-blocking, and no router behavior was changed as part of this fix.
- No Phase 2.5 or Phase 3 code was introduced.

Phase 2.5:
Dependency and smoke verification on `golive.site1`:
- Dependency/runtime verification passed with Frappe-compatible pins preserved:
- `google-auth == 2.48.0`
- `PyJWT == 2.8.0`
- `requests == 2.32.5`
- `google-genai` import: OK
- `GoogleSearch` available: `True`
- `bench --site golive.site1 migrate` had already passed before smoke verification.
- Live v1 smoke was run through the existing chat API entrypoints: `create_session()` → `send_message()` → queued `frapperag.rag.chat_runner.run_chat_job()` → `get_message_status()`.
- Smoke artifacts:
- Session: `RAG-SESS-2026-05-070007`
- Message: `RAG-MSG-2026-05-070007`
- Final assistant reply: `Hello! How can I help you today?`
- Runtime evidence from the successful smoke:
- Existing v1 worker path remained active: `api/chat.py` → `search_candidates` → `build_messages` → `generate_response`.
- `assistant_mode` remained `v1`.
- `chat_model` remained `gemini-2.5-flash`.
- `embedding_provider` remained `gemini`.
- `sidecar_client.health_check()` returned `{"ok": true, "url": "http://127.0.0.1:8100/health", "detail": null}`.
- Worker logs for `RAG-MSG-2026-05-070007` showed normal shadow-routing and chat completion:
- `ROUTER_SHADOW` logged normally before retrieval.
- `search_candidates 0.566s → 0 candidates`
- `[TIMING][chat_engine] sidecar /chat 1.258s`
- `[CHAT_SUCCESS] message_id=RAG-MSG-2026-05-070007`
- This confirms the sidecar `/chat` path works through the `google-genai` runtime and produces a final assistant answer in the unchanged v1 flow.
- Google Search grounding support remains present but disabled by default:
- `debug_chat_runtime_settings()` reported `google_search_enabled = false`.
- For allowed future intents `erpnext_help`, `out_of_scope`, and `web_current_info`, `google_search_would_be_used = false` with both `has_erp_context = 0` and `has_erp_context = 1`.
- This confirms ERP-context prompts cannot enable Google Search in the current v1 runtime, and grounding remains off unless future settings and routed callers explicitly opt in.
- Verification-only update: no Phase 3 code was introduced in this pass.

Phase 3:
Manual-only safe live-query foundation implemented without changing normal chat behavior:
- Added `frapperag.assistant.planner` for `get_list` plan scaffolding and bench creation helpers.
- Added `frapperag.assistant.plan_validator` for enabled/queryable DocType checks, safe-field enforcement, allowed filter/operator enforcement, allowed sort-field enforcement, row caps, and large-table date-filter requirements.
- Added `frapperag.assistant.executors.get_list_executor` for read-only `frappe.get_list` execution using validated plans only.
- Added `AI Tool Call Log` plus `frapperag.assistant.tool_call_log` for planner/validator/executor audit records.
- Added manual bench verification helpers for describe/create/validate/execute/log-inspection flows.
- Confirmed scope boundaries for this pass:
- `assistant_mode` remains `v1`.
- Existing v1 chat answers and final Gemini responses remain unchanged.
- No normal-chat routing to Phase 3 was added.
- No Google Search was enabled.
- No write actions were implemented.
- No raw SQL execution path was added.
- No Query Builder joins were added.
- Still deferred to later phases: answer composer, normal-chat v2 execution wiring, report/template migration, and any broader multi-step orchestration beyond safe `get_list`.
- Verification results on `golive.site1`:
- Fresh end-to-end v1 chat smoke passed through the existing API/UI path: `create_session()` → `send_message()` → queued `frapperag.rag.chat_runner.run_chat_job()` → `get_message_status()`.
- Smoke artifacts:
- Session: `RAG-SESS-2026-05-070008`
- Message: `RAG-MSG-2026-05-070008`
- Final assistant reply: `Hello!`
- Runtime evidence from `logs/frapperag.log` for `RAG-MSG-2026-05-070008`:
- Existing v1 worker path remained active: `run_chat_job START` → `search_candidates 0.790s → 0 candidates` → `build_messages` → `[TIMING][chat_engine] sidecar /chat 2.862s` → `generate_response` → `[CHAT_SUCCESS]`.
- `ROUTER_SHADOW` logged normally for the same message before retrieval.
- `assistant_mode` remained `v1` before and after the smoke.
- Chat did not use Phase 3 planner/validator/executor code. The only `frapperag.assistant` import in the live chat worker remains the non-blocking shadow router, and the smoke produced no planner/validator/executor activity tied to the chat message.
- Large-table date guard was live-verified manually against allowed DocType `Sales Invoice`:
- Original policy value was `large_table_requires_date_filter = 0`.
- Temporarily set the `RAG Allowed DocType` child row for `Sales Invoice` to `1`, then cleared cached `AI Assistant Settings` because `schema_policy.load_allowed_doctype_policies()` reads the parent through `get_cached_doc()`.
- `debug_describe_queryable_doctype('Sales Invoice')` then showed `default_date_field = posting_date` and `large_table_requires_date_filter = 1`.
- Manual request `phase3-verify-no-date-guard-20260507` was rejected with `Step 1 must include a date filter on 'posting_date' for DocType 'Sales Invoice'.`
- Manual request `phase3-verify-with-date-20260507` succeeded through `debug_build_validate_and_execute_get_list_plan(...)` and returned 2 `Sales Invoice` rows.
- Restored `large_table_requires_date_filter` to `0`, cleared the settings cache again, and re-checked that `Sales Invoice` policy returned to `0`.
- `AI Tool Call Log` verification passed:
- Success entries were created for the valid manual executor run, including `validator.validate_plan` and `executor.get_list.execute_validated_plan` with `request_id = phase3-verify-with-date-20260507`, `row_count = 2`, and `assistant_mode = v1`.
- Rejected entries were created for invalid plans with `assistant_mode = v1`, including:
- `phase3-verify-no-date-guard-20260507` → missing required date filter
- `phase3-verify-disabled-doctype-20260507` → `User` not enabled for live queries
- `phase3-verify-unsafe-field-20260507` → unsafe field `customer_name`
- `phase3-verify-limit-20260507` → limit above 200
- `phase3-verify-raw-sql-20260507` → unsupported step key `sql`
- `phase3-verify-query-builder-20260507` → unsupported tool `query_builder`
- Fail-closed behavior was confirmed:
- Disabled DocTypes are rejected.
- Unsafe fields are rejected.
- Excessive limits are rejected.
- Raw SQL payloads are rejected before execution.
- Query Builder execution was not added; the validator rejects `tool = query_builder`, and `frapperag/assistant/executors/` still contains only `get_list_executor.py`.
- Normal v1 chat answer behavior remained unchanged during this verification.
- At the end of this Phase 3 verification pass, no Phase 4 code had been introduced yet.

Phase 4:
- Controlled hybrid chat integration implemented behind `assistant_mode = hybrid` only:
- Added `frapperag.assistant.chat_orchestrator` for the hybrid-only router → planner → validator → `get_list` executor → grounded composer flow.
- Added `frapperag.assistant.answer_composer` for concise grounded answers over validated live-query results.
- Updated `frapperag.rag.chat_runner.run_chat_job()` to keep `ROUTER_SHADOW` logging in all modes and attempt the hybrid branch only when `assistant_mode = hybrid`.
- Confirmed fail-closed hybrid fallback targets:
- non-structured intents
- low-confidence routes
- unsupported plans
- validation rejection
- executor failure
- composer failure
- unexpected hybrid errors
- Confirmed scope boundaries for this phase:
- Default `assistant_mode` remains `v1`.
- Existing v1 chat behavior remains unchanged in `v1` mode.
- Phase 3 planner/validator/executor are not used by chat in `v1` mode.
- No Google Search was enabled.
- No raw SQL execution path was added.
- No Query Builder joins were added.
- No write actions were implemented.
- No unsafe fields are intentionally exposed to Gemini.
- The full schema catalog is still not passed to Gemini; hybrid planning uses only bounded safe schema snippets for routed candidate DocTypes.
- Record-level vector indexing was not disabled as part of this phase.
- Observability and verification notes:
- Successful hybrid planner/validator/executor/composer calls log to `AI Tool Call Log` with `assistant_mode = hybrid`.
- Rejected hybrid plans log as `Rejected` in `AI Tool Call Log` with `assistant_mode = hybrid`.
- If `RAG Allowed DocType` child-row policy values are changed directly during verification, clear cached `AI Assistant Settings` before retesting because policy loading uses `get_cached_doc()`.
- Cache-clear command:
- `bench --site golive.site1 execute frappe.clear_document_cache --args '["AI Assistant Settings", "AI Assistant Settings"]'`
- Verification results on `golive.site1` (`2026-05-08`):
- Diagnosis:
- Router candidate DocTypes were already correct for the failing prompts:
- `List 3 customers` → `structured_query`, candidates `Customer`, `Delivery Note`, `Purchase Invoice`, ...
- `List the latest 3 Sales Invoices since 2026-01-01` → `structured_query`, candidates `Sales Invoice`, `Sales Order`, `Customer`, ...
- The planner bug was in the safe schema snippets passed to Gemini, not in routing:
- `_build_planner_schema_snippets()` truncated each DocType to the first 12 safe catalog fields in raw metadata order.
- That slice omitted validator-safe standard fields such as `name`, `modified`, `creation`, and `docstatus`.
- It also omitted later list-friendly safe fields such as `grand_total` and `status` for `Sales Invoice`.
- Result: Gemini often returned an empty plan payload (`doctype = ""`, `fields = []`) for obvious list questions, or asked for clarification for fields that were actually validator-safe but absent from the snippet.
- Minimal fix shipped in `frapperag/assistant/planner.py` only:
- Prepended the same safe standard fields the validator already allows (`name`, `modified`, `creation`, `docstatus`) to planner-visible schema snippets.
- Replaced raw-order truncation with a ranked safe-field selection that favors default date/title fields plus list-view/filter-friendly fields.
- Added `suggested_fields` to planner snippets and tightened the planner prompt so `doctype` must be present and must match a listed schema snippet name.
- Added narrow DocType parsing normalization for canonical allowed names only; validator safety rules were not weakened.
- Verification sequence:
- Confirmed `assistant_mode` started as `v1`.
- Initial v1 smoke passed unchanged:
- Session `RAG-SESS-2026-05-080006`
- Message `RAG-MSG-2026-05-080006`
- Final answer returned successfully with no citations.
- Worker log showed the existing v1 path (`load_whitelist` → `search_candidates` → `build_messages` → `generate_response`) and no `hybrid_attempt` / `HYBRID_*` lines.
- No `AI Tool Call Log` rows were created for `request_id = hybrid-RAG-MSG-2026-05-080006`.
- Switched temporarily to `assistant_mode = hybrid`.
- Hybrid structured-query success 1:
- Session `RAG-SESS-2026-05-080007`
- Message `RAG-MSG-2026-05-080007`
- Question: `List 3 customers`
- Router selected `structured_query` with `confidence = 0.68`.
- Planner selected `DocType = Customer`.
- Validator accepted.
- Read-only `get_list` executor returned 3 rows.
- Grounded composer returned the final answer.
- `query_result` citation was emitted with `doctype = Customer`, `columns = ["name", "customer_group", "territory"]`, `row_count = 3`.
- `AI Tool Call Log` recorded `Success` rows for `planner.plan_structured_query`, `validator.validate_plan`, `executor.get_list.execute_validated_plan`, and `composer.compose_structured_answer`, all with `assistant_mode = hybrid`.
- Worker log showed `[HYBRID_SUCCESS] ... request_id=hybrid-RAG-MSG-2026-05-080007 ... rows=3` and `hybrid_attempt ... handled=yes`.
- Hybrid structured-query success 2:
- Session `RAG-SESS-2026-05-080008`
- Message `RAG-MSG-2026-05-080008`
- Question: `List the latest 3 Sales Invoices since 2026-01-01`
- Planner selected `DocType = Sales Invoice`.
- Validator accepted.
- Read-only `get_list` executor returned 3 rows.
- Final answer included a `query_result` citation for `Sales Invoice`.
- `AI Tool Call Log` recorded `Success` rows for planner, validator, executor, and composer with `assistant_mode = hybrid`.
- Hybrid rejected-structured fallback passed:
- Session `RAG-SESS-2026-05-080010`
- Message `RAG-MSG-2026-05-080010`
- Question: `List the latest 3 Sales Invoices with item rows since 2026-01-01`
- Router still selected `structured_query` with `confidence = 0.82`.
- Planner rejected the request because `item rows` would require child-table access.
- `AI Tool Call Log` recorded `planner.plan_structured_query` as `Rejected` with `assistant_mode = hybrid`.
- Worker log showed `HYBRID_FALLBACK ... reason=planner_rejected` followed by `hybrid_attempt ... handled=no`.
- Chat safely fell back to the existing v1 path and returned a v1 `query_result` citation using template `aggregate_doctype`.
- Restored `assistant_mode = v1`.
- Final v1 smoke passed unchanged:
- Session `RAG-SESS-2026-05-080011`
- Message `RAG-MSG-2026-05-080011`
- Final answer returned successfully with no citations.
- Worker log again showed the unchanged v1 path with no `hybrid_attempt` / `HYBRID_*` lines.
- No `AI Tool Call Log` rows were created for `request_id = hybrid-RAG-MSG-2026-05-080011`.
- Verification scope checks:
- Google Search remained disabled during this pass.
- No raw SQL, Query Builder join path, or write action was added to the hybrid structured-query flow.
- Record-level vector indexing was not disabled in this fix.
- No Phase 5 cleanup was introduced and no v1 files were removed.

Phase 4B: Completed / Approved
- Hybrid hardening stayed inside the approved Phase 4 scope:
- Default `assistant_mode` remains `v1`.
- Hybrid remains limited to validated read-only `get_list`.
- No Google Search was enabled.
- No raw SQL, Query Builder joins, or write actions were introduced.
- No v1 files were removed.
- No record-level vector-index disabling or other Phase 5 cleanup was introduced.
- Added a manual structured-query matrix at [phase4b_hybrid_matrix.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_matrix.json).
- Added a bench runner at [phase4b_hybrid_runner.py](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_runner.py).
- Added `frapperag.assistant.chat_orchestrator.debug_probe_hybrid_path(...)` for debug-only hybrid probing of validation/execution and fail-closed fallback behavior without changing live `v1` chat execution.
- Manual commands:
- `bench --site golive.site1 execute frapperag.tests.phase4b_hybrid_runner.run_matrix`
- `bench --site golive.site1 execute frapperag.tests.phase4b_hybrid_runner.run_case --kwargs "{'case_id': 'unsafe_field_rejection'}"`
- `bench --site golive.site1 execute frapperag.assistant.tool_call_log.debug_get_recent_tool_logs --kwargs "{'limit': 20}"`
- Recorded matrix execution on `golive.site1`:
- Started at `2026-05-07T22:11:17Z`.
- Results file: [phase4b_hybrid_results_20260507T221117Z.json](/home/ah_hammadi/golive-bench/apps/frapperag/frapperag/tests/phase4b_hybrid_results_20260507T221117Z.json).
- Summary: `11 / 11` cases passed.
- `assistant_mode` was `v1` before the run and `v1` after the run.
- `AI Tool Call Log` rows emitted by the debug probe were tagged with `assistant_mode = hybrid` for accurate diagnosis while preserving the live default mode.
- Safe `get_list` cases passed:
- `customer_list` returned 5 rows from `Customer`.
- `sales_invoice_list_with_date_filter` returned 5 rows from `Sales Invoice`.
- `sales_invoice_latest_records` returned 3 rows from `Sales Invoice`.
- `item_list` returned 5 rows from `Item`.
- `supplier_list` returned 5 rows from `Supplier`.
- Unsafe or unsupported cases were rejected and recorded as hybrid fallback outcomes:
- `unsafe_field_rejection` rejected `Sales Invoice.items`.
- `disabled_doctype_rejection` rejected `User`.
- `excessive_limit_rejection` rejected `limit = 500`.
- `child_table_query_rejection_fallback` rejected `Sales Invoice Item`.
- Route-level fallback cases passed without attempting live hybrid execution:
- `unclear_query_fallback` routed to `unclear` and returned `fallback_reason = non_structured`.
- `non_structured_query_fallback` routed to `document_rag` and returned `fallback_reason = non_structured`.
- No additional runtime hybrid bug required a production-path fix beyond adding the debug probe and the manual matrix runner.

Phase 4C:
Self-Serve Analytics Foundation implemented as unused/import-safe foundation only.
- Added `frapperag/assistant/analytics/` with:
- `analytics_plan_schema.py`
- `relationship_graph.py`
- `metric_registry.py`
- `analytics_validator.py`
- Implemented Option A only: a structured JSON analytics DSL. No guarded text-to-SQL, no raw LLM SQL, and no analytics executor were introduced.
- Supported analytics plan shapes:
- `single_doctype_aggregate`
- `parent_child_aggregate`
- `time_bucket_aggregate`
- `period_comparison`
- `co_occurrence`
- `top_n`
- `bottom_n`
- `ratio`
- `trend`
- Added a curated safe relationship graph for:
- `Sales Invoice -> Sales Invoice Item`
- `Sales Order -> Sales Order Item`
- `Purchase Invoice -> Purchase Invoice Item`
- `Purchase Order -> Purchase Order Item`
- `Customer -> Territory`
- `Customer -> Customer Group`
- `Item -> Item Group`
- `Payment Entry -> Party`
- `Stock Ledger Entry -> Item`
- `Stock Ledger Entry -> Warehouse`
- Added a curated metric registry for:
- `sales_amount`
- `sales_qty`
- `invoice_count`
- `avg_invoice_value`
- `outstanding_amount`
- `purchase_amount`
- `purchase_qty`
- `stock_qty`
- `movement_qty`
- Added fail-closed analytics validation for:
- allowed DocType
- allowed field
- allowed metric
- allowed relationship
- allowed analysis type
- safe limit
- required date field for large tables
- no write operation
- no SQL string
- no unsupported child-table traversal
- The validator reuses existing `RAG Allowed DocType` query-policy fields where available, including `enabled`, `default_date_field`, `allow_child_tables`, `default_limit`, and `large_table_requires_date_filter`.
- Runtime boundaries preserved:
- no changes to v1 chat behavior
- no changes to hybrid runtime behavior beyond import-safe unused foundation code
- no vector sync changes
- no file/image/document work
- no write actions

Phase 5:
Disable transactional vector sync by default.

Phase 6:
Clean old files and sidecar endpoints.
