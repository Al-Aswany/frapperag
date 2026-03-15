# Implementation Plan: RAG Embedding Pipeline — Phase 1

**Branch**: `001-rag-embedding-pipeline` | **Date**: 2026-03-15 | **Last updated**: 2026-03-15 (spec clarifications applied)
**Spec**: `specs/001-rag-embedding-pipeline/spec.md`
**App**: `frapperag` (`apps/frapperag/`)

---

## Summary

Build the foundational RAG indexing pipeline for the `frapperag` app. This phase
delivers: (1) an AI Assistant Settings DocType for site-scoped configuration,
(2) an AI Indexing Job DocType with real-time progress tracking, (3) a LanceDB
file-based vector store scoped per site, (4) a Gemini `text-embedding-004` embedding
module, (5) a document-to-text conversion module for Sales Invoice / Customer / Item,
and (6) a whitelisted API that enqueues all heavy work as a Frappe background job and
returns a job ID immediately. No chat or retrieval UI is included in this phase.

---

## Technical Context

| Concern | Choice | Notes |
|---|---|---|
| **Language / Version** | Python 3.11+ | Enforced by constitution |
| **Framework** | Frappe v15+, ERPNext v15+ | Enforced by constitution |
| **Vector store** | LanceDB >= 0.8.0 (file-based, local) | `pa.list_(pa.float32(), 768)` schema |
| **Embedding model** | Google `text-embedding-004` | 768 dims, task_type=RETRIEVAL_DOCUMENT |
| **Embedding SDK** | `google-generativeai` >= 0.8.0 | Direct SDK; no LangChain |
| **LLM** | None in Phase 1 | Embeddings only; no chat |
| **Storage path** | `frappe.get_site_path("private/files/rag/")` | Never bench-level or app-level |
| **Testing** | N/A — no automated tests per Principle VII | Manual acceptance per quickstart.md |
| **Frontend** | Vanilla JS (Frappe Desk Page) | `frappe.require`, `frappe.call`, `frappe.realtime` |
| **Async** | `frappe.enqueue(queue="long", site=frappe.local.site)` | HTTP handler returns job ID in <3s |
| **Target platform** | Linux (Frappe bench) | Standard bench worker + Redis Queue |
| **Batch size** | 20 documents per Gemini API call | Free-tier safe; back-off on ResourceExhausted |

---

## Constitution Check

*GATE: Must pass before implementation begins. Re-checked after Phase 1 design.*

| Principle | Gate | Status | Evidence |
|---|---|---|---|
| **I. Frappe-Native Architecture** | All data as DocTypes; all APIs as `@frappe.whitelist()`; no custom web server | PASS | 4 DocTypes in JSON fixtures; 3 whitelist methods; hooks.py for scheduler and install |
| **II. Per-Client Data Isolation** | LanceDB at `frappe.get_site_path()`; `site=frappe.local.site` in enqueue; all heavy imports inside job function | PASS | `lancedb_store.py` uses `frappe.get_site_path("private/files/rag/")` exclusively; no module-level state |
| **III. Permission-Aware RAG Retrieval** | `frappe.set_user(user)` at job start; `frappe.has_permission()` per record before text conversion | PASS | Permission-excluded docs counted as skipped, not failed |
| **IV. Zero External Infrastructure** | Only LanceDB (file) + Google Gemini API; no Docker, no cloud DB, no separate servers | PASS | requirements.txt: `lancedb`, `pyarrow`, `google-generativeai` only |
| **V. Asynchronous-by-Default** | `trigger_indexing` creates DocType record + enqueues + returns job_id; zero blocking I/O in handler | PASS | All embedding, LanceDB writes, and DB reads are inside `run_indexing_job()` |
| **VI. Zero-Friction Installation** | `after_install` creates LanceDB dir; DocType JSON fixtures; roles in `fixtures` list | PASS | No manual steps beyond API key entry in AI Assistant Settings |
| **VII. No Automated Tests** | No test files; no test dependencies; no test tasks | PASS | quickstart.md manual acceptance checklist only |

**All 7 principles pass. Implementation may proceed.**

---

## Project Structure

### Documentation (this feature)

