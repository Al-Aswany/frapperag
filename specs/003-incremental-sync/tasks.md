# Tasks: Incremental Sync

**Input**: Design documents from `/workspace/specs/003-incremental-sync/`
**Branch**: `003-incremental-sync`
**Plan**: plan.md | **Spec**: spec.md | **Data Model**: data-model.md | **Contracts**: contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story this task belongs to (US1–US4)
- Exact file paths in every description
- No test tasks (Constitution Principle VII)

---

## Phase 1: Setup

**Purpose**: Create the new `sidecar/` package that Phase 2 builds into.

- [ ] T001 Create `apps/frapperag/frapperag/sidecar/__init__.py` (empty package file)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: DocType fixture, sidecar process, and HTTP client MUST all exist before any sync hook can enqueue work. No user story task can begin until this phase is complete.

**⚠️ CRITICAL**: Complete in order T002 → T003/T004 (parallel) → T005 → T006 → T007

- [ ] T002 Create `apps/frapperag/frapperag/frapperag/doctype/sync_event_log/` directory with `__init__.py`; create `sync_event_log.json` DocType fixture with fields: `doctype_name` (Data, reqd), `record_name` (Data, reqd), `trigger_type` (Select: Create|Update|Delete|Rename|Purge|Retry), `outcome` (Select: Queued|Running|Success|Skipped|Failed), `error_message` (Long Text); autoname `SYNC-LOG-{YYYYMMDD}-{####}`; permissions: System Manager all, RAG Admin create/read/write (no delete), RAG User none
- [ ] T003 [P] Create `apps/frapperag/frapperag/frapperag/doctype/sync_event_log/sync_event_log.py` with minimal `SyncEventLog(Document)` class body (no custom logic needed)
- [ ] T004 [P] Extend `apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.json`: add `section_sidecar` Section Break (label "RAG Sidecar"), `sidecar_port` Int field (default 8100, label "Sidecar Port"), `section_sync_health` collapsible Section Break (label "Sync Health"), `sync_health_html` HTML field (label "Sync Health"); append all four to `field_order`
- [ ] T005 Create `apps/frapperag/frapperag/sidecar/store.py`: `lancedb.connect()` once at module import targeting bench-level `rag/` directory; implement `get_or_create_table(table_name)`, `upsert_rows(table_name, rows)`, `delete_row(table_name, record_id) -> bool`, `drop_table(table_name) -> bool`; table naming: `v3_` + doctype.lower().replace(" ", "_"); record ID: `{doctype}:{name}`; schema mirrors existing `lancedb_store.py` (id, doctype, name, text, vector float32×768, last_modified) but with v3_ prefix
- [ ] T006 Create `apps/frapperag/frapperag/sidecar/main.py`: FastAPI app with lifespan that loads `multilingual-e5-base` via sentence-transformers once on startup; implement five endpoints per `contracts/sidecar-api.md`: `GET /health`, `POST /embed` (batch embed, returns vectors list), `POST /upsert` (embed one record + call store.upsert_rows), `DELETE /record/{table}/{record_id}` (call store.delete_row), `DELETE /table/{table}` (call store.drop_table); add `if __name__ == "__main__"` block with `argparse --port` and `uvicorn.run()`
- [ ] T007 Create `apps/frapperag/frapperag/rag/sidecar_client.py`: define `SidecarError(Exception)`; implement `_get_port() -> int` reading `sidecar_port` from `frappe.get_doc("AI Assistant Settings")`; implement `upsert_record(doctype, name, text, port=None)`, `delete_record(doctype, name, port=None)`, `drop_table(doctype, port=None)` — all use `httpx` with a 30s timeout, raise `SidecarError` on HTTP error or `httpx.ConnectError`; all imports inside functions (no module-level `import httpx`)

**Checkpoint**: `bench migrate` to register Sync Event Log DocType; `curl http://localhost:8100/health` to confirm sidecar starts

---

## Phase 3: User Story 1 — Index Stays Current After Document Changes (Priority: P1) 🎯 MVP

**Goal**: Every whitelisted record save or rename automatically re-indexes in the background without touching the Frappe response path.

**Independent Test**: Edit a whitelisted Customer field, save, wait for queue to drain, ask the RAG chat about that customer — the updated value appears in the answer.

