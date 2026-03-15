# Research: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline`
**Date**: 2026-03-15

---

## 1. LanceDB File-Based Storage in Python 3.11

**Decision**: Use `lancedb` package (pure file-based mode, no server). Connect via
`lancedb.connect(path)` where `path` is a directory. Each DocType gets its own
LanceDB table inside that directory.

**API pattern**:
```python
# Always import inside the job function — never at module level
import lancedb
import pyarrow as pa

db = lancedb.connect(frappe.get_site_path("private/files/rag"))
schema = pa.schema([
    pa.field("id",            pa.string()),      # "{doctype}:{name}"
    pa.field("doctype",       pa.string()),
    pa.field("name",          pa.string()),
    pa.field("text",          pa.string()),      # human-readable summary
    pa.field("vector",        pa.list_(pa.float32(), 768)),  # text-embedding-004 dims
    pa.field("last_modified", pa.string()),      # ISO datetime string
])
table_name = doctype.lower().replace(" ", "_")
table = db.create_table(table_name, schema=schema, exist_ok=True)

# Insert / upsert
table.add(rows)                    # initial load
table.merge_insert("id") \         # incremental: upsert by id
    .when_matched_update_all() \
    .when_not_matched_insert_all() \
    .execute(rows)
```

**Embedding dimensions**: `text-embedding-004` outputs 768 float32 values by default.
Use `pa.list_(pa.float32(), 768)` — a fixed-size list field; LanceDB indexes this
as a vector column automatically.

**Rationale**: LanceDB's file mode requires no server, no port, no Docker. Data is
stored as Arrow IPC files on disk. The `lancedb.connect(path)` call is instant and
has no global state — safe to instantiate inside each job invocation.

**Alternatives considered**:
- FAISS: file-based but lacks built-in upsert; requires numpy global state at import
  (changai anti-pattern). Rejected.
- ChromaDB: supports file mode but has a persistent server option that could cause
  confusion; heavier dependency tree. Rejected.
- pgvector: requires PostgreSQL extension; external infrastructure. Rejected.

---

## 2. Google `text-embedding-004` via Direct SDK

**Decision**: Use `google-generativeai` SDK (`import google.generativeai as genai`).
No LangChain. No intermediate wrapper libraries.

**API pattern**:
```python
import google.generativeai as genai

genai.configure(api_key=api_key)

# Single document
result = genai.embed_content(
    model="models/text-embedding-004",
    content=text,
    task_type="RETRIEVAL_DOCUMENT",
)
vector = result["embedding"]  # list of 768 floats

# Batch (up to 100 documents per request, max 2048 tokens each)
result = genai.embed_content(
    model="models/text-embedding-004",
    content=texts,            # list[str]
    task_type="RETRIEVAL_DOCUMENT",
)
vectors = result["embedding"]  # list[list[float]]
```

**Task type**: `RETRIEVAL_DOCUMENT` for indexing; `RETRIEVAL_QUERY` must be used when
embedding search queries in Phase 2 — these are not interchangeable.

**Rate limits** (as of 2026):
- Free tier: 100 requests/minute, 1500 requests/day for embedding models
- Paid tier: 1500 requests/minute

**Batch strategy**: Embed in batches of **20 documents per API call** to stay well
within free-tier rate limits and give predictable latency. Each batch call is followed
by a short `time.sleep(0.1)` only if a `ResourceExhausted` exception is caught
(exponential back-off: 2s, 4s, 8s, max 3 retries). Retried documents are counted
as failed after exhausting retries.

**Rationale**: `google-generativeai` is the official Python SDK. Embedding with
`embed_content()` is a single function call with no class instantiation, making it
safe to call from inside a background job without module-level state. LangChain would
introduce 30+ transitive dependencies and a class instantiation that can carry global
state — the changai anti-pattern.

