# Quickstart: RAG Chat Core — Phase 2

**Branch**: `002-rag-chat-core`
**Date**: 2026-03-16
**Prerequisites**: Phase 1 (`001-rag-embedding-pipeline`) installed and at least one DocType indexed.

---

## Setup Checklist

Before testing Phase 2, verify Phase 1 is working:

```bash
# Inside bench console — confirm at least one v1_* table exists
bench --site <site> console
>>> import lancedb, frappe
>>> db = lancedb.connect(frappe.get_site_path("private", "files", "rag"))
>>> db.table_names()      # must show ['v1_customer'] or similar
>>> db.open_table("v1_customer").count_rows()   # must be > 0
```

Then install Phase 2 and start a short-queue worker:

```bash
bench --site <site> migrate          # creates Chat Session + Chat Message tables
bench worker --queue short &         # required for chat background jobs
bench restart
```

---

## Acceptance Test 1 — First Chat Message (P1 core flow)

**Covers**: FR-001, FR-004, FR-005, FR-006, FR-008, FR-009, FR-014, SC-001, SC-003

1. Log in as a user with the `RAG User` role.
2. Navigate to `/app/rag-chat`.
3. Click **New Chat** — the session list should update with an untitled entry.
4. In the input bar, type: `Tell me about our customers` and press Enter.
5. **Expected**: Input immediately disables (locked); a "Thinking…" bubble appears.
6. **Expected**: Within ~30 seconds, the thinking bubble is replaced by an assistant response describing indexed customers.
7. **Expected**: One or more clickable citation links appear below the response (e.g., `Customer: CUST-00001`).
8. **Expected**: Clicking a citation navigates to the Frappe record page for that customer.
9. **Expected**: The session title in the sidebar updates to the first 80 characters of the question.
10. **Expected**: Input re-enables after the response appears.

---

## Acceptance Test 2 — No Indexed Data (edge case)

**Covers**: FR-011, SC-001

1. On a fresh site with no indexed LanceDB tables (or after deleting the `private/files/rag/` directory).
2. Submit any question.
3. **Expected**: The assistant responds with a friendly message indicating no data is available — no exception, no blank state, no traceback shown to user.

---

## Acceptance Test 3 — Permission Filtering (SC-002)

**Covers**: FR-006, FR-012, SC-002

1. Create a Customer record that `RAG User A` does NOT have read access to (e.g., set row-level permissions to a different role).
2. Re-index Customers so the restricted customer is in the vector store.
3. Log in as `RAG User A` and ask: `Tell me about [restricted customer name]`.
4. **Expected**: The response does not reveal any information about the restricted customer — it either says context is insufficient or discusses only accessible customers.

---

## Acceptance Test 4 — Multi-Turn Context (P2 story)

**Covers**: FR-007, SC-004

1. In a session, ask: `Who is our top customer?`
2. Wait for the assistant response mentioning a specific customer by name.
3. Follow up: `What is their outstanding balance?`
4. **Expected**: The assistant correctly understands "their" refers to the customer mentioned in the prior turn and provides relevant information — without the user re-stating the customer name.

---

## Acceptance Test 5 — Session Ownership Isolation (SC-006)

**Covers**: FR-013, SC-006

1. Log in as User A, create a session, send a message.
2. Note the session ID from the URL or browser dev tools.
3. Log out. Log in as User B.
4. Navigate to `/app/rag-chat` — **Expected**: User B sees no sessions belonging to User A.
5. In browser console, run:
   ```javascript
   frappe.call({method: "frapperag.api.chat.get_messages", args: {session_id: "<User A session ID>"}})
   ```
6. **Expected**: `PermissionError` — no message content returned.

---

## Acceptance Test 6 — Input Locking (clarification Q1)

**Covers**: FR-019

1. Submit a question and immediately try to type and send another question before the response arrives.
2. **Expected**: The input field and Send button are disabled — the second message cannot be sent.
3. **Expected**: After the response arrives, input re-enables automatically.

---

## Acceptance Test 7 — Archive Session (FR-020)

**Covers**: FR-020

1. Create two sessions with at least one message each.
2. In the session list, click the Archive (⋯) button on Session 1.
3. Confirm the dialog.
4. **Expected**: Session 1 disappears from the default session list (which shows Open sessions only).
5. **Expected**: Session 1's messages remain accessible: call `list_sessions(include_archived=1)` from the bench console or browser to confirm the session is returned with `status: "Archived"` and all messages are still readable.

---

## Acceptance Test 8 — Stalled Message Recovery (SC-005)

**Covers**: FR-016, SC-005

1. Submit a question while no short-queue worker is running.
2. Wait 11 minutes.
3. **Expected**: `mark_stalled_chat_messages()` (runs every 30 min) transitions the Pending message to Failed.

*Note*: To test without waiting 30 min for the scheduler, run manually inside bench console:
```python
bench --site <site> console
>>> from frapperag.rag.chat_runner import mark_stalled_chat_messages
>>> mark_stalled_chat_messages()
>>> frappe.db.commit()
```

---

## Acceptance Test 9 — Non-Transient AI Error (clarification Q3)

**Covers**: FR-015

1. Set an invalid API key in AI Assistant Settings.
2. Submit a chat message.
3. **Expected**: The message is marked Failed immediately (no 60-second wait); an error indicator appears in the chat UI without a page refresh.
4. **Expected**: Error detail is written to the Chat Message record.

---

## Bench Console Verification Snippets

```python
# Verify Chat Session creation
import frappe
frappe.init(site="<site>")
frappe.connect()
sessions = frappe.db.get_all("Chat Session", fields=["name", "title", "status", "owner"])
print(sessions)

# Verify Chat Messages for a session
msgs = frappe.db.get_all("Chat Message",
    filters={"session": "<session-id>"},
    fields=["name", "role", "status", "tokens_used"])
print(msgs)

# Manually trigger stalled message cleanup
from frapperag.rag.chat_runner import mark_stalled_chat_messages
mark_stalled_chat_messages()
frappe.db.commit()

# Verify LanceDB search works
from frapperag.rag.retriever import search_all_tables
results = search_all_tables([0.1] * 768)   # dummy vector — real test passes actual embedding
print(results[:2])
```
