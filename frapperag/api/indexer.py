import frappe
from frappe.utils import now_datetime, add_to_date
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


def _require_rag_admin():
    """Raise PermissionError unless caller has RAG Admin or System Manager role."""
    roles = set(frappe.get_roles())
    if not (roles & {"RAG Admin", "System Manager"}):
        frappe.throw(
            "You do not have permission to access sync health data.",
            frappe.PermissionError,
        )


@frappe.whitelist()
def trigger_full_index() -> dict:
    """Enqueue a background indexing job for every allowed DocType.

    DocTypes that already have a Queued or Running job are skipped (not an error).
    Returns a summary of queued and skipped DocTypes.
    """
    tool = DocIndexerTool()
    tool.check_permission(frappe.session.user)

    settings = frappe.get_doc("AI Assistant Settings")
    if not settings.is_enabled:
        frappe.throw(
            "AI Assistant is disabled. Enable it in AI Assistant Settings.",
            frappe.ValidationError,
        )

    allowed = [r.doctype_name for r in (settings.allowed_doctypes or [])]
    if not allowed:
        frappe.throw("No allowed DocTypes configured.", frappe.ValidationError)

    queued = []
    skipped = []

    for doctype in allowed:
        active = frappe.db.exists(
            "AI Indexing Job",
            {"doctype_to_index": doctype, "status": ["in", ["Queued", "Running"]]},
        )
        if active:
            skipped.append(doctype)
            continue
        result = tool.execute({"doctype": doctype, "user": frappe.session.user})
        queued.append({"doctype": doctype, "job_id": result["job_id"]})

    return {"queued": queued, "skipped": skipped}


@frappe.whitelist()
def get_sync_health() -> dict:
    """Return per-DocType sync success/failure counts (last 24 h) and failed entries list."""
    _require_rag_admin()

    cutoff = add_to_date(now_datetime(), hours=-24)

    # Per-DocType outcome counts within the last 24 hours
    rows = frappe.db.get_all(
        "Sync Event Log",
        filters={"creation": [">=", cutoff]},
        fields=["doctype_name", "outcome", "count(name) as cnt", "max(creation) as last_seen"],
        group_by="doctype_name, outcome",
        order_by=None,
        ignore_permissions=True,
    )

    # Build summary keyed by doctype_name
    summary_map: dict = {}
    for row in rows:
        dt = row.doctype_name
        if dt not in summary_map:
            summary_map[dt] = {"doctype_name": dt, "success_count": 0, "failed_count": 0, "last_success": None}
        if row.outcome == "Success":
            summary_map[dt]["success_count"] = row.cnt
            summary_map[dt]["last_success"] = str(row.last_seen) if row.last_seen else None
        elif row.outcome == "Failed":
            summary_map[dt]["failed_count"] = row.cnt

    # All-time failures (limit 100, newest first)
    failures_raw = frappe.db.get_all(
        "Sync Event Log",
        filters={"outcome": "Failed"},
        fields=["name", "doctype_name", "record_name", "trigger_type", "error_message", "creation"],
        order_by="creation desc",
        limit=100,
        ignore_permissions=True,
    )
    failures = [
        {
            "sync_log_id":   f.name,
            "doctype_name":  f.doctype_name,
            "record_name":   f.record_name,
            "trigger_type":  f.trigger_type,
            "error_message": f.error_message,
            "creation":      str(f.creation),
        }
        for f in failures_raw
    ]

    return {"summary": list(summary_map.values()), "failures": failures}


@frappe.whitelist()
def retry_sync(sync_log_id: str) -> dict:
    """Re-queue a failed sync attempt. Creates a new Sync Event Log entry; original preserved."""
    _require_rag_admin()

    if not frappe.db.exists("Sync Event Log", sync_log_id):
        frappe.throw(
            f"Sync Event Log '{sync_log_id}' not found.",
            frappe.ValidationError,
        )

    original = frappe.get_doc("Sync Event Log", sync_log_id)
    if original.outcome != "Failed":
        frappe.throw(
            f"Sync Event Log '{sync_log_id}' is not in Failed state (current: {original.outcome}).",
            frappe.ValidationError,
        )

    new_log = frappe.get_doc({
        "doctype": "Sync Event Log",
        "doctype_name": original.doctype_name,
        "record_name": original.record_name,
        "trigger_type": "Retry",
        "outcome": "Queued",
    })
    new_log.insert(ignore_permissions=True)
    frappe.db.commit()

    if original.trigger_type == "Purge":
        frappe.enqueue(
            "frapperag.rag.sync_runner.run_purge_job",
            queue="short",
            timeout=120,
            site=frappe.local.site,
            sync_log_id=new_log.name,
            doctype=original.doctype_name,
            user=frappe.session.user,
        )
    else:
        frappe.enqueue(
            "frapperag.rag.sync_runner.run_sync_job",
            queue="short",
            timeout=120,
            site=frappe.local.site,
            sync_log_id=new_log.name,
            doctype=original.doctype_name,
            name=original.record_name,
            trigger_type="Retry",
            user=frappe.session.user,
        )

    return {"sync_log_id": new_log.name, "status": "Queued"}