**Alternatives considered**:
- `google-genai` (new v2 SDK): functionally equivalent but requires more boilerplate
  (`genai.Client(api_key=...)`, `client.models.embed_content(...)`, and
  `result.embeddings[0].values`). The older SDK has the cleaner one-liner for
  embedding-only use. May migrate in Phase 2 if needed.
- Custom REST calls to `https://generativelanguage.googleapis.com/v1beta/...`:
  avoids SDK dependency but requires manual auth, retry logic, and JSON parsing.
  The SDK handles all this already. Rejected.

---

## 3. Frappe Background Job Pattern

**Decision**: Use `frappe.enqueue()` with explicit `site=frappe.local.site` (changai
pattern), `queue="long"`, `timeout=7200` (2 hours), and a unique `job_name` to
prevent duplicate concurrent jobs for the same DocType.

**API pattern** (in the whitelist method):
```python
job_doc = frappe.get_doc({
    "doctype": "AI Indexing Job",
    "doctype_to_index": doctype,
    "status": "Queued",
    "triggered_by": frappe.session.user,
}).insert(ignore_permissions=True)

frappe.enqueue(
    "frapperag.rag.indexer.run_indexing_job",
    queue="long",
    timeout=7200,
    job_name=f"rag_index_{doctype.lower().replace(' ', '_')}",
    site=frappe.local.site,          # ← changai pattern: explicit site
    job_id=job_doc.name,
    doctype=doctype,
    user=frappe.session.user,
)
return {"job_id": job_doc.name, "status": "Queued"}
```

**Job function opening** (inside background job):
```python
def run_indexing_job(job_id: str, doctype: str, user: str, **kwargs):
    # Site context is already initialised by the Frappe worker
    # (frappe.init + frappe.connect happen before this function is called)
    # No need to manually call frappe.init() here — the worker handles it.
    # The `site` kwarg passed to enqueue ensures correct site routing.
    job = frappe.get_doc("AI Indexing Job", job_id)
    ...
```

**Duplicate prevention**: `job_name` uniqueness is enforced at the RQ (Redis Queue)
level. If a job with the same `job_name` is already queued or running, `frappe.enqueue`
silently skips enqueuing. The whitelist method must also check the DB before enqueuing
to return a meaningful error to the user.

**Stalled job detection**: A `scheduler_events` cron entry runs every 30 minutes
and transitions any job with `status="Running"` and
`last_progress_update < now - 2 hours` to `status="Failed (Stalled)"`.

**Rationale**: Explicit `site=frappe.local.site` prevents the wrong site's data from
being accessed when multiple sites share a bench worker pool. This is the root cause
of changai's isolation bug (module-level globals without site scoping).

---

## 4. `frappe.publish_realtime` Pattern

**Decision**: Publish progress events to the triggering user only (not broadcast),
using a namespaced event name to avoid collisions.

**Pattern** (inside background job):
```python
frappe.publish_realtime(
    event="rag_index_progress",
    message={
        "job_id": job_id,
        "status": job.status,
        "progress_percent": job.progress_percent,
        "processed_records": job.processed_records,
        "total_records": job.total_records,
        "failed_records": job.failed_records,
        "skipped_records": job.skipped_records,
    },
    user=user,            # target only the triggering user's session
    after_commit=False,   # publish immediately, not after DB commit
)
```

**Client-side subscription** (Vanilla JS):
```javascript
frappe.realtime.on("rag_index_progress", function(data) {
    // update progress bar and status fields from data
});
```

**Rationale**: Publishing to `user=` rather than broadcasting avoids leaking
job progress to other administrators. `after_commit=False` ensures the update
reaches the browser even if the DB transaction is open (in-progress updates).

---

## 5. Permission Enforcement at Indexing Time

**Decision**: Use `frappe.db.get_all()` with `ignore_permissions=False` (the default)
for all document reads inside the job. Additionally, call `frappe.set_user(user)`
at the start of the job to ensure the effective user context is the triggering user,
not the system worker user.

