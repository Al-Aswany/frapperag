"""
DocIndexerTool — validates, checks permission, enqueues job, returns job ID.
run_indexing_job() — background worker entry point.
mark_stalled_jobs() — 30-minute scheduler cron.
"""

import traceback

import frappe
from frappe.utils import now_datetime, add_to_date


def _log():
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger

from frapperag.rag.base_indexer import BaseIndexer

# Fields fetched for DocTypes that use frappe.db.get_all (no child tables needed)
FLAT_FIELDS_BY_DOCTYPE = {
    "Customer": [
        "name", "modified", "customer_name", "customer_type",
        "customer_group", "territory", "email_id",
    ],
    "Item": [
        "name", "modified", "item_name", "item_group", "stock_uom",
        "standard_rate", "description", "is_stock_item",
    ],
    "Item Price": [
        "name", "modified", "item_code", "item_name", "price_list",
        "price_list_rate", "currency", "valid_from", "valid_upto",
    ],
    "Supplier": [
        "name", "modified", "supplier_name", "supplier_type",
        "supplier_group", "country", "email_id", "disabled",
    ],
}

# DocTypes that require frappe.get_doc (child table data needed for text conversion)
GET_DOC_DOCTYPES = {
    "Sales Invoice", "Purchase Invoice", "Purchase Order", "Purchase Receipt",
    "Delivery Note", "Sales Order", "Stock Entry",
}

WRITE_BATCH_SIZE = 20   # documents per embedding API call and LanceDB write


class DocIndexerTool(BaseIndexer):

    name = "rag_doc_indexer"

    def validate_arguments(self, args: dict) -> None:
        doctype = args.get("doctype", "").strip()
        if not doctype:
            frappe.throw("doctype is required.", frappe.ValidationError)

        settings = frappe.get_doc("AI Assistant Settings")
        if not settings.is_enabled:
            frappe.throw(
                "AI Assistant is disabled. Enable it in AI Assistant Settings.",
                frappe.ValidationError,
            )

        allowed = {r.doctype_name for r in settings.allowed_doctypes}
        if doctype not in allowed:
            frappe.throw(
                f"Document type '{doctype}' is not in the allowed list.",
                frappe.ValidationError,
            )

        # FR-009: reject immediately if a job for this DocType is already active
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
            "doctype":          "AI Indexing Job",
            "doctype_to_index": doctype,
            "status":           "Queued",
            "triggered_by":     user,
        })
        job_doc.insert(ignore_permissions=True)
        frappe.db.commit()

        # api_key is NOT passed via enqueue kwargs — it is read inside the job
        # from AI Assistant Settings to keep the credential out of Redis.
        queue_job = frappe.enqueue(
            "frapperag.rag.indexer.run_indexing_job",
            queue="long",
            timeout=7200,
            job_name=f"rag_index_{doctype.lower().replace(' ', '_')}",
            site=frappe.local.site,   # changai pattern: explicit site
            indexing_job_id=job_doc.name,
            doctype=doctype,
            user=user,
        )

        queue_id = getattr(queue_job, "id", None) or "local"
        job_doc.db_set("queue_job_id", queue_id)
        frappe.db.commit()

        return {"job_id": job_doc.name, "status": "Queued"}


