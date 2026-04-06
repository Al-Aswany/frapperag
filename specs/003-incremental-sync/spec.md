# Feature Specification: Incremental Sync

**Feature Branch**: `003-incremental-sync`
**Created**: 2026-04-04
**Status**: Draft
**Input**: User description: "Specify Phase 3 — Incremental Sync."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Index Stays Current After Document Changes (Priority: P1)

An employee edits a Customer record — updating the credit limit or adding a note — then immediately asks the RAG chat assistant a question about that customer. The assistant's answer reflects the updated data without the administrator needing to trigger a manual re-index. The update happens invisibly: as soon as the document is saved in Frappe, the new content is queued for indexing in the background.

**Why this priority**: The primary motivation for incremental sync is to keep chat answers accurate after routine data-entry work. Without auto-indexing on save, the RAG index silently drifts out of date and the assistant confidently cites stale information — eroding user trust. Everything else in this phase is supporting infrastructure around this core behaviour.

**Independent Test**: Can be fully tested by (1) confirming a field value is returned correctly by the assistant, (2) editing that field in Frappe and saving, (3) waiting for the background job to complete, then (4) asking the same question and verifying the updated value appears in the response.

**Acceptance Scenarios**:

1. **Given** a whitelisted DocType record exists and is indexed, **When** an authorised user edits and saves it, **Then** a background sync job is queued immediately and the vector index entry for that record is updated once the job completes.
2. **Given** a new record is created for a whitelisted DocType, **When** the record is saved, **Then** a background sync job is queued and the record is added to the vector index.
3. **Given** a whitelisted DocType record is saved, **When** the RAG sidecar is temporarily unavailable, **Then** the sync job fails with a logged error — the document save itself is not blocked or rolled back.
4. **Given** a DocType is NOT on the whitelist, **When** a record of that type is saved, **Then** no sync job is queued and no index activity occurs.

---

### User Story 2 — Deleted Records Disappear From Chat Answers (Priority: P2)

An administrator cancels and deletes a Sales Invoice. The next time any user asks the assistant a question that would have matched that invoice, the deleted record does not appear in the response or citations — even though a prior full re-index had included it.

**Why this priority**: Serving citations that link to deleted records causes broken links and erodes trust. However, the chat experience continues to work fully (Story 1) even while this story is unimplemented; deletions are a lower-frequency event than edits.

**Independent Test**: Can be tested by indexing a record, deleting it in Frappe, waiting for the sync job to complete, then asking a question that previously matched that record and confirming it no longer appears in the response.

**Acceptance Scenarios**:

1. **Given** an indexed record is permanently deleted or trashed in Frappe, **When** the deletion completes, **Then** a background sync job is queued to remove the record's vector entry from the index.
2. **Given** the sync job runs for a deleted record, **Then** the record's vector entry is absent from the index and no further queries return it as a citation.
3. **Given** a record is deleted while the sidecar is unavailable, **Then** the sync job fails with a logged error — the deletion in Frappe is not blocked.

---

### User Story 3 — Whitelist Changes Are Reflected in the Index (Priority: P3)

An administrator removes a DocType from the AI Assistant Settings whitelist — for example, because the data is sensitive and should no longer be surfaced in chat. After saving the settings, all vector entries for that DocType are purged so the assistant can no longer retrieve or cite records of that type. Conversely, adding a new DocType to the whitelist does not automatically index it — the existing manual "Index Now" action from Phase 1 is the intended path.

**Why this priority**: Without whitelist-driven purges, removing a DocType from the whitelist has no effect on what the AI actually returns — creating a false sense of data governance. Adding this behaviour is important for privacy correctness but has no impact on the daily indexing loop (Stories 1 and 2).

**Independent Test**: Can be tested by (1) indexing records of a DocType, (2) removing that DocType from the whitelist and saving settings, (3) waiting for the purge job to complete, then (4) asking a question that previously matched those records and confirming none appear.

**Acceptance Scenarios**:

1. **Given** a DocType is on the whitelist and has indexed records, **When** an administrator removes it from the whitelist and saves AI Assistant Settings, **Then** a background purge job is queued to remove all vector entries for that DocType.
2. **Given** the purge job completes, **Then** no chat query returns records belonging to the purged DocType.
3. **Given** a new DocType is added to the whitelist, **When** the settings are saved, **Then** no automatic indexing is triggered — the administrator must manually initiate indexing using the existing "Index Now" action.

