# Tasks: RAG Chat Core — Phase 2

**Input**: Design documents from `/specs/002-rag-chat-core/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, quickstart.md

**Tests**: No automated tests per Principle VII (No Automated Tests). Manual acceptance validation via `quickstart.md`.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3)
- All paths are relative to the repository root

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create new DocType directory stubs for Phase 2 entities. App base structure (`hooks.py`, `rag/`, `api/`) already exists from Phase 1.

- [ ] T001 Create `apps/frapperag/frapperag/frapperag/doctype/chat_session/__init__.py` as an empty Python file (new DocType directory)
- [ ] T002 [P] Create `apps/frapperag/frapperag/frapperag/doctype/chat_message/__init__.py` as an empty Python file (new DocType directory)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Define Chat Session and Chat Message DocTypes with row-level permission hooks. Required before any API or UI work can function.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete — `bench migrate` must succeed with both new DocTypes visible.

- [ ] T003 [P] Create Chat Session DocType JSON at `apps/frapperag/frapperag/frapperag/doctype/chat_session/chat_session.json` — Standard DocType, module FrappeRAG, naming series `RAG-SESS-.YYYY.-.MM.-.DD.-####`, Is Submittable: No, Track Changes: No; fields: `title` (Data), `status` (Select: Open\nArchived, default Open); permissions: System Manager (CRUD), RAG Admin (CRU), RAG User (CRU)
- [ ] T004 [P] Create Chat Message DocType JSON at `apps/frapperag/frapperag/frapperag/doctype/chat_message/chat_message.json` — Standard DocType, module FrappeRAG, naming series `RAG-MSG-.YYYY.-.MM.-.DD.-####`, Is Submittable: No, Track Changes: No; fields: `session` (Link→Chat Session, required), `role` (Select: user\nassistant, required), `content` (Long Text, required), Section Break "Processing", `status` (Select: Pending\nCompleted\nFailed, default Pending), `tokens_used` (Int, default 0), Section Break "Citations", `citations` (Long Text), Section Break "Error Detail" (collapsible), `error_detail` (Long Text); permissions: System Manager (CRUD), RAG Admin (CRU), RAG User (CRU)
- [ ] T005 [P] Create `apps/frapperag/frapperag/frapperag/doctype/chat_session/chat_session.py` — implement `permission_query_conditions(user)`: if user not set use `frappe.session.user`; return `""` for System Manager; return `` `tabChat Session`.`owner` = {frappe.db.escape(user)} `` for all other roles
- [ ] T006 [P] Create `apps/frapperag/frapperag/frapperag/doctype/chat_message/chat_message.py` — implement `permission_query_conditions(user)`: if user not set use `frappe.session.user`; return `""` for System Manager; return subquery `` `tabChat Message`.`session` IN (SELECT `name` FROM `tabChat Session` WHERE `owner` = {escaped}) `` for all other roles
- [ ] T007 Update `apps/frapperag/frapperag/hooks.py` to: (a) append `"frapperag.rag.chat_runner.mark_stalled_chat_messages"` to the existing `*/5 * * * *` cron list alongside the Phase 1 `mark_stalled_jobs` entry — **note**: change the Phase 1 cron key from `*/30 * * * *` to `*/5 * * * *` so both stalled-job cleaners share the faster schedule (SC-005 requires within-15-min recovery: 10-min cutoff + 5-min poll); (b) add or extend `permission_query_conditions` dict with `"Chat Session"` → `"frapperag.frapperag.doctype.chat_session.chat_session.permission_query_conditions"` and `"Chat Message"` → `"frapperag.frapperag.doctype.chat_message.chat_message.permission_query_conditions"`

**Checkpoint**: Run `bench --site <site> migrate` — both DocTypes must appear in the database. `frappe.get_doc({"doctype": "Chat Session"})` and `frappe.get_doc({"doctype": "Chat Message"})` must not raise import/schema errors.

---

## Phase 3: User Story 1 — Ask a Business Question (Priority: P1) 🎯 MVP

**Goal**: A user can submit one natural-language question and receive a permission-filtered AI response with clickable citations, delivered in real-time without page refresh.

**Independent Test**: Open `/app/rag-chat`, click **New Chat**, type a question on a site with at least one indexed LanceDB table, press Enter. Verify: input locks immediately, "Thinking…" bubble appears, response arrives within ~30s with ≥1 clickable citation, input re-enables. Acceptance Test 1 in `quickstart.md`.

### Implementation for User Story 1

