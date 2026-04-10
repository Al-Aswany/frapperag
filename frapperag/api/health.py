"""Admin endpoint — returns current system health snapshot for the /app/rag-health page.

Role-gated: System Manager or RAG Admin only.
Reads the `RAG System Health` Single DocType (written every ~1 minute by the
scheduler) plus 24-hour failure counts from AI Indexing Job and Chat Message.
"""

import frappe
from frappe.utils import add_to_date, now_datetime


@frappe.whitelist()
def get_health_status() -> dict:
    """Return a single health payload consumed by rag_health.js.

    Returns:
        sidecar_status          — "Reachable" | "Unreachable" | "Unknown"
        sidecar_response_time_ms — int milliseconds
        last_checked            — ISO datetime string or None
        indexing_failures       — list of {doctype, count} for last 24 h
        chat_failures_24h       — int
        gemini_last_success     — ISO datetime string or None
        gemini_last_failure     — {timestamp, reason} or None
    """
    allowed_roles = {"System Manager", "RAG Admin"}
    user_roles = set(frappe.get_roles(frappe.session.user))
    if allowed_roles.isdisjoint(user_roles):
        frappe.throw("Not permitted", frappe.PermissionError)

    # Sidecar health snapshot (Single DocType — Frappe caches Single reads)
    health = frappe.get_doc("RAG System Health")

    cutoff = add_to_date(now_datetime(), hours=-24)

    # Indexing failures last 24 h grouped by DocType
    indexing_rows = frappe.db.sql(
        """SELECT doctype_to_index AS doctype, COUNT(*) AS cnt
           FROM `tabAI Indexing Job`
           WHERE status = 'Failed' AND creation >= %s
           GROUP BY doctype_to_index""",
        [cutoff],
        as_dict=True,
    )
    indexing_failures = [{"doctype": r.doctype, "count": r.cnt} for r in indexing_rows]

    # Chat failures last 24 h
    chat_failures_24h = (
        frappe.db.sql(
            """SELECT COUNT(*) FROM `tabChat Message`
               WHERE status = 'Failed' AND creation >= %s""",
            [cutoff],
        )[0][0]
        or 0
    )

    # Last completed indexing job timestamp (approximates last Gemini success)
    gemini_last_success_row = frappe.db.sql(
        """SELECT MAX(end_time) FROM `tabAI Indexing Job`
           WHERE status IN ('Completed', 'Completed with Errors')"""
    )
    gemini_last_success = (
        str(gemini_last_success_row[0][0]) if gemini_last_success_row and gemini_last_success_row[0][0] else None
    )

    # Most recent failed indexing job with failure reason
    gemini_last_failure_row = frappe.db.sql(
        """SELECT end_time, failure_reason FROM `tabAI Indexing Job`
           WHERE status = 'Failed'
           ORDER BY end_time DESC LIMIT 1""",
        as_dict=True,
    )
    gemini_last_failure = None
    if gemini_last_failure_row:
        gemini_last_failure = {
            "timestamp": str(gemini_last_failure_row[0].end_time) if gemini_last_failure_row[0].end_time else None,
            "reason": gemini_last_failure_row[0].failure_reason or "",
        }

    return {
        "sidecar_status":           health.sidecar_status or "Unknown",
        "sidecar_response_time_ms": health.sidecar_response_time_ms or 0,
        "last_checked":             str(health.last_checked) if health.last_checked else None,
        "indexing_failures":        indexing_failures,
        "chat_failures_24h":        chat_failures_24h,
        "gemini_last_success":      gemini_last_success,
        "gemini_last_failure":      gemini_last_failure,
    }