```
specs/001-rag-embedding-pipeline/
├── spec.md               <- feature specification
├── plan.md               <- this file
├── research.md           <- technical decisions and rationale
├── data-model.md         <- DocType definitions and LanceDB schema
├── quickstart.md         <- manual acceptance validation guide
├── contracts/
│   └── api-contracts.md  <- whitelist method and realtime event contracts
└── checklists/
    └── requirements.md   <- spec quality checklist (all passed)
```

### Source Code (`apps/frapperag/frapperag/`)

```
apps/frapperag/frapperag/
|
+-- hooks.py                             # after_install, scheduler_events, fixtures
+-- requirements.txt                     # lancedb, pyarrow, google-generativeai
+-- modules.txt                          # FrappeRAG
|
+-- setup/
|   +-- __init__.py
|   +-- install.py                       # after_install() -- creates private/files/rag/
|
+-- frapperag/                           # Frappe module (same name as app)
|   +-- doctype/
|       +-- ai_assistant_settings/
|       |   +-- __init__.py
|       |   +-- ai_assistant_settings.json    # Single DocType definition
|       |   +-- ai_assistant_settings.py      # validate() hook
|       +-- rag_allowed_doctype/
|       |   +-- __init__.py
|       |   +-- rag_allowed_doctype.json      # Child table: doctype_name -> DocType
|       +-- rag_allowed_role/
|       |   +-- __init__.py
|       |   +-- rag_allowed_role.json          # Child table: role -> Role
|       +-- ai_indexing_job/
|           +-- __init__.py
|           +-- ai_indexing_job.json           # Standard DocType, naming series
|           +-- ai_indexing_job.py             # controller stub
|
+-- rag/                                 # Core RAG utilities (no module-level state)
|   +-- __init__.py
|   +-- base_indexer.py                  # BaseIndexer ABC (BaseTool lifecycle)
|   +-- lancedb_store.py                 # get_store(), upsert_vectors()
|   +-- text_converter.py                # to_text(doctype, doc_dict) -> str | None
|   +-- embedder.py                      # embed_texts(texts, api_key) -> list[list[float]]
|   +-- indexer.py                       # DocIndexerTool + run_indexing_job() + mark_stalled_jobs()
|
+-- api/
    +-- __init__.py
    +-- indexer.py                       # @frappe.whitelist() HTTP endpoints
```

---

## Module Design

### `hooks.py`

```python
app_name      = "frapperag"
app_title     = "FrappeRAG"
app_publisher = "Your Org"
app_license   = "MIT"

after_install = "frapperag.setup.install.after_install"

fixtures = [
    {"dt": "Role", "filters": [["name", "in", ["RAG Admin", "RAG User"]]]},
]

scheduler_events = {
    "cron": {
        "*/30 * * * *": ["frapperag.rag.indexer.mark_stalled_jobs"],
    }
}
```

### `requirements.txt`

```
lancedb>=0.8.0
pyarrow>=14.0.0
google-generativeai>=0.8.0
```

No LangChain. No FAISS. No anthropic. No openai.

---

### `setup/install.py`

```python
import os
import frappe

def after_install():
    rag_path = frappe.get_site_path("private", "files", "rag")
    os.makedirs(rag_path, exist_ok=True)
    frappe.db.commit()
```

Creates `{site}/private/files/rag/` on install. Zero manual setup.

---

### `rag/base_indexer.py` — BaseIndexer ABC

Adapted from `frappe_assistant_core/core/base_tool.py`. Differences:
- Removed MCP-specific fields (`inputSchema`, `to_mcp_format`, `source_app` hook)
- `check_permission(user)` takes the user argument explicitly
- `log_execution` writes to `frappe.logger("frapperag")` at INFO level
- No `_config_cache` (settings are always read fresh from the Single DocType)

