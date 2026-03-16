# Feature Specification: RAG Embedding Pipeline — Phase 1

**Feature Branch**: `001-rag-embedding-pipeline`
**Created**: 2026-03-15
**Status**: Validated (2026-03-16)
**App**: `frapperag`

---

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Configure the AI Assistant (Priority: P1)

An ERPNext system administrator opens the AI assistant settings for the first time
after installing the app. They enter their AI provider credentials, select which
document types they want the assistant to be able to search (starting with Sales
Invoice, Customer, and Item), and define which user roles are permitted to use the
assistant. They save the settings and the app is ready to index.

**Why this priority**: Nothing else in the system can function without valid credentials
and a declared scope. All other stories depend on this configuration existing.

**Independent Test**: Can be fully tested by opening the settings form, filling it in,
saving, and confirming all values are persisted correctly — delivers a configured,
ready-to-index assistant with no other components required.

**Acceptance Scenarios**:

1. **Given** the app is freshly installed, **When** an administrator opens the AI
   assistant settings form, **Then** the form contains fields for API credentials,
   a document type selection list, a role selection list, and a sync schedule
   preference — all empty by default.

2. **Given** a valid API key is entered, **When** the administrator saves the settings,
   **Then** the key is stored securely (not visible in plain text after saving) and
   a confirmation message is shown.

3. **Given** the administrator selects "Sales Invoice", "Customer", and "Item" from
   the document type list, **When** they save, **Then** only those three types appear
   as indexable in subsequent screens.

4. **Given** the administrator assigns the "Sales Manager" role as an allowed role,
   **When** a user without that role attempts to trigger indexing, **Then** the system
   rejects the request with a clear permission error.

---

### User Story 2 — Trigger a Document Indexing Job (Priority: P1)

A configured administrator selects a document type (e.g., Customer) and triggers
an indexing run. The system immediately acknowledges the request with a job reference
number and begins processing in the background. The administrator is free to continue
working in ERPNext — they do not need to wait for the job to finish.

**Why this priority**: This is the core value delivery of Phase 1. Without a completed
indexing job, no AI-assisted search is possible. It is the primary action the feature
must support.

**Independent Test**: Can be fully tested by triggering an indexing job for the
Customer document type and confirming a job ID is returned immediately (before any
documents are processed) — delivers the ability to kick off background semantic
indexing without blocking the user.

**Acceptance Scenarios**:

1. **Given** the assistant is configured with a valid API key and "Customer" is in
   the allowed document type list, **When** an administrator triggers indexing for
   "Customer", **Then** the system responds with a unique job identifier in under
   3 seconds and the administrator's browser is not blocked.

2. **Given** a user without an allowed role attempts to trigger indexing,
   **When** the request is submitted, **Then** the system rejects it immediately
   with a clear permission-denied message and no job is created.

3. **Given** an indexing job for "Customer" is already running, **When** an
   administrator attempts to trigger another indexing job for the same document type,
   **Then** the system rejects the request immediately with a clear error message and
   creates no second job. Queuing a second run is not supported.

4. **Given** the "Customer" document type has no records in the system, **When**
   indexing is triggered, **Then** the job completes immediately with a status of
   "Completed" and a note that zero records were processed.

---

### User Story 3 — Monitor Indexing Job Progress in Real Time (Priority: P2)

After triggering an indexing job, the administrator can see its progress updating
live on screen — how many records have been processed, how many remain, and the
current status — without refreshing the page. When the job finishes (or fails),
the final outcome is clearly displayed.

**Why this priority**: Without progress visibility, the administrator has no way to
know if the system is working or stuck. This story transforms the indexing trigger
from a fire-and-forget black box into an observable operation, building confidence
in the system.

**Independent Test**: Can be fully tested by triggering an indexing job and watching
the progress indicator update on screen at least twice during a single run before
reaching a final status — delivers observable, real-time job transparency.

**Acceptance Scenarios**:

1. **Given** an indexing job is running, **When** the administrator has the job
   detail screen open, **Then** the progress percentage and records-processed count
   update automatically at least once every 10 seconds without any manual page
   refresh.

2. **Given** an indexing job completes successfully, **When** the final update
   arrives, **Then** the status changes to "Completed", the total records indexed
   is displayed, and no further updates occur.

3. **Given** an indexing job encounters a non-fatal error on a single document
   (e.g., the document summary cannot be generated), **When** the job continues
   and finishes, **Then** the final status is "Completed with Errors", the count
   of failed records is shown, and the job is not marked as a complete failure.

4. **Given** an indexing job fails catastrophically (e.g., credentials are revoked
   mid-run), **When** the failure is detected, **Then** the job status updates
   to "Failed", a descriptive error message is displayed, and all progress made
   so far is preserved in the job record.

---

### User Story 4 — Review Indexing Job History (Priority: P3)