---

### User Story 4 — Admins Can Monitor Sync Health (Priority: P4)

An administrator notices the chat assistant is returning stale information. They open AI Assistant Settings and review the incremental sync activity panel, which shows counts of recent sync successes and failures per DocType, the last time each DocType was synced, and a list of any records that failed to sync. From this view they can retry failed sync jobs.

**Why this priority**: Without visibility, administrators have no way to detect drift. But the index continues to grow (via Stories 1–2) even with zero observability; monitoring is operational hygiene rather than a functional requirement.

**Independent Test**: Can be tested by introducing a deliberate sync failure (e.g., stopping the sidecar mid-job), then verifying the failure appears in the sync health panel and can be retried.

**Acceptance Scenarios**:

1. **Given** incremental sync jobs have run, **When** the administrator opens AI Assistant Settings, **Then** a summary shows per-DocType counts of successful and failed sync events within the last 24 hours plus the last successful sync timestamp.
2. **Given** one or more sync jobs have failed, **When** the administrator views the failure list, **Then** each failure identifies the specific DocType and record name and provides a Retry action.
3. **Given** the administrator clicks Retry on a failed sync entry, **Then** a new Sync Event Log entry is created for the retry attempt and a new sync job is queued — the original Failed entry is preserved as history and remains visible in the log.

---

### Edge Cases

- What happens when a record is saved multiple times in rapid succession before the first sync job completes? If the first job is still queued (not yet running), no duplicate is enqueued. If the first job is already executing, a new job is queued immediately — the running job covers the prior state and the new job will update the index to the latest state when it runs.
- What happens when a record passes permission check at the time of save but the indexing user's permissions change before the job executes? The job checks permissions at execution time using the job's stored user context; if permission is denied at run time, the record is skipped and the outcome is logged.
- What happens when the sidecar is offline for an extended period and many sync events accumulate? Jobs queue normally in Frappe's background job system; they are processed in order once the sidecar is available again.
- What happens when a document's whitelisted DocType is renamed via Frappe's Rename feature? The rename triggers an after-rename hook that queues a delete-old-key plus index-new-record pair.
- What happens if the AI Indexing Job record for a sync event is lost (e.g., database restored from backup)? Stale vector entries remain until the next full re-index is triggered manually.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: When a record belonging to a whitelisted DocType is created or updated, the system MUST automatically queue a background sync job to (re-)index that record — without blocking the document save operation.
- **FR-002**: When a record belonging to a whitelisted DocType is deleted or trashed, the system MUST automatically queue a background sync job to remove that record's vector entry from the index.
- **FR-003**: Background sync jobs MUST be dispatched via the existing async job infrastructure and MUST NOT run inline during the Frappe document lifecycle hook.
- **FR-004**: The incremental sync job MUST verify the document still satisfies the indexing user's read permissions at job execution time. If permission is denied at run time, the record MUST be skipped and the outcome logged — no error is raised to the user.
- **FR-005**: When a DocType is removed from the AI Assistant Settings whitelist, the system MUST queue a background purge job that drops the entire vector table for that DocType in a single atomic operation. The table is recreated from scratch if the DocType is re-added to the whitelist and "Index Now" is triggered.
- **FR-006**: Adding a DocType to the whitelist MUST NOT trigger automatic indexing. The administrator MUST use the existing manual "Index Now" action from Phase 1 to populate the index for the newly-added DocType.
- **FR-007**: If the RAG sidecar is unavailable when a sync job executes, the job MUST fail gracefully — logging the error and recording the failure — without rolling back or blocking the triggering document operation.
- **FR-008**: The system MUST prevent duplicate sync jobs for the same record: if a sync job for a given record is already queued and has not yet started, a subsequent save event on the same record MUST NOT enqueue an additional job. If a sync job for a record is already actively executing when a new save occurs, a new sync job MUST be queued immediately — the running job will index the prior state and the new job will update the index to the latest state once it runs.
- **FR-009**: AI Assistant Settings MUST surface a sync health summary showing, per whitelisted DocType: count of successful sync events, count of failed sync events, and timestamp of the last successful sync — for events within the last 24 hours.
- **FR-010**: The sync health view MUST list individual failed sync events, each identifying the DocType and record name, with a Retry action. Clicking Retry MUST create a new Sync Event Log entry for the retry attempt and queue a new sync job — the original Failed entry MUST be preserved as history.
- **FR-011**: When a record is renamed via Frappe's built-in Rename operation, the system MUST queue a job to remove the old vector entry and index the record under its new identifier.
- **FR-012**: Sync job outcomes (success, skipped due to permissions, failed) MUST be recorded persistently so that FR-009 and FR-010 can be satisfied without querying external systems.
- **FR-013**: A scheduled job MUST prune Sync Event Log entries older than 30 days to prevent unbounded storage growth.