```python
import time
from abc import ABC, abstractmethod
import frappe


class BaseIndexer(ABC):

    name: str = ""
    source_app: str = "frapperag"

    @abstractmethod
    def validate_arguments(self, args: dict) -> None:
        """Raise frappe.ValidationError if args invalid."""

    @abstractmethod
    def check_permission(self, user: str) -> None:
        """Raise frappe.PermissionError if user not authorised."""

    @abstractmethod
    def execute(self, args: dict) -> dict:
        """Enqueue job. Return {"job_id": ..., "status": "Queued"}."""

    def safe_execute(self, args: dict, user: str) -> dict:
        """Validate -> check permission -> execute -> log. Returns result dict."""
        start = time.time()
        try:
            self.validate_arguments(args)
            self.check_permission(user)
            result = self.execute(args)
            self.log_execution(args, result, time.time() - start, success=True)
            return result
        except (frappe.PermissionError, frappe.ValidationError):
            self.log_execution(args, {}, time.time() - start, success=False)
            raise
        except Exception:
            self.log_execution(args, {}, time.time() - start, success=False)
            frappe.log_error(
                title=f"RAG Indexer Error [{self.name}]",
                message=frappe.get_traceback()
            )
            raise

    def log_execution(self, args: dict, result: dict, duration: float, success: bool):
        frappe.logger("frapperag").info(
            f"[RAG] {self.name} | success={success} | duration={duration:.2f}s"
        )
```

---

### `rag/lancedb_store.py` — LanceDB Wrapper

Rules enforced in this module:
- `lancedb` and `pyarrow` imported **inside every function** — never at module level.
- Path is always built from `frappe.get_site_path(...)` — never hardcoded.
- No module-level `db` object; no global state.

```python
import frappe

EMBEDDING_DIM = 768  # text-embedding-004 default output dimensions


def _get_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("id",            pa.string()),
        pa.field("doctype",       pa.string()),
        pa.field("name",          pa.string()),
        pa.field("text",          pa.string()),
        pa.field("vector",        pa.list_(pa.float32(), EMBEDDING_DIM)),
        pa.field("last_modified", pa.string()),
    ])


def get_store(doctype: str):
    """Open (or create) the LanceDB table for a DocType. Returns (db, table)."""
    import lancedb
    rag_path = frappe.get_site_path("private", "files", "rag")
    db = lancedb.connect(rag_path)
    table_name = "v1_" + doctype.lower().replace(" ", "_")  # v1_ prefix for schema versioning
    table = db.create_table(table_name, schema=_get_schema(), exist_ok=True)
    return db, table


def upsert_vectors(doctype: str, rows: list) -> None:
    """Upsert a batch of row dicts into the LanceDB table for doctype."""
    import lancedb  # noqa: imported here intentionally, not at module level
    _, table = get_store(doctype)
    (
        table.merge_insert("id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(rows)
    )
```

---

### `rag/text_converter.py` — Document to Text

```python
SUPPORTED_DOCTYPES = {"Sales Invoice", "Customer", "Item"}


def to_text(doctype: str, doc: dict) -> str | None:
    """
    Convert a Frappe document dict to a human-readable text summary.
    Returns None for unsupported doctypes (caller counts as skipped).
    """
    converters = {
        "Sales Invoice": _sales_invoice_text,
        "Customer":      _customer_text,
        "Item":          _item_text,
    }
    fn = converters.get(doctype)
    return fn(doc) if fn else None


def _sales_invoice_text(d: dict) -> str:
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Sales Invoice {d['name']} issued on {d.get('posting_date')} "
        f"to customer {d.get('customer')} ({d.get('customer_name')}). "
        f"Grand total: {d.get('grand_total')} {d.get('currency')}. "
        f"Status: {d.get('status')}. Due date: {d.get('due_date')}. "
        f"Items: {items or 'none'}. "
        f"Outstanding amount: {d.get('outstanding_amount')}."
    )


def _customer_text(d: dict) -> str:
    return (
        f"Customer {d.get('customer_name')} (ID: {d['name']}). "
        f"Type: {d.get('customer_type')}. "
        f"Customer group: {d.get('customer_group')}. "
        f"Territory: {d.get('territory')}. "
        f"Primary contact: {d.get('email_id') or 'not set'}. "
        f"Outstanding amount: {d.get('outstanding_amount', 0)}."
    )


def _item_text(d: dict) -> str:
    return (
        f"Item {d.get('item_name')} (code: {d['name']}). "
        f"Item group: {d.get('item_group')}. "
        f"Stock unit: {d.get('stock_uom')}. "
        f"Standard selling rate: {d.get('standard_rate', 0)}. "
        f"Description: {(d.get('description') or '').strip()[:500]}. "
        f"Is stock item: {d.get('is_stock_item')}."
    )
```

---

### `rag/embedder.py` — Gemini Embedding Caller