An administrator opens a list of all indexing jobs run to date. They can see when
each job was triggered, which document type it covered, its final status, how many
records were processed, and whether any errors occurred. They can open any past job
to see its full detail.

**Why this priority**: Operational confidence and auditability. Administrators need
to verify that scheduled or manual indexing runs have succeeded, and investigate
failures after the fact.

**Independent Test**: Can be fully tested by running two indexing jobs and confirming
both appear in the history list with correct status, document type, and record counts.

**Acceptance Scenarios**:

1. **Given** three indexing jobs have been run (one for each of Sales Invoice,
   Customer, Item), **When** an administrator opens the indexing job list,
   **Then** all three jobs appear with their document type, start time, status,
   and record count.

2. **Given** a failed indexing job exists in the history, **When** the administrator
   opens its detail record, **Then** they can see the error message that caused
   the failure and the timestamp of the failure.

---

### Edge Cases

- **No records**: Indexing triggered for a document type that currently has zero
  records must complete immediately with status `Completed` and a processed count
  of 0. No special status variant is used.
- **Revoked credentials**: If the AI API key is revoked or rate-limited mid-job, the
  job must record the failure, stop gracefully, and not leave a corrupt index.
- **Interrupted job**: If the server restarts while a job is running, the job must
  transition to `Failed (Stalled)` on next detection — it must never remain
  permanently in "Running" status. `Failed (Stalled)` is the canonical terminal
  state for all stalled or worker-interrupted scenarios.
- **Permission boundary**: Documents that the indexing service account is not
  permitted to read must be silently skipped and counted as skipped (not as errors).
- **Duplicate trigger**: Triggering a second indexing job for the same document type
  while one is already in `Queued` or `Running` status must be rejected immediately
  with a clear error. No second job is created and no deferred run is queued.
- **Large document sets**: Indexing 10,000+ records must not require manual
  intervention and must not time out.

---

## Requirements *(mandatory)*

### Functional Requirements

**Settings & Configuration**

- **FR-001**: System MUST provide a single settings form where administrators can
  store and update AI API credentials.
- **FR-002**: Credentials stored in FR-001 MUST NOT be displayed in plain text to
  any user after they are saved.
- **FR-003**: System MUST allow administrators to select a list of document types
  eligible for AI indexing; in Phase 1 the eligible set MUST include at minimum
  Sales Invoice, Customer, and Item.
- **FR-004**: System MUST allow administrators to designate one or more user roles
  as permitted to trigger and manage indexing jobs.
- **FR-005**: System MUST allow administrators to record a preferred automatic sync
  schedule. (Automatic scheduled execution is deferred to a future phase; this
  field captures the preference only.)

**Indexing Trigger**

- **FR-006**: System MUST expose a mechanism for permitted users to trigger an
  indexing job for a selected document type.
- **FR-007**: The indexing trigger in FR-006 MUST respond with a unique job
  identifier before any documents are processed.
- **FR-008**: System MUST reject indexing trigger requests from users whose role
  is not in the permitted roles list defined in FR-004 This check MUST be enforced server-side before any job is created or enqueued. Client-side enforcement alone is not acceptable..
- **FR-009**: System MUST prevent two concurrent indexing jobs from running against
  the same document type simultaneously. When a duplicate trigger is attempted, the
  system MUST reject it immediately with a clear error and MUST NOT create a second
  job or queue a deferred run.

**Background Indexing Job**

- **FR-010**: The indexing job MUST read only those documents the job is permitted
  to access; documents outside its permission scope MUST be skipped, not errored.
- **FR-011**: The indexing job MUST convert each eligible document into a
  human-readable text summary before generating its semantic index entry.
  Supported document types in Phase 1: Sales Invoice, Customer, Item.
- **FR-012**: The indexing job MUST generate a semantic vector representation for
  each document text summary using the configured AI embedding service.
- **FR-013**: The indexing job MUST store all generated vector data in a location
  that is physically scoped to the current site and cannot be accessed by any
  other site on the same server.
- **FR-014**: The indexing job MUST update its progress record after every batch
  of documents processed, not only at the start and end.
- **FR-015**: A failure to process a single document MUST NOT stop the indexing
  job. The failed document MUST be counted and the job MUST continue.
- **FR-016**: A failure that makes further processing impossible (e.g., revoked
  credentials) MUST cause the job to stop, record the error, and transition to
  a terminal "Failed" state.
- **FR-023**: When indexing is re-triggered for a DocType that was previously indexed,
  the system MUST upsert each document's vector entry by document ID — updating
  existing entries and inserting new ones. The existing index table MUST NOT be
  dropped or rebuilt from scratch. Deletions (for documents removed from the source)
  are deferred to a future phase.

**Job Tracking & Visibility**

