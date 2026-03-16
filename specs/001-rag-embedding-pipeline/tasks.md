# Tasks: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline`
**Date**: 2026-03-15 (updated after spec clarifications)
**Input**: `specs/001-rag-embedding-pipeline/`
**App root**: `apps/frapperag/frapperag/`

**No test tasks** — Principle VII prohibits automated tests. Acceptance via `quickstart.md`.

**Implementation order**: DocTypes → hooks.py → Python modules bottom-up
(lancedb_store → text_converter → embedder → base_indexer → indexer → api) → JS page last.

**Spec clarifications incorporated (2026-03-15)**:
- FR-009: Duplicate trigger MUST be rejected immediately — no queuing
- `Failed (Stalled)` is the canonical terminal state for stalled/interrupted jobs
- FR-019: Stalled detection applies to `Running` jobs only; `Queued` jobs are exempt
- FR-023: Re-index MUST upsert by document ID — table MUST NOT be dropped or rebuilt

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no shared state)
- **[US#]**: User story this task satisfies (from spec.md)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the files that every subsequent task depends on.

- [x] T001 [P] Create `apps/frapperag/frapperag/requirements.txt` with `lancedb>=0.8.0`, `pyarrow>=14.0.0`, `google-generativeai>=0.8.0` (no LangChain, no FAISS, no openai)
- [x] T002 [P] Create `apps/frapperag/frapperag/modules.txt` containing a single line: `FrappeRAG`
- [x] T003 [P] Create all empty `__init__.py` stubs: `setup/__init__.py`, `rag/__init__.py`, `api/__init__.py`, `frapperag/doctype/rag_allowed_doctype/__init__.py`, `frapperag/doctype/rag_allowed_role/__init__.py`, `frapperag/doctype/ai_assistant_settings/__init__.py`, `frapperag/doctype/ai_indexing_job/__init__.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: App wiring that must exist before any DocType or module is useful.

**⚠️ CRITICAL**: hooks.py and install.py must be complete before running `bench migrate` or any acceptance test.

- [x] T004 Create `apps/frapperag/frapperag/hooks.py` with: `app_name="frapperag"`, `app_title="FrappeRAG"`, `after_install="frapperag.setup.install.after_install"`, `fixtures=[{"dt": "Role", "filters": [["name", "in", ["RAG Admin", "RAG User"]]]}]`, and `scheduler_events={"cron": {"*/30 * * * *": ["frapperag.rag.indexer.mark_stalled_jobs"]}}`
- [x] T005 Create `apps/frapperag/frapperag/setup/install.py` with `after_install()` that calls `os.makedirs(frappe.get_site_path("private", "files", "rag"), exist_ok=True)` then `frappe.db.commit()`

**Checkpoint**: Run `bench --site <site> install-app frapperag` — the `private/files/rag/` directory must be created automatically.

---

## Phase 3: User Story 1 — Configure AI Assistant (Priority: P1) 🎯 MVP

**Goal**: System Manager can save a Gemini API key, select allowed DocTypes and roles, and have all three validated on save.

**Independent Test**: Open AI Assistant Settings, save with a blank API key while `Enabled = 1` — system must raise a validation error. Save with valid data — record saves cleanly and key is masked in plain text.

- [x] T006 [P] [US1] Create `apps/frapperag/frapperag/frapperag/doctype/rag_allowed_doctype/rag_allowed_doctype.json` — child table DocType with one field: `doctype_name` (Link → DocType, `in_list_view: 1`); parent DocType = `AI Assistant Settings`, fieldname = `allowed_doctypes`
- [x] T007 [P] [US1] Create `apps/frapperag/frapperag/frapperag/doctype/rag_allowed_role/rag_allowed_role.json` — child table DocType with one field: `role` (Link → Role, `in_list_view: 1`); parent DocType = `AI Assistant Settings`, fieldname = `allowed_roles`
- [x] T008 [US1] Create `apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.json` — Single DocType with fields: `is_enabled` (Check, default 1), `gemini_api_key` (Password, reqd), `sync_schedule` (Select: Manual Only/Daily/Weekly), `allowed_doctypes` (Table → RAG Allowed DocType), `allowed_roles` (Table → RAG Allowed Role); permissions: System Manager full CRUD, RAG Admin read+write
- [x] T009 [US1] Create `apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.py` — `AIAssistantSettings` controller with `validate()` that raises `frappe.ValidationError` when `is_enabled=1` and any of: `gemini_api_key` is blank, `allowed_doctypes` is empty, `allowed_roles` is empty

**Checkpoint**: `bench --site <site> migrate` must create the DocType. Open AI Assistant Settings in Desk — all fields visible. Save with missing API key while Enabled — error raised. ✅ US1 complete.

---

## Phase 4: User Story 2 — Trigger Indexing Job (Priority: P1)

**Goal**: An authorised user can call `trigger_indexing` and receive a job ID in under 3 seconds while embedding runs in the background. A second trigger for the same DocType while one is running must be rejected immediately (FR-009).

**Independent Test**: Call `trigger_indexing("Customer")` via browser console — returns `{job_id: "AI-INDX-...", status: "Queued"}` in under 3s. Call it again immediately — receives a `ValidationError`, no second job created. Check LanceDB after completion — table `v1_customer` contains one row per Customer, upserted by document ID (FR-023).

- [x] T010 [P] [US2] Create `apps/frapperag/frapperag/frapperag/doctype/ai_indexing_job/ai_indexing_job.json` — Standard DocType with naming series `AI-INDX-.YYYY.-.MM.-.DD..####`; fields: `doctype_to_index` (Data, reqd), `status` (Select: Queued/Running/Completed/Completed with Errors/Failed/Failed (Stalled), default Queued), `triggered_by` (Link → User, reqd), `progress_percent` (Percent, default 0), `start_time` (Datetime), `end_time` (Datetime), `last_progress_update` (Datetime), `total_records` (Int, default 0), `processed_records` (Int, default 0), `skipped_records` (Int, default 0), `failed_records` (Int, default 0), `tokens_used` (Int, default 0), `error_detail` (Long Text), `queue_job_id` (Data, read_only); permissions: System Manager full CRUD, RAG Admin read+write+create, RAG User read-only
- [x] T011 [P] [US2] Create `apps/frapperag/frapperag/frapperag/doctype/ai_indexing_job/ai_indexing_job.py` — stub `AIIndexingJob` controller class (no custom methods needed in Phase 1; status transitions are managed directly by `run_indexing_job`)
- [x] T012 [P] [US2] Create `apps/frapperag/frapperag/rag/lancedb_store.py` — `EMBEDDING_DIM = 768`; `_get_schema()` returns PyArrow schema with fields: `id` (string), `doctype` (string), `name` (string), `text` (string), `vector` (list<float32, 768>), `last_modified` (string); `get_store(doctype)` connects via `lancedb.connect(frappe.get_site_path("private", "files", "rag"))`, table name = `"v1_" + doctype.lower().replace(" ", "_")`, calls `db.create_table(table_name, schema=_get_schema(), exist_ok=True)` — the table is NEVER dropped or rebuilt; `upsert_vectors(doctype, rows)` calls `table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows)` — upsert by composite key `"{doctype}:{name}"` satisfies FR-023 (re-index preserves unchanged entries); all `import lancedb` and `import pyarrow` statements inside functions, never at module level
- [x] T013 [P] [US2] Create `apps/frapperag/frapperag/rag/text_converter.py` — `SUPPORTED_DOCTYPES = {"Sales Invoice", "Customer", "Item"}`; `to_text(doctype, doc)` dispatches to `_sales_invoice_text`, `_customer_text`, `_item_text`, returns `None` for unsupported types (caller counts as skipped — never as an error); text summaries are generated from a per-DocType Python template function — no LLM inference during summarisation (FR-022); `_sales_invoice_text(d)` builds sentence including name, posting_date, customer, customer_name, grand_total, currency, status, due_date, items child list (item_name × qty), outstanding_amount; `_customer_text(d)` includes customer_name, name, customer_type, customer_group, territory, email_id, outstanding_amount; `_item_text(d)` includes item_name, name, item_group, stock_uom, standard_rate, description (capped at 500 chars), is_stock_item
- [x] T014 [P] [US2] Create `apps/frapperag/frapperag/rag/embedder.py` — constants: `EMBEDDING_MODEL = "models/gemini-embedding-001"` (`text-embedding-004` unavailable on v1beta; replaced by `gemini-embedding-001`), `EMBEDDING_DIMS = 768` (passed as `output_dimensionality` to keep schema compatibility), `BATCH_SIZE = 20`, `MAX_RETRIES = 3`, `RETRY_BASE_DELAY = 2.0`, `RATE_LIMIT_SLEEP = 60.0`; `class EmbeddingError(Exception)`; `embed_texts(texts, api_key)` imports `google.generativeai` and `from google.api_core.exceptions import ResourceExhausted` inside the function, calls `genai.configure(api_key=api_key)`, loops over batches of `BATCH_SIZE`, retries up to `MAX_RETRIES`: on `ResourceExhausted` sleeps `RATE_LIMIT_SLEEP` seconds flat (no exponential back-off for rate limits), on other exceptions uses exponential back-off (`delay *= 2` starting at 2s), raises `EmbeddingError` after all retries exhausted; all heavy imports inside function, never at module level
- [x] T015 [P] [US2] Create `apps/frapperag/frapperag/rag/base_indexer.py` — `BaseIndexer(ABC)` with class attributes `name = ""`, `source_app = "frapperag"`; abstract methods: `validate_arguments(args)`, `check_permission(user)`, `execute(args)`; concrete `safe_execute(args, user)` runs validate → check_permission → execute → log_execution, re-raises `PermissionError`/`ValidationError` and calls `frappe.log_error` for unexpected exceptions; `log_execution(args, result, duration, success)` writes to `frappe.logger("frapperag").info(...)` (adapted from `frappe_assistant_core/core/base_tool.py`, MCP-specific fields removed)
- [x] T016 [US2] Create `apps/frapperag/frapperag/rag/indexer.py` — constants: `FLAT_FIELDS_BY_DOCTYPE` mapping Customer and Item to their flat field lists; `GET_DOC_DOCTYPES = {"Sales Invoice"}`; `WRITE_BATCH_SIZE = 20`; `DocIndexerTool(BaseIndexer)`: `validate_arguments` checks `is_enabled`, verifies doctype in `allowed_doctypes`, then calls `frappe.db.exists("AI Indexing Job", {"doctype_to_index": doctype, "status": ["in", ["Queued", "Running"]]})` — if any match exists, raises `frappe.ValidationError` immediately and creates no job (FR-009); `check_permission` checks user roles against `allowed_roles`; `execute` creates AI Indexing Job (status=Queued), calls `frappe.enqueue("frapperag.rag.indexer.run_indexing_job", queue="long", timeout=7200, site=frappe.local.site, indexing_job_id=..., doctype=..., user=...)` — **`job_id` must NOT be used** (reserved by Frappe/RQ as the RQ job identifier; using it silently drops the value from kwargs); **no api_key in kwargs**; `run_indexing_job(indexing_job_id, doctype, user, **kwargs)` (renamed from `job_id` for same reason) reads `api_key = frappe.get_doc("AI Assistant Settings").get_password("gemini_api_key")` as first action, calls `frappe.set_user(user)`, transitions job to Running, uses `frappe.get_doc(doctype, name).as_dict()` for DocTypes in `GET_DOC_DOCTYPES` (Sales Invoice — needs child items) and `frappe.db.get_all(doctype, fields=flat_fields)` for others (Customer, Item — no child tables), calls `frappe.has_permission` per record (skipped ≠ failed), batches text conversion + `embed_texts()` + `upsert_vectors()`, accumulates `job.tokens_used += sum(len(t) // 4 for t in pending_texts)` after each successful `embed_texts()` call (FR-021), publishes `rag_index_progress` via `frappe.publish_realtime(user=user)` after each batch and on terminal state; `mark_stalled_jobs()` queries **only** `status="Running"` jobs with `last_progress_update < now - 2h` and sets them to `Failed (Stalled)` — `Queued` jobs are explicitly excluded from this check (FR-019)
- [x] T017 [US2] Create `apps/frapperag/frapperag/api/indexer.py` with `@frappe.whitelist() trigger_indexing(doctype)` that instantiates `DocIndexerTool()` and returns `tool.safe_execute(args={"doctype": doctype, "user": frappe.session.user}, user=frappe.session.user)`

**Checkpoint**: Via browser console, call `frapperag.api.indexer.trigger_indexing` with `doctype="Customer"` — job ID returned in under 3s, background worker embeds records, AI Indexing Job transitions to Completed, LanceDB table `v1_customer` exists with rows. Call trigger again while Running — ValidationError returned, no second job created. ✅ US2 complete.

---

## Phase 5: User Story 3 — Monitor Progress (Priority: P2)

**Goal**: Admin can watch a live progress bar update as documents are indexed without refreshing the page. On job failure, error message appears and `Failed (Stalled)` is displayed for stalled workers.

**Independent Test**: Trigger Customer indexing from the RAG admin page — progress bar advances at least twice before reaching 100%, then stops updating and shows final status. Progress updates reach the screen within 10 seconds of each batch completing (SC-003).

- [x] T018 [US3] Add `get_job_status(job_id)` to `apps/frapperag/frapperag/api/indexer.py` — checks `frappe.db.exists`, calls `frappe.has_permission("AI Indexing Job", throw=True)`, returns dict with: `job_id`, `doctype_to_index`, `status`, `progress_percent`, `total_records`, `processed_records`, `skipped_records`, `failed_records`, `start_time` (str or None), `end_time` (str or None), `error_detail`
- [x] T019 [US3] Create `apps/frapperag/frapperag/frapperag/page/rag_admin/rag_admin.json` — Frappe Page definition with `name="rag-admin"`, `title="RAG Index Manager"`, `module="FrappeRAG"`, `roles=[{"role": "RAG Admin"}, {"role": "System Manager"}]`
- [x] T020 [US3] Create `apps/frapperag/frapperag/frapperag/page/rag_admin/rag_admin.js` — `frappe.pages["rag-admin"].on_page_load` handler that: (1) calls `frappe.client.get` for AI Assistant Settings and populates a `<select>` with allowed DocTypes; (2) on "Start Indexing" button click calls `frapperag.api.indexer.trigger_indexing` and stores `current_job_id`; (3) `subscribe_to_progress()` listens on `frappe.realtime.on("rag_index_progress", ...)` with `if (data.job_id !== current_job_id) return` guard, updates progress bar width and percentage text, and calls `frappe.realtime.off` on terminal status (`["Completed", "Completed with Errors", "Failed", "Failed (Stalled)"]`); (4) `frappe.realtime.on("rag_index_error", ...)` shows `frappe.msgprint` with red indicator and calls `frappe.realtime.off` on both events; (5) `update_ui(data)` sets status text, progress bar width, and counts line (Processed / Skipped / Failed / Total)

**Checkpoint**: Navigate to FrappeRAG → RAG Index Manager, select Customer, click Start Indexing — progress bar animates in real time. ✅ US3 complete.

---

## Phase 6: User Story 4 — Review Job History (Priority: P3)

**Goal**: Admin can see a paginated table of all past indexing jobs with status, record counts, and timestamps. Failed jobs show their error message and the `Failed (Stalled)` state is distinguishable from a clean `Failed`.

**Independent Test**: After running 3 jobs (Sales Invoice, Customer, Item), open the RAG admin page — all 3 appear in the Recent Jobs table with correct DocType, status, and record counts. A `Failed (Stalled)` job is visually distinct from a `Failed` job.

- [x] T021 [US4] Add `list_jobs(limit=20, page=1)` to `apps/frapperag/frapperag/api/indexer.py` — calls `frappe.has_permission("AI Indexing Job", throw=True)`, uses `frappe.db.get_all` with `order_by="creation desc"`, `limit`, `start=(page-1)*limit`, fields: name, doctype_to_index, status, progress_percent, total_records, processed_records, failed_records, triggered_by, start_time, end_time; returns `{"jobs": [...], "total": frappe.db.count("AI Indexing Job"), "page": page}`
- [x] T022 [US4] Add `load_job_list()` function to `apps/frapperag/frapperag/frapperag/page/rag_admin/rag_admin.js` — calls `frapperag.api.indexer.list_jobs` with `{limit: 10, page: 1}`, renders a Bootstrap table with columns: Job ID, DocType, Status, Records (processed/total), Started; table appended to `#rag-job-list` div; `load_job_list()` called once on page load and again after each job reaches a terminal state

**Checkpoint**: All 4 user stories functional. Run through `quickstart.md` acceptance checklist items US4-1 and US4-2. ✅ US4 complete.

---

## Final Phase: Polish & Validation

- [x] T023 Walk through all 14 items in `specs/001-rag-embedding-pipeline/quickstart.md` Acceptance Validation Checklist (US1-1 through US4-2); mark each ✅ or document the failure with steps to reproduce

---

## Dependencies & Execution Order

### Phase Dependencies

```
Phase 1 (Setup)          → no dependencies; start immediately
Phase 2 (Foundational)   → depends on Phase 1; blocks all user stories
Phase 3 (US1, P1)        → depends on Phase 2; independent of Phase 4/5/6
Phase 4 (US2, P1)        → depends on Phase 2 + Phase 3 (Settings DocType needed by indexer)
Phase 5 (US3, P2)        → depends on Phase 4 (job DocType + trigger endpoint must exist)
Phase 6 (US4, P3)        → depends on Phase 4 (list_jobs reads AI Indexing Job)
Final Phase              → depends on all phases complete
```

### Within Phase 4 (US2) — strict bottom-up order

```
T010, T011 (ai_indexing_job DocType)    ← parallel, no deps
T012, T013, T014, T015 (rag/ modules)  ← parallel, no deps on each other
T016 (indexer.py)                       ← depends on T012, T013, T014, T015
T017 (api/indexer.py trigger)           ← depends on T016
```

### Parallel Opportunities

- **Phase 1**: T001, T002, T003 — all parallel (different files)
- **Phase 3**: T006 and T007 — parallel (different child table files); T008 after T006/T007
- **Phase 4**: T010, T011, T012, T013, T014, T015 — all parallel (different files); T016 after all six; T017 after T016
- **Phase 5**: T019 after T018 exists (page routing); T020 implements the full page JS

---

## Parallel Example: Phase 4 (US2 Core)

```bash
# All six of these can be assigned simultaneously:
Task T010: Create ai_indexing_job.json (with tokens_used field, Failed (Stalled) status)
Task T011: Create ai_indexing_job.py (stub controller)
Task T012: Create rag/lancedb_store.py (v1_ prefix; merge_insert upsert; table never dropped)
Task T013: Create rag/text_converter.py (Sales Invoice / Customer / Item; no LLM inference)
Task T014: Create rag/embedder.py (ResourceExhausted → 60s flat sleep)
Task T015: Create rag/base_indexer.py (ABC lifecycle from base_tool.py pattern)

# Only after all six complete:
Task T016: Create rag/indexer.py (api_key from Settings; get_doc for Sales Invoice;
           duplicate → reject immediately; stalled detection Running-only)
Task T017: Create api/indexer.py trigger_indexing()
```

---

## Implementation Strategy

### MVP First (US1 + US2 only)

1. Complete Phase 1 (Setup) + Phase 2 (Foundational)
2. Complete Phase 3 (US1 — Settings DocType)
3. Complete Phase 4 (US2 — Indexing Job + all Python modules + trigger API)
4. **STOP and VALIDATE**: Call `trigger_indexing("Customer")` from browser console, verify LanceDB `v1_customer` rows exist; call again immediately to confirm duplicate rejection
5. Demo/deploy this MVP — embeddings generated and stored correctly in LanceDB

### Incremental Delivery

1. Phase 1–2 → Foundation
2. Phase 3 → Settings configurable (**US1 done**)
3. Phase 4 → Indexing works headlessly via API (**US2 done**)
4. Phase 5 → Admin page with live progress (**US3 done**)
5. Phase 6 → Job history visible (**US4 done**)
6. Final → Full acceptance validated

---

## Notes

- All `import lancedb`, `import pyarrow`, `import google.generativeai` MUST be inside functions — never at module level (Principle II: per-client isolation)
- LanceDB path is ALWAYS `frappe.get_site_path("private", "files", "rag")` — never hardcoded (Principle II)
- **FR-023**: LanceDB table is NEVER dropped on re-index. `merge_insert("id")` upserts by `"{doctype}:{name}"`. Deletions deferred to Phase 2.
- LanceDB table names use `"v1_"` prefix — `v1_sales_invoice`, `v1_customer`, `v1_item`
- **`api_key` is NEVER passed via `frappe.enqueue` kwargs** — read from `AI Assistant Settings` inside `run_indexing_job` (keeps key out of Redis)
- **`job_id` is RESERVED by Frappe/RQ** — passing `job_id=...` to `frappe.enqueue` sets the RQ job identifier; it is never forwarded to the function. Use `indexing_job_id=...` instead.
- `frappe.get_doc(doctype, name).as_dict()` only for Sales Invoice (child items needed); `frappe.db.get_all(fields=...)` for Customer and Item
- `tokens_used` accumulated as `sum(len(t) // 4 for t in pending_texts)` after each successful `embed_texts()` call (FR-021)
- `ResourceExhausted` → 60s flat sleep before retry; all other exceptions → 2s/4s/8s exponential back-off
- **FR-009**: Duplicate trigger MUST be rejected immediately via `frappe.db.exists` check in `validate_arguments` — no second job created, no queuing
- **FR-019**: `mark_stalled_jobs()` filters `status="Running"` ONLY — `Queued` jobs are exempt from the 2-hour stalled check
- `Failed (Stalled)` is the canonical terminal state for stalled/worker-interrupted jobs (not "Failed" or "Interrupted")
- No test files. No pytest. No test dependencies. (Principle VII)