```python
EMBEDDING_MODEL   = "models/text-embedding-004"
BATCH_SIZE        = 20     # documents per API call
MAX_RETRIES       = 3
RETRY_BASE_DELAY  = 2.0    # seconds; doubled each retry (generic errors)
RATE_LIMIT_SLEEP  = 60.0   # seconds to wait on ResourceExhausted before retry


class EmbeddingError(Exception):
    """Raised when embedding generation fails unrecoverably."""


def embed_texts(texts: list, api_key: str) -> list:
    """
    Embed a list of texts using Gemini text-embedding-004.
    Returns a list of 768-dim float vectors in the same order as input.
    Imports google.generativeai inside this function -- never at module level.
    Raises EmbeddingError on unrecoverable failure.

    Rate-limit handling: ResourceExhausted is caught specifically and sleeps
    RATE_LIMIT_SLEEP (60s) before retrying that batch only. Other exceptions
    use exponential back-off (2s, 4s, 8s).
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    results = []

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]
        delay = RETRY_BASE_DELAY
        last_exc = None
        for attempt in range(MAX_RETRIES):
            try:
                response = genai.embed_content(
                    model=EMBEDDING_MODEL,
                    content=batch,
                    task_type="RETRIEVAL_DOCUMENT",
                )
                results.extend(response["embedding"])
                last_exc = None
                break
            except ResourceExhausted as exc:
                # Rate limit hit — wait 60 seconds before retrying this batch
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RATE_LIMIT_SLEEP)
                # No exponential back-off for rate limits; always 60s
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2
        if last_exc:
            raise EmbeddingError(
                f"Embedding failed after {MAX_RETRIES} attempts: {last_exc}"
            ) from last_exc

    return results
```

---

### `rag/indexer.py` — DocIndexerTool + Job Runner

Two responsibilities in one module:
1. `DocIndexerTool` — validates, checks permission, enqueues, returns job ID.
2. `run_indexing_job()` — the background job function (entry point for worker).
3. `mark_stalled_jobs()` — 30-minute scheduler cron function.