- [ ] T008 Add `doc_events` dict to `apps/frapperag/frapperag/hooks.py` with `"*": {"on_update": "frapperag.rag.sync_hooks.on_document_save"}` entry (preserve existing `permission_query_conditions` and `scheduler_events` keys)
- [ ] T009 Create `apps/frapperag/frapperag/rag/sync_hooks.py`: implement `on_document_save(doc, method=None)` — whitelist check via `frappe.cache().get_doc("AI Assistant Settings")` (not `frappe.get_doc()`); if disabled or DocType not in `allowed_doctypes`, return immediately; determine `trigger_type` from `doc.is_new()` (Create vs Update); insert `Sync Event Log` entry with `outcome="Queued"` via `frappe.get_doc({...}).insert(ignore_permissions=True)`; `frappe.db.commit()`; call `frappe.enqueue("frapperag.rag.sync_runner.run_sync_job", queue="short", timeout=120, job_name=f"rag_sync_{table_key}", site=frappe.local.site, sync_log_id=log.name, doctype=doc.doctype, name=doc.name, trigger_type=trigger_type, user=frappe.session.user)`
- [ ] T010 Create `apps/frapperag/frapperag/rag/sync_runner.py`: implement `run_sync_job(sync_log_id, doctype, name, trigger_type, user, **kwargs)` — `frappe.set_user(user)`; update log entry to `outcome="Running"`; for Create/Update: `frappe.has_permission(doctype, doc=name, ptype="read", user=user)` check (→ Skipped if denied); load doc, call `to_text(doctype, doc_data)` from existing `frapperag.rag.text_converter`; call `sidecar_client.upsert_record(doctype, name, text)` catching `SidecarError`; update log entry to `outcome="Success"`, `"Skipped"`, or `"Failed"` with `error_message`; `frappe.db.commit()` after each status change; all heavy imports (`from frapperag.rag.sidecar_client import ...`) inside function body
- [ ] T011 [P] [US1] Add `"after_rename": "frapperag.rag.sync_hooks.on_document_rename"` to the `"*"` doc_events entry in `apps/frapperag/frapperag/hooks.py`; add `on_document_rename(doc, merge=False)` to `apps/frapperag/frapperag/rag/sync_hooks.py` — same cache-based whitelist check; derive old name from `doc.get_doc_before_save().name` if available else skip; create one `Sync Event Log` entry with `trigger_type="Rename"`; enqueue `run_sync_job` with `trigger_type="Rename"` passing both old and new name via kwargs; extend `run_sync_job` in `sync_runner.py` to handle Rename: `delete_record(doctype, old_name)` then `upsert_record(doctype, name, text)`

**Checkpoint**: Save a whitelisted record → a Sync Event Log entry appears with outcome Success. Rename a record → old vector entry removed, new one added.

---

## Phase 4: User Story 2 — Deleted Records Disappear From Chat Answers (Priority: P2)

**Goal**: Trashing or permanently deleting a whitelisted record removes its vector entry from the index so it never appears in chat citations.

**Independent Test**: Index a Customer, delete it in Frappe, wait for queue to drain, ask the RAG chat a question that previously matched that customer — the record does not appear.

