# Feature Specification: RAG Chat Core

**Feature Branch**: `002-rag-chat-core`
**Created**: 2026-03-16
**Status**: Draft
**Input**: User description: "Specify Phase 2 — RAG Retrieval and Chat Core."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Ask a Business Question (Priority: P1)

A logged-in user navigates to the RAG Chat page and types a natural language question about their business data (e.g., "Show me unpaid invoices for ACME Corp" or "What is the credit limit for customer XYZ?"). The system searches all indexed data, filters the results to only what the user is permitted to see, and generates a plain-language answer with links to the source documents. The response appears in the chat interface without a page refresh.

**Why this priority**: This is the core value proposition — the entire feature is worthless without at least one working question-answer cycle that respects permissions and surfaces citations.

**Independent Test**: Can be fully tested by opening the chat page, submitting a question on a site with at least one indexed DocType, and verifying a response arrives with at least one clickable citation link.

**Acceptance Scenarios**:

1. **Given** data has been indexed and the user has permission to read at least one relevant record, **When** the user submits a question, **Then** an assistant message appears in the chat thread with a plain-language answer and clickable links to the source documents.
2. **Given** no data has been indexed yet, **When** the user submits any question, **Then** a friendly informational message is displayed — no exception is raised and no empty/broken state is shown.
3. **Given** all documents retrieved for the user's question are ones the user is not authorised to read, **When** the AI generates a response, **Then** the response acknowledges there is no accessible context and does not fabricate or reference restricted information.
4. **Given** the AI API returns a rate-limit error, **When** the background job encounters the error, **Then** the job pauses 60 seconds and retries — the message remains in a Pending visual state during this time.

---

### User Story 2 — Continue a Multi-Turn Conversation (Priority: P2)

A user who has been chatting returns to an existing session and asks a follow-up question that assumes prior context (e.g., "What is their credit limit?" after previously asking about a specific customer). The AI's response reflects the conversation history — up to the last 10 turns — allowing natural dialogue without the user needing to repeat prior information.

**Why this priority**: Without conversation memory, every message is an isolated interaction and the chat is not meaningfully more useful than a one-shot query tool.

**Independent Test**: Can be tested by asking a contextual follow-up question (one that uses pronouns or refers to entities mentioned in a prior turn) and verifying the AI resolves it correctly from conversation history.

**Acceptance Scenarios**:

1. **Given** a session with prior messages and the user asks a follow-up referencing a prior entity, **When** the response arrives, **Then** the AI correctly resolves the reference from conversation history without the user re-stating it.
2. **Given** a session with more than 10 prior turns, **When** a new message is submitted, **Then** the AI receives the most recent 10 turns as context (oldest turns beyond 10 are not included).

---

### User Story 3 — Manage Chat Sessions (Priority: P3)

A user can create a new chat session, view a list of their past sessions, and switch between them. Each session maintains its own independent conversation thread. Sessions owned by other users are never visible to this user.

**Why this priority**: Session management is needed for organized, multi-topic work, but the core chat loop (Story 1) delivers value even with a single auto-created session.

**Independent Test**: Can be tested by creating multiple sessions under two different user accounts and verifying complete ownership isolation.

**Acceptance Scenarios**:

1. **Given** a user is on the chat page, **When** they initiate a new conversation, **Then** a new session is created and appears at the top of their session list.
2. **Given** User A and User B both have chat sessions, **When** User A views the session list, **Then** only User A's sessions are visible — no sessions belonging to User B appear.
3. **Given** User A knows the identifier of User B's session and attempts to access it directly, **Then** a permission error is returned immediately and no message content is revealed.

---

### Edge Cases