```python
import traceback
import frappe
from frappe.utils import now_datetime, add_to_date
from frapperag.rag.base_indexer import BaseIndexer

# Fields fetched for DocTypes that use frappe.db.get_all (no child tables needed)
FLAT_FIELDS_BY_DOCTYPE = {
    "Customer": [
        "name", "modified", "customer_name", "customer_type",
        "customer_group", "territory", "email_id", "outstanding_amount",
    ],
    "Item": [
        "name", "modified", "item_name", "item_group", "stock_uom",
        "standard_rate", "description", "is_stock_item",
    ],
}

# DocTypes that require frappe.get_doc (child table data needed for text conversion)
GET_DOC_DOCTYPES = {"Sales Invoice"}

WRITE_BATCH_SIZE = 20   # documents per embedding API call and LanceDB write


class DocIndexerTool(BaseIndexer):

    name = "rag_doc_indexer"

    def validate_arguments(self, args: dict) -> None:
        doctype = args.get("doctype", "").strip()
        if not doctype:
            frappe.throw("doctype is required", frappe.ValidationError)
        settings = frappe.get_doc("AI Assistant Settings")
        if not settings.is_enabled:
            frappe.throw("AI Assistant is disabled.", frappe.ValidationError)
        allowed = {r.doctype_name for r in settings.allowed_doctypes}
        if doctype not in allowed:
            frappe.throw(
                f"'{doctype}' is not in the allowed document types.",
                frappe.ValidationError,
            )
        running = frappe.db.exists(
            "AI Indexing Job",
            {"doctype_to_index": doctype, "status": ["in", ["Queued", "Running"]]},
        )
        if running:
            frappe.throw(
                f"An indexing job for '{doctype}' is already in progress.",
                frappe.ValidationError,
            )

    def check_permission(self, user: str) -> None:
        settings = frappe.get_doc("AI Assistant Settings")
        allowed_roles = {r.role for r in settings.allowed_roles}
        user_roles = set(frappe.get_roles(user))
        if not allowed_roles.intersection(user_roles):
            frappe.throw(
                "You do not have permission to trigger indexing.",
                frappe.PermissionError,
            )

    def execute(self, args: dict) -> dict:
        doctype = args["doctype"].strip()
        user    = args["user"]

        job_doc = frappe.get_doc({
            "doctype":         "AI Indexing Job",
            "doctype_to_index": doctype,
            "status":          "Queued",
            "triggered_by":    user,
        })
        job_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # Fix 4: api_key NOT passed via enqueue kwargs — read inside the job from Settings.
        # This keeps the key out of Redis and is more secure.
        queue_job = frappe.enqueue(
            "frapperag.rag.indexer.run_indexing_job",
            queue="long",
            timeout=7200,
            job_name=f"rag_index_{doctype.lower().replace(' ', '_')}",
            site=frappe.local.site,        # changai pattern: explicit site
            job_id=job_doc.name,
            doctype=doctype,
            user=user,
        )

        queue_id = getattr(queue_job, "id", None) or "local"
        job_doc.db_set("queue_job_id", queue_id)
        frappe.db.commit()

        return {"job_id": job_doc.name, "status": "Queued"}


def run_indexing_job(job_id: str, doctype: str, user: str, **kwargs):
    """
    Background job entry point. Site context is already initialised by the Frappe worker.
    All heavy imports happen inside this function -- never at module level.

    Fix 4: api_key is read here from AI Assistant Settings, not passed via enqueue.
    Fix 1: Sales Invoice uses frappe.get_doc to fetch child items;
           Customer and Item use frappe.db.get_all (no child tables needed).
    Fix 5: tokens_used accumulated after each embed_texts() call (chars // 4 estimate).
    """
    from frapperag.rag.lancedb_store import upsert_vectors
    from frapperag.rag.text_converter import to_text
    from frapperag.rag.embedder import embed_texts, EmbeddingError

    # Fix 4: Read api_key from Settings inside the job — not passed through Redis
    api_key = frappe.get_doc("AI Assistant Settings").get_password("gemini_api_key")

    # Enforce triggering user's permission context (Principle III)
    frappe.set_user(user)

    job = frappe.get_doc("AI Indexing Job", job_id)
    job.status               = "Running"
    job.start_time           = now_datetime()
    job.last_progress_update = now_datetime()
    job.save(ignore_permissions=True)
    frappe.db.commit()
    _publish(job, user)

    try:
        # Fix 1: Use frappe.db.get_all for flat DocTypes; frappe.get_doc for Sales Invoice
        if doctype in GET_DOC_DOCTYPES:
            # Sales Invoice: fetch names only, then get_doc per record for child items
            name_list = frappe.db.get_all(doctype, fields=["name", "modified"],
                                          ignore_permissions=False)
        else:
            flat_fields = FLAT_FIELDS_BY_DOCTYPE.get(doctype, ["name", "modified"])
            name_list = frappe.db.get_all(doctype, fields=flat_fields,
                                          ignore_permissions=False)

        job.total_records = len(name_list)
        job.save(ignore_permissions=True)
        frappe.db.commit()

        pending_docs  = []
        pending_texts = []

        for idx, rec in enumerate(name_list):
            # Permission check per record (Principle III)
            if not frappe.has_permission(doctype, doc=rec["name"], ptype="read", user=user):
                job.skipped_records += 1
                continue

            # Fix 1: Get full doc (with child tables) only for Sales Invoice
            if doctype in GET_DOC_DOCTYPES:
                doc_data = frappe.get_doc(doctype, rec["name"]).as_dict()
            else:
                doc_data = rec

            text = to_text(doctype, doc_data)
            if text is None:
                job.skipped_records += 1
                continue

            pending_docs.append(rec)
            pending_texts.append(text)

            is_last = (idx == len(name_list) - 1)
            if len(pending_texts) >= WRITE_BATCH_SIZE or (is_last and pending_texts):
                try:
                    vectors = embed_texts(pending_texts, api_key)
                    rows = [
                        {
                            "id":            f"{doctype}:{r['name']}",
                            "doctype":       doctype,
                            "name":          r["name"],
                            "text":          t,
                            "vector":        v,
                            "last_modified": str(r.get("modified", "")),
                        }
                        for r, t, v in zip(pending_docs, pending_texts, vectors)
                    ]
                    upsert_vectors(doctype, rows)
                    job.processed_records += len(rows)
                    # Fix 5: Accumulate estimated token usage (chars // 4 per text)
                    job.tokens_used += sum(len(t) // 4 for t in pending_texts)

                except EmbeddingError as exc:
                    # Fatal: Gemini API unrecoverable — abort the job
                    job.status       = "Failed"
                    job.error_detail = str(exc)
                    job.end_time     = now_datetime()
                    job.save(ignore_permissions=True)
                    frappe.db.commit()
                    _publish(job, user, error=str(exc))
                    return

                except Exception as exc:
                    # Soft batch failure — count and continue
                    job.failed_records += len(pending_texts)
                    job.error_detail    = (
                        (job.error_detail or "")
                        + f"\nBatch error near record {idx}: {exc}"
                    )

                finally:
                    pending_docs  = []
                    pending_texts = []

                # Progress update after each batch
                done = job.processed_records + job.skipped_records + job.failed_records
                total = job.total_records or 1
                job.progress_percent     = round((done / total) * 100, 1)
                job.last_progress_update = now_datetime()
                job.save(ignore_permissions=True)
                frappe.db.commit()
                _publish(job, user)

        job.status          = "Completed with Errors" if job.failed_records else "Completed"
        job.progress_percent = 100.0
        job.end_time         = now_datetime()
        job.save(ignore_permissions=True)
        frappe.db.commit()
        _publish(job, user)

    except Exception:
        tb = traceback.format_exc()
        job.status       = "Failed"
        job.error_detail = tb
        job.end_time     = now_datetime()
        job.save(ignore_permissions=True)
        frappe.db.commit()
        _publish(job, user, error=tb)
        frappe.log_error(
            title=f"RAG Indexing Job Failed [{job_id}]",
            message=tb,
        )


def _publish(job, user: str, error: str = None):
    msg = {
        "job_id":            job.name,
        "status":            job.status,
        "progress_percent":  job.progress_percent,
        "processed_records": job.processed_records,
        "total_records":     job.total_records,
        "skipped_records":   job.skipped_records,
        "failed_records":    job.failed_records,
    }
    if error:
        msg["error"] = error[:2000]   # cap error string length
    event = "rag_index_error" if error else "rag_index_progress"
    frappe.publish_realtime(event=event, message=msg, user=user, after_commit=False)


def mark_stalled_jobs():
    """Scheduler (every 30 min): transition Running jobs with no recent update."""
    cutoff = add_to_date(now_datetime(), hours=-2)
    stalled = frappe.db.get_all(
        "AI Indexing Job",
        filters={"status": "Running", "last_progress_update": ["<", cutoff]},
        pluck="name",
    )
    for job_name in stalled:
        frappe.db.set_value(
            "AI Indexing Job",
            job_name,
            {
                "status":       "Failed (Stalled)",
                "error_detail": "Job exceeded 2-hour progress timeout. Worker may have crashed.",
                "end_time":     now_datetime(),
            },
        )
    if stalled:
        frappe.db.commit()
```