- [ ] T012 [US2] Add `"on_trash": "frapperag.rag.sync_hooks.on_document_trash"` to the `"*"` doc_events entry in `apps/frapperag/frapperag/hooks.py`
- [ ] T013 [US2] Add `on_document_trash(doc, method=None)` to `apps/frapperag/frapperag/rag/sync_hooks.py`: same `frappe.cache().get_doc("AI Assistant Settings")` whitelist check; insert Sync Event Log entry with `trigger_type="Delete"`, `outcome="Queued"`; enqueue `run_sync_job` with `trigger_type="Delete"`, `queue="short"`, `timeout=60`
- [ ] T014 [US2] Extend `run_sync_job` in `apps/frapperag/frapperag/rag/sync_runner.py` to handle `trigger_type="Delete"`: call `sidecar_client.delete_record(doctype, name)` (no permission check needed — deletion is already authorised by Frappe's own event); catch `SidecarError` and record failure; update log outcome to Success or Failed

**Checkpoint**: Trash a whitelisted record → Sync Event Log shows Delete/Success. Query the sidecar to confirm the record ID is absent from the v3_ table.

---

## Phase 5: User Story 3 — Whitelist Changes Are Reflected in the Index (Priority: P3)

**Goal**: Removing a DocType from the AI Assistant Settings whitelist purges all its vector entries so the assistant can never retrieve or cite those records again.

**Independent Test**: Index Item records, remove "Item" from the whitelist and save AI Assistant Settings, wait for purge job to complete, ask a question that matches Item content — zero Item citations appear.

- [ ] T015 [US3] Extend `apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.py`: add `on_update(self)` method — call `old = self.get_doc_before_save()` to retrieve pre-save state (no `before_save` hook needed); compute `old_allowed = {r.doctype_name for r in old.allowed_doctypes} if old else set()`; compute `new_allowed = {r.doctype_name for r in self.allowed_doctypes}`; for each DocType in `old_allowed - new_allowed`: insert Sync Event Log entry with `trigger_type="Purge"`, `outcome="Queued"`, `record_name="*"`; `frappe.db.commit()`; enqueue `run_purge_job` with `job_name=f"rag_purge_{dt.lower().replace(' ','_')}"`, `queue="short"`, `timeout=120`
- [ ] T016 [US3] Add `run_purge_job(sync_log_id, doctype, user, **kwargs)` to `apps/frapperag/frapperag/rag/sync_runner.py`: `frappe.set_user(user)`; update log to `outcome="Running"`; call `sidecar_client.drop_table(doctype)` catching `SidecarError`; update log to Success or Failed with `error_message`; `frappe.db.commit()`; all imports inside function body

**Checkpoint**: Remove a DocType from the whitelist, save settings → Sync Event Log shows Purge/Success. Confirm the `v3_{doctype}` table no longer exists in LanceDB.

---

## Phase 6: User Story 4 — Admins Can Monitor Sync Health (Priority: P4)

**Goal**: Administrators can view per-DocType sync success/failure counts and retry failed sync jobs directly from AI Assistant Settings.

**Independent Test**: Cause a sync failure (stop the sidecar mid-job), open AI Assistant Settings — the failed entry appears in the Sync Health panel with a Retry button; click Retry — a new Sync Event Log entry is created and the job re-runs.

- [ ] T017 [P] [US4] Add `get_sync_health()` to `apps/frapperag/frapperag/api/indexer.py`: `@frappe.whitelist()`; verify caller has RAG Admin or System Manager role; compute 24h cutoff; query `Sync Event Log` grouped by `doctype_name` + `outcome` within cutoff for summary counts and last-success timestamp; query `Sync Event Log` for `outcome="Failed"` (all time, limit 100, order by `creation desc`) for failure list; return `{"summary": [...], "failures": [...]}` matching contract in `contracts/api-contracts.md`
- [ ] T018 [P] [US4] Add `retry_sync(sync_log_id)` to `apps/frapperag/frapperag/api/indexer.py`: `@frappe.whitelist()`; verify role; load original entry and assert `outcome="Failed"` (throw `ValidationError` otherwise); create new Sync Event Log entry with same `doctype_name`, `record_name`, `trigger_type="Retry"`, `outcome="Queued"`; `frappe.db.commit()`; enqueue `run_sync_job` (for non-Purge original) or `run_purge_job` (for Purge original) on `queue="short"`; return `{"sync_log_id": new_entry.name, "status": "Queued"}`
- [ ] T019 [US4] Create `apps/frapperag/frapperag/frapperag/doctype/ai_assistant_settings/ai_assistant_settings.js`: on `refresh` event call `frappe.call("frapperag.api.indexer.get_sync_health")` and render result into the `sync_health_html` field — render a table of per-DocType success/fail counts and a failure list where each row has a Retry button calling `frappe.call("frapperag.api.indexer.retry_sync", {sync_log_id: "..."})` then re-rendering the panel; use `frappe.call(...)` exclusively (no `fetch`); guard against missing `sync_health_html` field gracefully
- [ ] T020 [US4] Add `mark_stalled_sync_jobs()` to `apps/frapperag/frapperag/rag/sync_runner.py`: query `Sync Event Log` for `outcome="Running"` and `modified < (now - 10 minutes)`; `frappe.db.set_value(...)` to `outcome="Failed"`, `error_message="Stalled: no update for >10 minutes"` for each; `frappe.db.commit()` if any updated
- [ ] T021 [US4] Add `prune_sync_event_log()` to `apps/frapperag/frapperag/rag/sync_runner.py`: compute cutoff as `add_to_date(now_datetime(), days=-30)`; `frappe.db.delete("Sync Event Log", {"creation": ["<", cutoff]})`; `frappe.db.commit()`

**Checkpoint**: After a sync failure, admin sees it in the panel and can click Retry to queue a new attempt. Original Failed entry persists.

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Wire the scheduler cron entries, ensure the sidecar starts with the bench, and confirm requirements are complete.

- [ ] T022 Extend `scheduler_events` in `apps/frapperag/frapperag/hooks.py`: add `"frapperag.rag.sync_runner.mark_stalled_sync_jobs"` to the existing `"*/5 * * * *"` cron list; add a new `"daily"` list containing `"frapperag.rag.sync_runner.prune_sync_event_log"`
- [ ] T023 Update `apps/frapperag/frapperag/setup/install.py` `after_install()`: detect whether a `rag_sidecar:` line already exists in the bench Procfile; if not, append `rag_sidecar: {bench_path}/env/bin/python {app_path}/frapperag/sidecar/main.py --port 8100` (derive paths from `frappe.utils.get_bench_path()`); log a message instructing the admin to run `bench start` to launch the sidecar
- [ ] T024 [P] Verify `apps/frapperag/requirements.txt` contains all Phase 3 dependencies: `sentence-transformers>=2.7.0`, `fastapi>=0.110.0`, `uvicorn>=0.29.0`, `httpx>=0.27.0`, `lancedb>=0.8.0`, `pyarrow`; add any that are missing

**Final checkpoint**: `bench migrate` → no errors. `bench start` → sidecar logs "Startup complete". Edit a whitelisted record → Sync Event Log Success entry within seconds. Remove a DocType from whitelist → Purge/Success entry. Admin panel shows health summary.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — **BLOCKS all user story phases**
- **US1 (Phase 3)**: Depends on Phase 2
- **US2 (Phase 4)**: Depends on Phase 2; US1 does NOT need to be complete (different trigger path)
- **US3 (Phase 5)**: Depends on Phase 2; US1 and US2 do NOT need to be complete
- **US4 (Phase 6)**: Depends on Phase 2; best started after US1/US2/US3 so there is data to query
- **Polish (Phase 7)**: Depends on all user story phases

### Within-Phase Dependencies (critical ordering)

```
Phase 2:  T002 → T003/T004 (parallel) → T005 → T006 → T007
Phase 3:  T008 → T009 → T010 → T011
Phase 4:  T012 → T013 → T014
Phase 5:  T015 → T016
Phase 6:  T017/T018 (parallel) → T019 → T020/T021 (parallel)
Phase 7:  T022 → T023 → T024
```

### Intra-file modification order (across phases)

`hooks.py` is touched in T008, T011, T012, T022 — edit sequentially in that order to avoid conflicts.
`sync_runner.py` is grown incrementally: created in T010, extended in T014, T016, T020, T021.
`sync_hooks.py` is grown incrementally: created in T009, extended in T011, T013.
`api/indexer.py` is extended in T017 and T018 (parallel — different functions).

### Parallel Opportunities

Within Phase 2: T003 and T004 touch different files — run in parallel.
Within Phase 6: T017 and T018 add different functions to the same file — serialize writes but the research is parallel.
T020 and T021 add different functions to `sync_runner.py` — serialize writes.

---

## Parallel Example: Phase 2 (Foundational)

```
# Run simultaneously (different files):
Task T003: "Create sync_event_log.py in doctype/sync_event_log/"
Task T004: "Extend ai_assistant_settings.json with sidecar_port and sync_health_html fields"

# After T003/T004 complete, run sequentially:
Task T005: "Create sidecar/store.py"        → must complete before T006
Task T006: "Create sidecar/main.py"         → must complete before T007
Task T007: "Create rag/sidecar_client.py"
```

---

## Implementation Strategy

### MVP: User Story 1 Only

1. Complete Phase 1 (Setup) — 1 task
2. Complete Phase 2 (Foundational) — 6 tasks, ~30 min
3. Complete Phase 3 (US1) — 4 tasks
4. **STOP and VALIDATE**: edit a whitelisted record, confirm Sync Event Log Success entry, confirm chat answer reflects the change
5. Ship US1 increment

### Incremental Delivery

1. Phase 1 + Phase 2 → foundation ready
2. Phase 3 (US1) → auto-sync on save, rename ✅ — demo to stakeholders
3. Phase 4 (US2) → auto-delete on trash ✅
4. Phase 5 (US3) → whitelist purge ✅
5. Phase 6 (US4) → admin health panel + retry ✅
6. Phase 7 (Polish) → scheduler, Procfile, requirements ✅

Each step is independently demoable without breaking the prior increment.
