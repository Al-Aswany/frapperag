import frappe
from frapperag.rag.indexer import DocIndexerTool


@frappe.whitelist()
def trigger_indexing(doctype: str) -> dict:
    """Enqueue a background indexing job. Returns job_id immediately.

    All embedding, LanceDB writes, and document reads happen inside the
    background worker — zero blocking I/O in this HTTP handler (Principle V).
    """
    tool = DocIndexerTool()
    return tool.safe_execute(
        args={"doctype": doctype, "user": frappe.session.user},
        user=frappe.session.user,
    )


@frappe.whitelist()
def get_job_status(job_id: str) -> dict:
    """Return current status and progress for a single AI Indexing Job."""
    if not frappe.db.exists("AI Indexing Job", job_id):
        frappe.throw(
            f"AI Indexing Job '{job_id}' not found.",
            frappe.DoesNotExistError,
        )
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
    """Return a paginated list of AI Indexing Jobs, newest first."""
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