---

### `api/indexer.py` — Whitelisted HTTP Endpoints

```python
import frappe
from frapperag.rag.indexer import DocIndexerTool


@frappe.whitelist()
def trigger_indexing(doctype: str) -> dict:
    """
    Enqueue background indexing job. Returns job_id immediately.
    All embedding/IO is inside the background worker.
    """
    tool = DocIndexerTool()
    return tool.safe_execute(
        args={"doctype": doctype, "user": frappe.session.user},
        user=frappe.session.user,
    )


@frappe.whitelist()
def get_job_status(job_id: str) -> dict:
    if not frappe.db.exists("AI Indexing Job", job_id):
        frappe.throw(f"AI Indexing Job '{job_id}' not found.", frappe.DoesNotExistError)
    frappe.has_permission("AI Indexing Job", throw=True)
    job = frappe.get_doc("AI Indexing Job", job_id)
    return {
        "job_id":            job.name,
        "doctype_to_index":  job.doctype_to_index,
        "status":            job.status,
        "progress_percent":  job.progress_percent,
        "total_records":     job.total_records,
        "processed_records": job.processed_records,
        "skipped_records":   job.skipped_records,
        "failed_records":    job.failed_records,
        "start_time":        str(job.start_time) if job.start_time else None,
        "end_time":          str(job.end_time)   if job.end_time   else None,
        "error_detail":      job.error_detail,
    }


@frappe.whitelist()
def list_jobs(limit: int = 20, page: int = 1) -> dict:
    frappe.has_permission("AI Indexing Job", throw=True)
    offset = (int(page) - 1) * int(limit)
    jobs = frappe.db.get_all(
        "AI Indexing Job",
        fields=[
            "name", "doctype_to_index", "status", "progress_percent",
            "total_records", "processed_records", "failed_records",
            "triggered_by", "start_time", "end_time",
        ],
        order_by="creation desc",
        limit=int(limit),
        start=offset,
        ignore_permissions=False,
    )
    total = frappe.db.count("AI Indexing Job")
    return {
        "jobs":  [dict(j, job_id=j.name) for j in jobs],
        "total": total,
        "page":  int(page),
    }
```