### Key Entities *(data involved)*

- **Sync Event Log**: A record of each incremental sync attempt. Attributes: DocType, record name, trigger type (create / update / delete / rename / purge / retry), outcome (Queued / Success / Skipped / Failed), error message if failed, timestamp. One entry is created per attempt — retries produce new entries; original Failed entries are preserved. Used to populate the sync health summary and failure list.
- **AI Assistant Settings** (extended from Phase 1): Gains a section displaying the sync health summary (FR-009) and failed event list (FR-010). No new top-level settings DocType is introduced; this is an extension of the existing settings page.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After saving a whitelisted record, the updated content is reflected in a chat answer within the time required for the background job queue to drain under normal load — no manual re-index is required.
- **SC-002**: After deleting a whitelisted record, a subsequent chat query that previously matched that record no longer includes it as a citation — verifiable without a manual re-index.
- **SC-003**: Removing a DocType from the whitelist and saving AI Assistant Settings results in zero records of that DocType appearing in any subsequent chat answer — verifiable by querying for content unique to the purged DocType.
- **SC-004**: The AI Assistant Settings sync health panel correctly reflects the current success and failure state of recent sync activity, updated within 5 minutes of each job completing.
- **SC-005**: A failed sync job that is retried from the admin panel re-queues successfully and, if the underlying cause is resolved, completes on retry — a new success entry appears in the log and the original failed entry remains visible in history.
- **SC-006**: The document save, update, and delete operations for any whitelisted DocType take no measurably longer with incremental sync enabled than without — sync is fully decoupled from the document lifecycle.

## Assumptions

- Phase 1 (001-rag-embedding-pipeline) is deployed and has produced at least one indexed LanceDB table; this phase adds ongoing maintenance on top of that baseline.
- Phase 2 (002-rag-chat-core) is deployed; the chat pipeline reads from the same index this phase maintains.
- Frappe's `doc_events` hook mechanism is the trigger for create/update/delete/rename events; this is standard Frappe and requires no additional infrastructure.
- The indexing user for incremental sync jobs is the same administrator-level user configured for the original full index — preserving permission consistency.
- Full re-index ("Index Now" from Phase 1) remains the recovery mechanism for catastrophic index loss or schema migration; incremental sync does not replace it.
- Deduplication of queued sync jobs (FR-008) leverages Frappe's `job_id` parameter on `frappe.enqueue`; duplicate prevention is best-effort and is not guaranteed across worker restarts.
- The Sync Event Log stores lightweight outcome records — not document content — so storage growth is bounded by event frequency, not document size (managed by FR-013 pruning).
- Records modified while the sync worker queue is paused are not automatically caught up by this phase; manual re-index is the recovery path for queue gaps.
- Catch-up scheduling (periodic scan for recently-modified records not yet reflected in the index) is explicitly out of scope for this phase and may be addressed in a future phase.

## Clarifications

### Session 2026-04-04

- Q: When the administrator clicks Retry on a failed sync entry, what happens to the original failed entry in the log? → A: A new Sync Event Log entry is created for the retry attempt; the original Failed entry is preserved as history (one entry per attempt, audit trail maintained).
- Q: When a DocType is removed from the whitelist, how should the purge job remove its vector entries? → A: Drop the entire vector table for that DocType in a single atomic operation — instant regardless of record count; table is recreated from scratch if the DocType is re-added and "Index Now" is triggered.
- Q: What should happen when a new save occurs for a record whose sync job is already actively executing? → A: Queue a new sync job immediately — the running job indexes the prior state; the new job corrects the index to the latest state when it runs (eventual consistency, no worker coordination required).
