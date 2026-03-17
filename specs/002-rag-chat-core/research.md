# Research: RAG Chat Core — Phase 2

**Branch**: `002-rag-chat-core`
**Date**: 2026-03-16

---

## Decision 1: LanceDB Vector Search API

**Decision**: Use `table.search(vector, vector_column_name="vector").limit(k).to_list()` to retrieve top-K candidates per table. Sort results by `_distance` ascending (lower = closer).

**Rationale**: LanceDB >= 0.8.0 uses ANN (approximate nearest-neighbour) search by default. The `_distance` column is added automatically to search results. Default metric is L2; since `gemini-embedding-001` produces normalised vectors, cosine and L2 rankings are equivalent for our purposes. Explicit `.metric("cosine")` can be applied but is not required.

**Alternatives considered**:
- FTS (full-text search) — rejected; we need semantic search, not keyword match.
- Manual cosine similarity in Python — rejected; LanceDB's native search is faster and already integrated.

---

## Decision 2: Gemini 1.5 Flash Chat API (Multi-Turn)

**Decision**: Use `genai.GenerativeModel("gemini-1.5-flash").start_chat(history=[...]).send_message(text)` for multi-turn chat. History entries must be in `{"role": "user"|"model", "parts": [str]}` format. "model" is the Gemini role label for assistant turns.

**Rationale**: `start_chat()` maintains the conversation state object internally and handles the multi-turn message format expected by the Gemini API. Passing the history list up-front avoids reconstructing the conversation on each call.

**Alternatives considered**:
- `generate_content()` with a flat content list — works for single-turn but is more verbose for multi-turn; `start_chat()` is the idiomatic multi-turn interface.
- LangChain `ChatGoogleGenerativeAI` — rejected; constitution prohibits LangChain.

---

## Decision 3: System Persona Pattern for Gemini

**Decision**: Inject the system persona as the first user/model exchange in the history list (a synthetic "priming" exchange). Gemini 1.5 Flash does not have a dedicated system role in the `start_chat()` API (unlike the `system_instruction` parameter available in newer SDK versions).

**Rationale**: Using a synthetic opening exchange (`user: <persona instructions>` / `model: "Understood."`) reliably sets context in all `google-generativeai` >= 0.8.0 versions. If `system_instruction` becomes available and stable before implementation, prefer it — it's cleaner. The priming exchange approach is the safe fallback.

**Alternatives considered**:
- `GenerativeModel(system_instruction=...)` constructor parameter — available in newer SDK; if confirmed available on the target SDK version, use this instead and remove the priming exchange.

---

## Decision 4: Frappe Short Queue

**Decision**: Use `queue="short"` with `timeout=300` seconds for chat jobs.

**Rationale**: Frappe's built-in queue names are `short`, `default`, and `long`. Short queue workers are optimised for fast, bounded tasks (default timeout 300s). A single Gemini 1.5 Flash call + LanceDB search + permission filtering should complete in well under 30 seconds under normal conditions. The 300s ceiling is a generous safety net. Phase 1 indexing used `queue="long"` (7200s) because it processes thousands of documents; chat jobs process a single query.

**Alternatives considered**:
- `queue="default"` (600s timeout) — viable but short is more appropriate signalling.
- `queue="long"` — rejected; chat should not compete with long-running indexing jobs for the same worker queue.

---

## Decision 5: `permission_query_conditions` Hook

**Decision**: Implement `permission_query_conditions(user)` as a module-level function in `chat_session.py` and `chat_message.py`. Register them in `hooks.py` under `permission_query_conditions`.

**Rationale**: This is the standard Frappe mechanism for row-level filtering in list views and `frappe.db.get_all()` calls that respect permissions. Without it, `frappe.db.get_all("Chat Session", ...)` would return all sessions to all users (the DocType-level `RAG User` permission grants list access, but not row-level filtering).

For Chat Message, a subquery is used: `tabChat Message.session IN (SELECT name FROM tabChat Session WHERE owner = {user})`. This is the correct Frappe pattern for child-like records where the row ownership lives on the parent.

**Alternatives considered**:
- Manual `filters={"owner": frappe.session.user}` in every query — fragile; misses any query path that doesn't include the filter explicitly.
- Custom `has_permission` hook — works for single-record access but doesn't affect list queries.

---

## Decision 6: Citation JSON Structure

**Decision**: Citations are stored as a JSON array of `{"doctype": str, "name": str}` objects in the `Chat Message.citations` Long Text field. Example: `[{"doctype": "Customer", "name": "CUST-00001"}]`.

**Rationale**: This is the minimal structure needed to generate clickable `/app/{doctype-slug}/{name}` links in the frontend. Storing as JSON in a Long Text field avoids creating a child DocType for Phase 2 (simpler data model; no migration needed if the structure evolves).

**Alternatives considered**:
- Child DocType `Chat Message Citation` — cleaner relational model but adds DocType overhead; deferred to a future phase if query/filter needs arise.
- Embedding citations inline in the response text — rejected; citations need to be separately renderable as links.

---

## Decision 7: `frappe.router.slug()` for Citation Links

**Decision**: Use `frappe.router.slug(doctype)` in JavaScript to convert a DocType name (e.g., `"Sales Invoice"`) to the URL slug format (e.g., `"sales-invoice"`) needed for `/app/sales-invoice/SINV-0001` links.

**Rationale**: `frappe.router.slug()` is available in Frappe v15 Desk JavaScript and is the canonical utility for this conversion. Using it avoids manual string manipulation that could diverge from Frappe's own slug logic.

**Alternatives considered**:
- Manual `.toLowerCase().replace(/ /g, "-")` — works for most cases but not guaranteed to match Frappe's slugging logic for all DocType names.

---

## Decision 8: Session Title Update Timing

**Decision**: `run_chat_job()` sets `Chat Session.title` to the first 80 characters of the user's question immediately after the assistant reply is successfully inserted and committed. Only set if `session.title` is currently empty (idempotent).

**Rationale**: FR-002 requires the title to be set when the first assistant response is successfully delivered. Setting it in the background job (not in the whitelist method) ensures the title only appears after the pipeline has actually succeeded, not when the user submits a question that might fail.

**Alternatives considered**:
- Setting the title in `send_message()` at submission time — rejected per FR-002 and spec Assumptions.
- Using the assistant's response text as the title — rejected; the user's question is a more stable and predictable title source.