---

### Vanilla JS Admin Page (Frappe Desk Page)

**File**: `frapperag/frapperag/page/rag_admin/rag_admin.js`
**Page name**: `rag-admin`

```javascript
frappe.pages["rag-admin"].on_page_load = function(wrapper) {
    var page = frappe.ui.make_app_page({
        parent: wrapper,
        title: "RAG Index Manager",
        single_column: true,
    });

    // DocType selector + trigger button
    var $form = $(`
        <div class="rag-admin-form" style="padding: 20px;">
            <div class="form-group">
                <label>Document Type</label>
                <select id="rag-doctype-select" class="form-control" style="max-width:300px;">
                    <option value="">-- select --</option>
                </select>
            </div>
            <button id="rag-trigger-btn" class="btn btn-primary">Start Indexing</button>
            <div id="rag-job-status" style="margin-top:20px; display:none;">
                <p><strong>Job:</strong> <span id="rag-job-id"></span></p>
                <p><strong>Status:</strong> <span id="rag-status"></span></p>
                <div class="progress" style="max-width:400px;">
                    <div id="rag-progress-bar" class="progress-bar" role="progressbar"
                         style="width:0%">0%</div>
                </div>
                <p style="margin-top:8px; font-size:12px; color:#888;">
                    <span id="rag-counts"></span>
                </p>
            </div>
            <div id="rag-job-list" style="margin-top:30px;"></div>
        </div>
    `).appendTo(page.main);

    // Load allowed doctypes from settings
    frappe.call({
        method: "frappe.client.get",
        args: { doctype: "AI Assistant Settings" },
        callback: function(r) {
            var allowed = (r.message.allowed_doctypes || []).map(d => d.doctype_name);
            allowed.forEach(function(dt) {
                $("#rag-doctype-select").append(
                    $("<option>").val(dt).text(dt)
                );
            });
        }
    });

    // Trigger indexing
    var current_job_id = null;

    $("#rag-trigger-btn").on("click", function() {
        var doctype = $("#rag-doctype-select").val();
        if (!doctype) { frappe.msgprint("Please select a document type."); return; }

        $(this).prop("disabled", true).text("Starting...");

        frappe.call({
            method: "frapperag.api.indexer.trigger_indexing",
            args: { doctype: doctype },
            callback: function(r) {
                current_job_id = r.message.job_id;
                $("#rag-job-id").text(current_job_id);
                $("#rag-status").text(r.message.status);
                $("#rag-job-status").show();
                $("#rag-trigger-btn").prop("disabled", false).text("Start Indexing");
                subscribe_to_progress();
            },
            error: function() {
                $("#rag-trigger-btn").prop("disabled", false).text("Start Indexing");
            }
        });
    });

    function subscribe_to_progress() {
        var terminal = ["Completed", "Completed with Errors", "Failed", "Failed (Stalled)"];

        frappe.realtime.on("rag_index_progress", function(data) {
            if (data.job_id !== current_job_id) return;
            update_ui(data);
            if (terminal.includes(data.status)) {
                frappe.realtime.off("rag_index_progress");
                frappe.realtime.off("rag_index_error");
                load_job_list();
            }
        });

        frappe.realtime.on("rag_index_error", function(data) {
            if (data.job_id !== current_job_id) return;
            update_ui(data);
            frappe.msgprint({ message: data.error || "Indexing failed.", indicator: "red" });
            frappe.realtime.off("rag_index_progress");
            frappe.realtime.off("rag_index_error");
            load_job_list();
        });
    }

    function update_ui(data) {
        var pct = (data.progress_percent || 0).toFixed(1);
        $("#rag-status").text(data.status);
        $("#rag-progress-bar").css("width", pct + "%").text(pct + "%");
        $("#rag-counts").text(
            "Processed: " + data.processed_records +
            " | Skipped: " + data.skipped_records +
            " | Failed: " + data.failed_records +
            " / Total: " + data.total_records
        );
    }

    function load_job_list() {
        frappe.call({
            method: "frapperag.api.indexer.list_jobs",
            args: { limit: 10, page: 1 },
            callback: function(r) {
                var rows = (r.message.jobs || []).map(function(j) {
                    return "<tr>" +
                        "<td>" + j.job_id + "</td>" +
                        "<td>" + j.doctype_to_index + "</td>" +
                        "<td>" + j.status + "</td>" +
                        "<td>" + (j.processed_records || 0) + "/" + (j.total_records || 0) + "</td>" +
                        "<td>" + (j.start_time || "") + "</td>" +
                    "</tr>";
                }).join("");
                $("#rag-job-list").html(
                    "<h5>Recent Jobs</h5>" +
                    "<table class='table table-bordered'>" +
                        "<thead><tr><th>Job ID</th><th>DocType</th><th>Status</th><th>Records</th><th>Started</th></tr></thead>" +
                        "<tbody>" + rows + "</tbody>" +
                    "</table>"
                );
            }
        });
    }

    load_job_list();
};
```