def run_indexing_job(indexing_job_id: str, doctype: str, user: str, **kwargs):
    """Background job entry point. Site context is already initialised by the worker.

    All heavy imports happen inside this function — never at module level
    (Principle II: per-client isolation).

    Key implementation notes:
    - Embedding and vector storage go exclusively through the RAG sidecar HTTP API
      (sidecar_client.upsert_record). Workers never import lancedb or
      sentence_transformers directly (Constitution Principle IV).
    - Sales Invoice uses frappe.get_doc to capture child items table.
    - Customer and Item use frappe.db.get_all with flat field lists (no child tables).
    - Stalled detection applies to Running jobs only; Queued jobs are exempt (FR-019).
    """
    job_id = indexing_job_id
    from frapperag.rag.text_converter import to_text
    from frapperag.rag.sidecar_client import (
        upsert_record, SidecarError, SidecarUnavailableError, SidecarPermanentError
    )

    _log().info(f"[JOB_START] job_id={job_id} doctype={doctype} user={user}")

    # Enforce the triggering user's permission context (Principle III)
    frappe.set_user(user)

    job = frappe.get_doc("AI Indexing Job", job_id)
    job.status               = "Running"
    job.start_time           = now_datetime()
    job.last_progress_update = now_datetime()
    job.save(ignore_permissions=True)
    frappe.db.commit()
    _publish(job, user)

    try:
        # Fetch records — get_doc for Sales Invoice (child items needed),
        # get_all with flat fields for Customer and Item.
        if doctype in GET_DOC_DOCTYPES:
            name_list = frappe.db.get_all(
                doctype, fields=["name", "modified"], ignore_permissions=False
            )
        else:
            flat_fields = FLAT_FIELDS_BY_DOCTYPE.get(doctype, ["name", "modified"])
            name_list = frappe.db.get_all(
                doctype, fields=flat_fields, ignore_permissions=False
            )

        job.total_records = len(name_list)
        job.save(ignore_permissions=True)
        frappe.db.commit()

        batch_count = 0  # track records processed since last progress update

        for idx, rec in enumerate(name_list):
            # Per-record permission check (Principle III): skipped ≠ failed
            if not frappe.has_permission(
                doctype, doc=rec["name"], ptype="read", user=user
            ):
                job.skipped_records += 1
                batch_count += 1
                continue

            # Full doc (with child tables) only for Sales Invoice
            if doctype in GET_DOC_DOCTYPES:
                doc_data = frappe.get_doc(doctype, rec["name"]).as_dict()
            else:
                doc_data = rec

            text = to_text(doctype, doc_data)
            if text is None:
                job.skipped_records += 1
                batch_count += 1
                continue

            try:
                # Embed + upsert via sidecar — single HTTP call per record.
                # The sidecar applies "passage: " prefix and writes to the v4_ table.
                upsert_record(doctype, rec["name"], text)
                job.processed_records += 1

            except SidecarUnavailableError as exc:
                # Transient errors exhausted all retries — abort immediately.
                job.status         = "Failed"
                job.error_detail   = str(exc)
                job.failure_reason = "Sidecar unavailable"
                job.end_time       = now_datetime()
                job.save(ignore_permissions=True)
                frappe.db.commit()
                _publish(job, user, error=str(exc))
                _log().warning(f"[JOB_FAIL] job_id={job_id} failure_reason=Sidecar unavailable")
                return

            except SidecarPermanentError as exc:
                # Permanent 4xx client error — abort immediately.
                sc_suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
                failure_reason = f"Sidecar error{sc_suffix}"[:140]
                job.status         = "Failed"
                job.error_detail   = str(exc)
                job.failure_reason = failure_reason
                job.end_time       = now_datetime()
                job.save(ignore_permissions=True)
                frappe.db.commit()
                _publish(job, user, error=str(exc))
                _log().warning(f"[JOB_FAIL] job_id={job_id} failure_reason={failure_reason}")
                return

            except SidecarError as exc:
                # Other non-2xx sidecar response — abort immediately.
                job.status         = "Failed"
                job.error_detail   = str(exc)
                job.failure_reason = "Sidecar unavailable"
                job.end_time       = now_datetime()
                job.save(ignore_permissions=True)
                frappe.db.commit()
                _publish(job, user, error=str(exc))
                _log().warning(f"[JOB_FAIL] job_id={job_id} failure_reason=Sidecar unavailable")
                return

            except Exception as exc:
                # Soft per-record failure — count as failed and continue (FR-015).
                job.failed_records += 1
                job.error_detail = (
                    (job.error_detail or "")
                    + f"\nRecord {rec['name']}: {exc}"
                )

            batch_count += 1

            # Progress update after each batch (FR-014)
            if batch_count >= WRITE_BATCH_SIZE or (idx == len(name_list) - 1):
                done  = job.processed_records + job.skipped_records + job.failed_records
                total = job.total_records or 1
                job.progress_percent     = round((done / total) * 100, 1)
                job.last_progress_update = now_datetime()
                job.save(ignore_permissions=True)
                frappe.db.commit()
                _publish(job, user)
                batch_count = 0

        job.status           = "Completed with Errors" if job.failed_records else "Completed"
        job.progress_percent = 100.0
        job.end_time         = now_datetime()
        job.save(ignore_permissions=True)
        frappe.db.commit()
        _publish(job, user)
        _log().info(f"[JOB_SUCCESS] job_id={job_id} status={job.status} processed={job.processed_records} failed={job.failed_records}")

    except Exception:
        tb = traceback.format_exc()
        failure_reason = "Unknown error"
        if "quota" in tb.lower() or "429" in tb:
            failure_reason = "Gemini quota exceeded"
        job.status         = "Failed"
        job.error_detail   = tb
        job.failure_reason = failure_reason[:140]
        job.end_time       = now_datetime()
        job.save(ignore_permissions=True)
        frappe.db.commit()
        _publish(job, user, error=tb)
        _log().warning(f"[JOB_FAIL] job_id={job_id} failure_reason={failure_reason}")
        frappe.log_error(
            title=f"RAG Indexing Job Failed [{job_id}]",
            message=tb,
        )


def _publish(job, user: str, error: str = None) -> None:
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
        msg["error"] = error[:2000]  # cap to avoid oversized realtime payloads
    event = "rag_index_error" if error else "rag_index_progress"
    frappe.publish_realtime(event=event, message=msg, user=user, after_commit=False)


def mark_stalled_jobs() -> None:
    """Scheduler (every 30 min): transition Running jobs with no recent update.

    FR-019: Only Running jobs are checked. Queued jobs are exempt — they are
    waiting for a worker slot and have made no progress commitment yet.
    """
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
                "status":         "Failed (Stalled)",
                "failure_reason": "Response timed out",
                "error_detail":   (
                    "Job exceeded 2-hour progress timeout. "
                    "Worker may have crashed."
                ),
                "end_time":       now_datetime(),
            },
        )
    if stalled:
        frappe.db.commit()