- **FR-017**: System MUST create a persistent record for every indexing job,
  capturing: document type, triggered-by user, start time, end time, status,
  records processed, records skipped, records failed, error detail, and estimated
  tokens consumed (see FR-021).
- **FR-018**: System MUST push live progress updates to the triggering user's
  session without requiring a page refresh.
- **FR-019**: A job that has been in `Running` status for more than 2 hours without
  a progress update MUST be automatically transitioned to `Failed (Stalled)`.
  Jobs in `Queued` status are exempt from this check — they are waiting for a worker
  slot and have made no progress commitment yet.
- **FR-020**: Administrators MUST be able to view a list of all indexing jobs and
  open any individual job to inspect its full detail.
- **FR-021**: The AI Indexing Job record MUST store the total 
tokens consumed during embedding generation.
- **FR-022**: Document text summaries MUST be generated using 
a per-DocType Python template function — not via LLM 
inference. LLM calls are prohibited during the 
summarisation step.

### Key Entities

- **AI Assistant Settings**: The single system-wide configuration record. Holds API
  credentials, the list of indexable document types, permitted roles, and the desired
  sync schedule. One record per site.

- **AI Indexing Job**: One record per triggered indexing run. Tracks document type,
  triggered-by user, current status (Queued / Running / Completed / Completed with
  Errors / Failed / Failed (Stalled)), progress percentage, record counts (total,
  processed, skipped, failed), start time, end time, and error detail. Many records
  per site over time.

- **Document Text Summary** (transient, not stored): An intermediate representation
  of a Frappe document as a human-readable narrative. Generated per document during
  the indexing job and discarded after the vector entry is written. Not persisted as
  a separate DocType.

- **Semantic Index Store**: The persistent store of vector embeddings, one entry per
  indexed document, scoped to the site. Contains the document type, document name,
  and vector representation. Queryable in future phases for AI-assisted search.

---

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An administrator can complete the full workflow — configure settings,
  trigger an indexing job, and observe it complete — in under 5 minutes from a
  fresh app install, assuming valid API credentials are available.

- **SC-002**: The indexing trigger returns a job identifier in under 3 seconds,
  regardless of the size of the document set being indexed.

- **SC-003**: Progress updates reach the administrator's screen within 10 seconds
  of the indexing job advancing — without any manual page action.

- **SC-004**: 100% of triggered indexing jobs produce a traceable, terminal outcome
  record. No job may remain permanently in "Running" status. Silent failures are
  not acceptable.

- **SC-005**: A completed indexing job's stored data is inaccessible from any other
  site on the same server. Cross-site data access must produce zero results.

- **SC-006**: A mixed set of 100 records spanning Sales Invoice, Customer, and Item
  can be fully indexed without any manual intervention by an administrator.

- **SC-007**: A document that fails to be indexed does not prevent the remaining
  documents in the same job from being indexed. Failure isolation is complete.

---

## Assumptions

- **Scheduled auto-sync is deferred**: The sync schedule field in Settings captures
  the administrator's preference but does not trigger automatic execution in Phase 1.
  Scheduled indexing is planned for a future phase.

- **Text summary format is fixed per document type**: The human-readable summary
  template for Sales Invoice, Customer, and Item is determined at build time.
  User-configurable summary templates are out of scope for Phase 1.

- **Single active job per document type**: The constraint in FR-009 applies per
  document type. Jobs for different document types may run concurrently.

- **Indexing service user**: The indexing job runs with the permissions of the
  system user who enqueued it (or a dedicated service role), not with elevated
  system-wide access.

- **No chat or retrieval UI**: Phase 1 is strictly indexing and storage. The ability
  to search or query the index is out of scope and planned for a future phase.

- **Supported document types in Phase 1**: Only Sales Invoice, Customer, and Item
  are supported for text summarisation in this phase. Other document types may be
  selected in Settings but will be gracefully skipped at indexing time until support
  is added.

---

## Clarifications

### Session 2026-03-15

- Q: When an indexing job is already running for a DocType and a duplicate trigger is attempted, should the system reject immediately or queue a second run? → A: Reject immediately — return a clear error; no second job is created.
- Q: Should `Failed (Stalled)` be the single canonical terminal state name for stalled/interrupted jobs (vs "Failed" or "Interrupted")? → A: Yes — `Failed (Stalled)` is the canonical name for both server-restart and 2-hour timeout scenarios.
- Q: Should the 2-hour stalled-detection check (FR-019) apply to jobs stuck in `Queued` status, or only to `Running` jobs? → A: Running only — Queued jobs are exempt; they are waiting for a worker slot and have made no progress commitment.
- Q: When indexing is re-triggered for a previously indexed DocType, should existing vector entries be upserted by document ID or should the table be rebuilt from scratch? → A: Upsert by document ID — existing entries updated in place; no table drop; deletions deferred to a future phase.