---

## Complexity Tracking

> No constitution violations requiring justification.

| Potential Concern | Resolution |
|---|---|
| `frappe.set_user(user)` inside job — cross-job contamination? | Each Frappe worker process handles one job at a time (RQ semantics). `set_user` is process-local state, not shared across concurrent workers. |
| Sales Invoice child items require `frappe.get_doc` (not flat `get_all`) | `GET_DOC_DOCTYPES = {"Sales Invoice"}` routes Sales Invoice records to `frappe.get_doc(doctype, name).as_dict()` to capture child `items` rows. Customer and Item use `frappe.db.get_all` with flat field lists (no child tables needed). |
| `api_key` security in background jobs | `api_key` is **not** passed via `frappe.enqueue` kwargs (which serialise to Redis). It is read from `AI Assistant Settings` at the start of `run_indexing_job()`, keeping the credential out of the queue. |
| Duplicate trigger race condition | `validate_arguments` calls `frappe.db.exists` for any job in `["Queued", "Running"]` before creating a new one. Duplicate is rejected immediately with `ValidationError`; no second job is created or queued (FR-009, clarified 2026-03-15). |
| Re-index data safety (FR-023) | LanceDB `merge_insert("id")` upserts by composite key `"{doctype}:{name}"`. Existing vectors are updated in place; new ones are inserted. The table is never dropped. Documents deleted from Frappe remain in the index until Phase 2. |

---

## Post-Design Constitution Check

Re-verified after all modules designed and spec clarifications applied (2026-03-15):

| Principle | Status |
|---|---|
| I. Frappe-Native Architecture | PASS — 4 DocTypes as JSON fixtures; 3 whitelist methods; hooks.py only |
| II. Per-Client Data Isolation | PASS — `frappe.get_site_path()` only; `site=frappe.local.site` in enqueue; all imports inside job |
| III. Permission-Aware RAG Retrieval | PASS — `frappe.set_user(user)` + per-record `frappe.has_permission()`; skipped ≠ failed |
| IV. Zero External Infrastructure | PASS — `lancedb`, `pyarrow`, `google-generativeai` only |
| V. Asynchronous-by-Default | PASS — whitelist method creates record + enqueues + returns job_id; zero blocking I/O |
| VI. Zero-Friction Installation | PASS — `after_install` creates dir; fixtures ship roles; no manual steps beyond API key |
| VII. No Automated Tests | PASS — no test files, no pytest, no test tasks |

### Spec Clarifications Applied (2026-03-15)

| Clarification | Impact on Plan |
|---|---|
| FR-009: Duplicate trigger → reject immediately (no queuing) | `validate_arguments` already raises `ValidationError` on `frappe.db.exists` match; no change needed |
| `Failed (Stalled)` canonical terminal state (not "Interrupted") | `mark_stalled_jobs()` already sets `status="Failed (Stalled)"`; no change needed |
| FR-019: Stalled detection applies to `Running` only — `Queued` exempt | `mark_stalled_jobs()` already filters `status="Running"` only; no change needed |
| FR-023: Re-index uses upsert by document ID; table never dropped | `merge_insert("id")` in `upsert_vectors()` already implements this; no change needed; FR-023 documented in Complexity Tracking |