**Pattern**:
```python
def run_indexing_job(job_id, doctype, user, **kwargs):
    frappe.set_user(user)   # enforce triggering user's permissions
    records = frappe.db.get_all(
        doctype,
        fields=["name", "modified"],
        ignore_permissions=False,   # default; explicit for clarity
    )
    for record in records:
        if not frappe.has_permission(doctype, doc=record.name, ptype="read", user=user):
            job.skipped_records += 1
            continue
        # ... convert and embed
```

**Rationale**: Without `frappe.set_user()`, the background worker runs as the
system user and may access documents the triggering user is not permitted to see.
Calling `has_permission` explicitly for each record (Principle III) adds a double
check and correctly counts skipped records separately from failed ones.

---

## 6. Text Summarisation Templates

**Decision**: Fixed Jinja2-style f-string templates per DocType, rendered server-side
inside `text_converter.py`. No LLM involved in summary generation.

**Rationale**: The spec mandates human-readable text summarisation as a prerequisite
for embedding. Using the LLM to generate summaries would:
1. Double the Gemini API calls (one for summary, one for embedding)
2. Add non-determinism to the index content
3. Make re-indexing expensive

Simple field concatenation is fast, deterministic, and sufficient for semantic
embedding — the embedding model handles semantic understanding.

**Sales Invoice template**:
```
Sales Invoice {name} issued on {posting_date} to customer {customer} ({customer_name}).
Grand total: {grand_total} {currency}. Status: {status}. Due date: {due_date}.
Items: {items_text}. Outstanding amount: {outstanding_amount}.
```

**Customer template**:
```
Customer {customer_name} (ID: {name}). Type: {customer_type}.
Customer group: {customer_group}. Territory: {territory}.
Primary contact: {email_id}. Outstanding amount: {outstanding_amount}.
```

**Item template**:
```
Item {item_name} (code: {name}). Item group: {item_group}.
Stock unit: {stock_uom}. Standard selling rate: {standard_rate}.
Description: {description}. Is stock item: {is_stock_item}.
```

**Unknown/unsupported DocType**: returns `None`; the indexing job skips and counts
the document as skipped, not failed.

---

## 7. `requirements.txt` Dependencies

```
lancedb>=0.8.0
pyarrow>=14.0.0
google-generativeai>=0.8.0
```

**Notes**:
- `pyarrow` is a transitive dependency of `lancedb` but pinned explicitly because
  LanceDB is sensitive to pyarrow version mismatches.
- No `langchain*`, no `sentence-transformers`, no `faiss-cpu`, no `anthropic`,
  no `openai` — all prohibited by Principle IV.
- `lancedb>=0.8.0` introduces the `merge_insert()` upsert API used for incremental
  indexing. Earlier versions require a delete-then-insert pattern.

---

## 8. Resolved Decisions Summary

| Question | Decision | Principle |
|---|---|---|
| Vector store | LanceDB file-based, one table per DocType | II, IV |
| Embedding SDK | `google-generativeai`, `embed_content()` directly | IV |
| Embedding model | `text-embedding-004`, 768 dimensions, task_type=RETRIEVAL_DOCUMENT | Tech Stack |
| Batch size | 20 documents per embedding API call | V (avoid blocking) |
| Site scoping | `frappe.local.site` passed to `frappe.enqueue`; LanceDB path via `frappe.get_site_path()` | II |
| Permission enforcement | `frappe.set_user(user)` + per-record `frappe.has_permission()` | III |
| Text summary | Fixed f-string template per DocType in `text_converter.py` | I |
| Duplicate job guard | `job_name` uniqueness in RQ + pre-enqueue DB check | FR-009 |
| Stalled job recovery | 30-minute scheduler cron; 2-hour threshold | FR-019 |
| LangChain | Not used | IV |
| Import strategy | All heavy imports (`lancedb`, `pyarrow`, `google.generativeai`) inside job functions | II (no module-level globals) |