- [ ] T008 [P] [US1] Create `apps/frapperag/frapperag/rag/retriever.py` — constants `EMBEDDING_MODEL = "models/gemini-embedding-001"`, `EMBEDDING_DIMS = 768`, `TOP_K = 5`, `RATE_LIMIT_SLEEP = 60.0`, `MAX_RETRIES = 3`, `RETRY_BASE_DELAY = 2.0`; implement `embed_query(text, api_key)` (all imports inside function; configure genai; retry loop: ResourceExhausted → 60s sleep → retry, other exceptions → exponential back-off → retry, raise RuntimeError after MAX_RETRIES); implement `search_all_tables(query_vector)` (import lancedb inside function; open DB via `frappe.get_site_path("private","files","rag")`; filter `db.table_names()` to `v1_*`; return `[]` if none; search each table with `.search(query_vector, vector_column_name="vector").limit(TOP_K).to_list()`; collect `{doctype, name, text, _distance}` dicts; sort ascending by `_distance`; return); implement `filter_by_permission(candidates, user)` (call `frappe.has_permission(doctype, doc=name, ptype="read", user=user)` per candidate; return only allowed records)
- [ ] T009 [P] [US1] Create `apps/frapperag/frapperag/rag/prompt_builder.py` — define `SYSTEM_PERSONA` constant (business assistant, answer from context only, don't fabricate, mention sources by type/ID) and `EMPTY_CONTEXT_NOTE` constant (no accessible context found, user may lack permission or no data indexed); implement `build_messages(question, context_records, history)`: start with synthetic priming exchange `[{"role":"user","parts":[SYSTEM_PERSONA]}, {"role":"model","parts":["Understood. I will answer based only on provided context."]}]`; append `history[-10:]` mapping role "assistant"→"model"; if `context_records` non-empty build context block as `[{doctype} / {name}]\n{text}` joined by `\n\n` and append final user turn `"Context from ERP data:\n{context_text}\n\nQuestion: {question}"`; else append `"{EMPTY_CONTEXT_NOTE}\n\nQuestion: {question}"`; return messages list
- [ ] T010 [P] [US1] Create `apps/frapperag/frapperag/rag/chat_engine.py` — constants `CHAT_MODEL = "gemini-2.5-flash"`, `RATE_LIMIT_SLEEP = 60.0`; implement `generate_response(messages, context_records, api_key)` (all imports inside function; configure genai; build `GenerativeModel(CHAT_MODEL)`; split messages: `history = messages[:-1]`, `last_message = messages[-1]["parts"][0]`; `chat = model.start_chat(history=history)`; attempt `chat.send_message(last_message)` with one retry on ResourceExhausted after 60s sleep — all other exceptions propagate immediately; extract `response.text`; extract `tokens_used` from `response.usage_metadata.total_token_count` if available; build deduplicated citations list `[{"doctype": r["doctype"], "name": r["name"]}]` from context_records using a seen set; return `{"text": text, "citations": citations, "tokens_used": tokens_used}`)
- [ ] T011 [US1] Create `apps/frapperag/frapperag/rag/chat_runner.py` — implement `run_chat_job(message_id, session_id, user, **kwargs)`: import retriever/prompt_builder/chat_engine at top of function; read `api_key = frappe.get_doc("AI Assistant Settings").get_password("gemini_api_key")`; call `frappe.set_user(user)`; in try block: get Chat Message doc, extract question; call `embed_query(question, api_key)` → `search_all_tables(query_vector)` → `filter_by_permission(candidates, user)`; build `history=[]` (placeholder — US2 will add history loading); call `build_messages(question, filtered, history)` → `generate_response(messages, filtered, api_key)`; `frappe.db.set_value("Chat Message", message_id, {"status":"Completed"})`; commit; insert assistant reply doc (role="assistant", content=result["text"], citations=json.dumps(result["citations"]), status="Completed", tokens_used=result["tokens_used"]); commit; if session title blank set it to `question[:80].strip()`; commit; publish `rag_chat_response` realtime event to user with message_id, session_id, status, content, citations, tokens_used; in except block: format traceback, set message Failed+error_detail[:2000], commit, publish Failed realtime event, call `frappe.log_error`; implement `mark_stalled_chat_messages()`: compute `cutoff = add_to_date(now_datetime(), minutes=-10)`; get all Pending Chat Messages with creation < cutoff; set each to Failed with explanatory error_detail; commit if any found
- [ ] T012 [US1] Create `apps/frapperag/frapperag/api/chat.py` — implement `_assert_session_owner(session_id)` helper (throw DoesNotExistError if not exists, throw PermissionError if session.owner != frappe.session.user, return session doc); `@frappe.whitelist() create_session()` (insert Open Chat Session, commit, return `{"session_id": session.name}`); `@frappe.whitelist() send_message(session_id, content)` (assert owner; if `content` is empty or whitespace-only throw ValidationError "Message content cannot be empty." before any DB write; check `frappe.db.exists("Chat Message", {"session":session_id,"status":"Pending"})` and throw ValidationError if found; insert Pending Chat Message; commit; enqueue `"frapperag.rag.chat_runner.run_chat_job"` with `queue="short"`, `timeout=300`, `site=frappe.local.site`, `message_id=msg.name`, `session_id=session_id`, `user=frappe.session.user`; return `{"message_id": msg.name, "status":"Pending"}`); `@frappe.whitelist() get_messages(session_id)` (assert owner; get_all Chat Messages filtered by session, fields name/role/content/citations/status/tokens_used/creation, order by creation asc; return `{"messages": [...]}` with `message_id=m.name` alias); `@frappe.whitelist() list_sessions(include_archived=0)` (build filters `{"owner": frappe.session.user}`, add `"status":"Open"` unless `int(include_archived)` is truthy; `frappe.db.get_all("Chat Session", filters=filters, fields=["name","title","status","creation"], order_by="creation desc", ignore_permissions=False)`; return `{"sessions": [dict(s, session_id=s.name) for s in sessions]}`)
- [ ] T013 [P] [US1] Create `apps/frapperag/frapperag/page/rag_chat/rag_chat.json` — page definition with `name: "rag-chat"`, `title: "AI Assistant"`, `module: "FrappeRAG"`, `standard: "Yes"`, roles array containing RAG User, RAG Admin, System Manager
- [ ] T014 [US1] Create `apps/frapperag/frapperag/page/rag_chat/rag_chat.js` — implement `frappe.pages["rag-chat"].on_page_load` with: (a) `frappe.ui.make_app_page` with title "AI Assistant", single_column; (b) HTML layout appended to `page.main`: outer flex div `rag-chat-layout` (height calc(100vh-100px)); left panel 260px `rag-sessions` with `#rag-new-session` button + `#rag-session-list` div; right panel flex column `rag-thread` with `#rag-messages` flex-1 scrollable + input bar containing `#rag-input` (disabled) + `#rag-send` button (disabled); (c) state vars `current_session_id = null`, `current_message_id = null`; (d) `render_message(m, $container)` building user (right, blue #5e64ff) and assistant (left, gray #f5f5f5) bubbles with `frappe.utils.escape_html`, status note for Pending/Failed, citation links via `frappe.router.slug(c.doctype)` → `/app/{slug}/{name}` target _blank; (e) `set_input_locked(locked)` toggling `#rag-input, #rag-send` disabled; (f) `load_messages(session_id)` calling `get_messages`, rendering all messages, scrolling to bottom, detecting any Pending message to re-lock input and call `subscribe_realtime(pending.message_id)`; (g) `load_sessions()` calling `frapperag.api.chat.list_sessions` with `include_archived:0`, emptying `#rag-session-list`, and rendering each session as a `.rag-session-item` div (`data-id`, active background highlight `background:#f0f4ff` when `s.session_id === current_session_id`, title truncated with `text-overflow:ellipsis max-width:180px`) containing an `⋯` `.rag-archive-btn` button (`data-id`, title="Archive"); session click and archive button event handlers are added in T017 (US3); (h) New Chat button click: `frappe.call create_session`, set current_session_id, reset current_message_id, `frappe.realtime.off("rag_chat_response")`, clear `#rag-messages`, `set_input_locked(false)`, call `load_sessions()`, focus input; (i) `send_message()`: get trimmed input value, return if empty or no session, `set_input_locked(true)`, clear input, render optimistic user bubble, append `.rag-pending-bubble` thinking div, scroll to bottom, `frappe.call send_message`, in callback set `current_message_id = r.message.message_id` and call `subscribe_realtime`, in error remove pending bubble and unlock; wire `#rag-send` click and `#rag-input` keydown Enter (no Shift) to `send_message()`; (j) `subscribe_realtime(message_id)`: `frappe.realtime.off("rag_chat_response")` then `.on(...)` — guard `if (data.message_id !== message_id) return`; remove `.rag-pending-bubble`; on Completed render assistant bubble with citation links; on Failed show `frappe.msgprint` error; scroll to bottom, `set_input_locked(false)`, `frappe.realtime.off("rag_chat_response")`, `current_message_id = null`, call `load_sessions()`; (k) call `load_sessions()` at page init

**Checkpoint**: User Story 1 fully functional — one complete Q&A cycle with real-time response and clickable citations, including session list populated (AT-1 step 3) and session title visible in sidebar after first response (AT-1 step 9). Verify with quickstart.md Acceptance Test 1 (all 10 steps must pass) and Acceptance Test 2 (no indexed data friendly message).

---

## Phase 4: User Story 2 — Continue a Multi-Turn Conversation (Priority: P2)

**Goal**: Follow-up questions that use pronouns or implicit references to entities from prior turns are answered correctly using the last 10 conversation turns as context.

**Independent Test**: In an existing session, ask "Who is our top customer?", wait for response naming a customer, then ask "What is their outstanding balance?" — verify the AI resolves "their" from conversation history without the user repeating the customer name. Acceptance Test 4 in `quickstart.md`.

### Implementation for User Story 2

- [ ] T015 [US2] Update `apps/frapperag/frapperag/rag/chat_runner.py` — replace the `history=[]` placeholder in `run_chat_job()` with actual history loading: after getting `question` from the message doc, add step: `history_docs = frappe.db.get_all("Chat Message", filters={"session": session_id, "name": ["!=", message_id]}, fields=["role", "content"], order_by="creation desc", limit=10, ignore_permissions=False)`; `history = [{"role": d.role, "content": d.content} for d in reversed(history_docs)]`; pass this `history` to `build_messages(question, filtered, history)`

**Checkpoint**: User Story 2 functional. Follow-up questions resolve context from prior turns. Verify with quickstart.md Acceptance Test 4.

---

## Phase 5: User Story 3 — Manage Chat Sessions (Priority: P3)

**Goal**: Users see all their sessions in a sidebar, can switch between them, and archive sessions. Other users' sessions are never visible or accessible.

**Independent Test**: Create sessions under two user accounts; verify each user sees only their own sessions in the sidebar. Verify archive removes session from the Open list. Attempt direct API call to another user's session — verify PermissionError with no message content revealed. Acceptance Tests 5 and 7 in `quickstart.md`.

### Implementation for User Story 3

- [ ] T016 [US3] Add `archive_session` endpoint to `apps/frapperag/frapperag/api/chat.py` — `@frappe.whitelist() archive_session(session_id)`: `_assert_session_owner(session_id)`; `frappe.db.set_value("Chat Session", session_id, "status", "Archived")`; commit; return `{"session_id": session_id, "status": "Archived"}` (note: `list_sessions` is already implemented in T012)
- [ ] T017 [US3] Update `apps/frapperag/frapperag/page/rag_chat/rag_chat.js` — add session interaction event handlers (note: `load_sessions()` rendering is already implemented in T014; this task adds only the click-level interactivity): `$(document).on("click", ".rag-session-item", ...)` handler: skip if click target is `.rag-archive-btn`; if `sid === current_session_id` return; set `current_session_id = sid`, `current_message_id = null`, `frappe.realtime.off("rag_chat_response")`, call `load_sessions()` (re-renders sidebar to update active highlight), call `load_messages(sid)`; `$(document).on("click", ".rag-archive-btn", ...)` handler: `e.stopPropagation()`; `frappe.confirm("Archive this chat session?", function()` → `frappe.call archive_session` → on callback if archived session was `current_session_id` reset state (`current_session_id = null`, `current_message_id = null`, clear `#rag-messages`, `set_input_locked(true)`) then call `load_sessions()`

**Checkpoint**: All three user stories functional. Verify with quickstart.md Acceptance Test 5 (session ownership isolation) and Acceptance Test 7 (archive session).

---

## Phase 6: Polish & Validation

**Purpose**: End-to-end manual acceptance testing and edge-case verification per `quickstart.md`.

- [ ] T018 Run `bench --site <site> migrate` to create Chat Session + Chat Message tables, start a short-queue worker (`bench worker --queue short`), execute quickstart.md Acceptance Test 1 (first chat message — submit question, verify realtime response with citations, session title update) and Acceptance Test 2 (no indexed data — verify friendly message, no exception)
- [ ] T019 [P] Execute quickstart.md Acceptance Test 3 (permission filtering — verify restricted customer information not revealed in response or citations, SC-002) and Acceptance Test 5 (session ownership isolation — User B cannot see User A's sessions; direct API call to User A's session returns PermissionError with no message content, SC-006)
- [ ] T020 [P] Execute quickstart.md Acceptance Test 6 (input locking — second message cannot be submitted while Pending, FR-019), Acceptance Test 7 (archive session — session disappears from Open list, messages still accessible), Acceptance Test 8 (stalled message recovery — manually run `mark_stalled_chat_messages()` after 11+ minutes, verify Pending → Failed), and Acceptance Test 9 (invalid API key — message marked Failed immediately, no 60s wait, error detail written to record)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 completion — BLOCKS all user stories (`bench migrate` required)
- **User Story Phases (3–5)**: All depend on Foundational phase completion
  - Phase 3 (US1) can start immediately after Phase 2
  - Phase 4 (US2) depends on Phase 3 (modifies `chat_runner.py` created in T011)
  - Phase 5 (US3) depends on Phase 3 (extends `api/chat.py` from T012 and `rag_chat.js` from T014)
  - Phase 4 and Phase 5 can be worked in parallel by different developers once Phase 3 is complete
- **Polish (Phase 6)**: Depends on all user story phases

### User Story Dependencies

- **User Story 1 (P1)**: Depends only on Foundational phase — no dependencies on US2 or US3
- **User Story 2 (P2)**: Depends on US1 (modifies T011 `chat_runner.py`); independent of US3
- **User Story 3 (P3)**: Depends on US1 (extends T012 `api/chat.py` and T014 `rag_chat.js`); independent of US2

### Within Each User Story (Phase 3)

- T008, T009, T010 are independent modules — implement in parallel
- T011 (`chat_runner.py`) depends on T008, T009, T010 being complete
- T012 (`api/chat.py`) depends on T011 (calls `run_chat_job`)
- T013 (`rag_chat.json`) is independent — can run alongside any Phase 3 task
- T014 (`rag_chat.js`) depends on T012 (requires API method names and signatures)

### Parallel Opportunities

- T001 and T002 can run in parallel (different files)
- T003, T004, T005, T006 can all run in parallel (four different files)
- T008, T009, T010, T013 can all run in parallel (four different files)
- T016 and T017 are sequential (T017 uses `archive_session` added in T016)
- T019 and T020 can run in parallel (independent test scenarios)

---

## Parallel Example: User Story 1

```bash
# Phase 3 parallel block (all independent files):
Task: "Create rag/retriever.py"            # T008
Task: "Create rag/prompt_builder.py"       # T009
Task: "Create rag/chat_engine.py"          # T010
Task: "Create page/rag_chat/rag_chat.json" # T013

# Then sequentially (each depends on prior):
Task: "Create rag/chat_runner.py"          # T011 (depends on T008–T010)
Task: "Create api/chat.py (3 endpoints)"   # T012 (depends on T011)
Task: "Create page/rag_chat/rag_chat.js"   # T014 (depends on T012)
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001–T002)
2. Complete Phase 2: Foundational (T003–T007), run `bench migrate`
3. Complete Phase 3: User Story 1 (T008–T014)
4. **STOP and VALIDATE**: Run quickstart.md Acceptance Test 1
5. Deploy/demo — single Q&A cycle with citations is the core value proposition

### Incremental Delivery

1. Setup + Foundational → DocTypes ready, migration passed
2. User Story 1 (P1) → Working Q&A with realtime response and citations → Deploy/demo (MVP)
3. User Story 2 (P2) → Multi-turn context — one file update, high impact
4. User Story 3 (P3) → Full session management, archive, ownership isolation
5. Polish → All 9 acceptance tests in `quickstart.md` verified

### Parallel Team Strategy (2 developers after Phase 3)

- Developer A: User Story 2 (T015 — single update to `chat_runner.py`)
- Developer B: User Story 3 (T016–T017 — new API endpoints + JS session list)
- No file conflicts: T015 modifies `chat_runner.py`; T016 modifies `api/chat.py`; T017 modifies `rag_chat.js`

---

## Notes

- **No automated tests** per Principle VII — manual acceptance testing only via `quickstart.md`
- All heavy imports (`lancedb`, `google.generativeai`) MUST be inside functions — never at module level
- `api_key` is NEVER passed via `frappe.enqueue` kwargs — always read from AI Assistant Settings at job start
- Use `message_id` kwarg (not `job_id`) in `frappe.enqueue` to avoid Frappe/RQ reserved-name collision
- All LanceDB paths via `frappe.get_site_path("private", "files", "rag")` — never hardcoded
- `frappe.set_user(user)` MUST be the first substantive line in `run_chat_job()` (after imports and api_key read)
- `[P]` tasks touch different files with no cross-task dependencies — safe to implement simultaneously
- Both `bench worker --queue short` AND `bench worker --queue long` must be running for full functionality