- What happens when no indexed tables exist? → A friendly informational message is shown instead of an error or blank state.
- What happens when all retrieved results are filtered out by the user's permissions? → The AI is told that no accessible context is available and responds accordingly — it does not fabricate information from hidden records.
- What happens when a user attempts to open or interact with a session they do not own? → Immediate permission denial; no data from the session is returned.
- What happens if the AI API is temporarily unavailable due to rate limiting? → The background job pauses 60 seconds and retries; the message stays in Pending state during the pause.
- What happens if the AI API returns a non-transient error (invalid key, model unavailable)? → The message is marked Failed immediately; error detail is written to the record and surfaced to the user via realtime — no retry is attempted.
- What happens if a message stays in Pending state for more than 10 minutes (e.g., after a worker crash)? → A scheduler job automatically marks it as Failed.
- What happens if a user tries to submit a second message while the first is still Pending? → The chat input is locked (disabled) until the Pending message resolves to Completed or Failed; no second submission is accepted.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST provide a chat page where users with the `RAG User` role can type natural language questions about their business data.
- **FR-002**: System MUST allow users to create new chat sessions; session title is automatically set from the first user message when the first assistant response is successfully delivered — not at submission time.
- **FR-003**: System MUST display a list of the current user's chat sessions and allow switching between them.
- **FR-004**: System MUST accept a user's message and immediately return a message identifier to the frontend — the user must not wait for the AI response before the UI acknowledges the submission.
- **FR-005**: System MUST search all indexed business data to find semantically relevant records for the user's question.
- **FR-006**: System MUST check the requesting user's access permissions on every candidate record retrieved from the index — records the user cannot read MUST be excluded before the AI prompt is assembled.
- **FR-007**: System MUST include the last 10 conversation turns from the current session as context when generating an AI response.
- **FR-008**: System MUST generate a natural language response using only the permission-filtered context plus conversation history.
- **FR-009**: System MUST return a structured list of source document citations alongside every AI response.
- **FR-010**: Each citation MUST be presented in the chat UI as a clickable link that navigates directly to the source record in Frappe.
- **FR-011**: When no indexed data tables exist, the system MUST respond with a friendly informational message rather than raising an error.
- **FR-012**: When all retrieved records are filtered out by permissions, the system MUST signal this to the AI so it responds without fabricating information, rather than failing silently.
- **FR-013**: Users MUST only be able to read or interact with chat sessions and messages they own — cross-user access MUST be denied immediately.
- **FR-014**: The chat page MUST update with the AI response in real-time as soon as it is ready, without requiring the user to refresh the page.
- **FR-015**: When the AI provider returns a rate-limit error (ResourceExhausted), the system MUST pause 60 seconds and retry the request rather than immediately failing the message. For all other AI errors (e.g., invalid API key, model unavailable), the system MUST fail the message immediately — writing error detail to the message record and publishing the failure via realtime — without retrying.
- **FR-016**: Chat Messages that remain in a Pending state for more than 10 minutes MUST be automatically marked as Failed by a scheduled background process.
- **FR-017**: The Google API key MUST be read from the application Settings at the start of every background job — it MUST NOT be read or cached at request time.
- **FR-018**: The session creation endpoint MUST return the new session identifier synchronously in its HTTP response so the frontend can navigate to the new session immediately, without waiting for any background processing.
- **FR-019**: The chat input MUST be locked (disabled) whenever a message in the current session is in Pending status. The input MUST re-enable automatically when the Pending message transitions to Completed or Failed.
- **FR-020**: The session list MUST provide an Archive action per session. Triggering it transitions the session from Open to Archived. Archived sessions are excluded from the default session list view; they remain accessible via a dedicated toggle (include_archived=1). Archiving does not delete any messages.

### Key Entities *(data involved)*

- **Chat Session**: A named container for a single conversation thread. Belongs to one user. Attributes: owning user, auto-generated title (from first message), status (Open/Archived), creation date. Transition Open → Archived is triggered exclusively by an explicit user action (Archive button in the session list).
- **Chat Message**: A single conversational turn within a session. Attributes: parent session, role (user or assistant), message content, citations (structured list of source records), processing status (Pending / Completed / Failed), token usage count.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A user who submits a question sees the AI response appear in the chat thread without performing a page refresh — the update is delivered live when the response is ready.
- **SC-002**: The AI response never contains content from documents the requesting user is not authorised to read — verifiable by submitting a question that would match restricted records and confirming those records do not appear in the response or citations.
- **SC-003**: Every citation in an AI response can be opened in a single click, navigating the user directly to the source record in Frappe.
- **SC-004**: A follow-up question that uses pronouns or implicit references to entities mentioned in prior turns is answered correctly without the user needing to repeat prior information.
- **SC-005**: A message left in Pending state (e.g., after an unexpected worker stop) is automatically moved to Failed within 15 minutes by the scheduler (cron fires every 5 minutes; worst-case = 10-min cutoff + 5-min poll).
- **SC-006**: A user attempting to access another user's chat session receives an access-denied response with no message content revealed.
- **SC-007**: The chat page delivers its full feature set — session list, message thread, real-time response delivery — without any client-side build tool, package manager, or external JavaScript dependency.

## Assumptions

- The Phase 1 embedding pipeline (branch `001-rag-embedding-pipeline`) is deployed and has produced at least one indexed LanceDB table in the site's private files directory.
- The AI Assistant Settings DocType (from Phase 1) already stores the Google API key; this feature reads from it but does not add new key management UI.
- Top-K retrieval defaults to 5 candidate records per indexed table; this is a tuning parameter and does not affect the specification.
- Session title is auto-generated from the first user message and written when the first assistant response is successfully delivered; user-editable titles are out of scope for this phase.
- The `RAG User` role (created in Phase 1) gates access to the chat page; no new roles are introduced in this phase.
- Conversation memory is scoped strictly to the current session — cross-session or persistent long-term memory is out of scope.
- Responses are delivered as complete messages once generation finishes (not streamed character-by-character).
- The embedding model used for query embedding matches the model used during indexing (`gemini-embedding-001`, 768 dimensions, as established in Phase 1).

## Clarifications

### Session 2026-03-16

- Q: While a message is Pending in a session, can the user submit another message? → A: No — the chat input is locked until the Pending message resolves to Completed or Failed.
- Q: What causes a Chat Session to move from Open to Archived? → A: Explicit user action — an "Archive" button in the session list.
- Q: For non-rate-limit AI errors (e.g., invalid API key, model unavailable), should the message fail immediately or retry? → A: Fail immediately — mark message Failed, write error detail to the record, publish via realtime.
