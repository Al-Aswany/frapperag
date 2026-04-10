"""Scheduler health check — runs every ~1 minute via scheduler_events["all"].

Writes the result to the `RAG System Health` Single DocType so the admin
health page can read it without making a blocking HTTP call on page load.
"""

import time

import frappe


def run_health_check() -> None:
    """Check sidecar reachability and record the result in RAG System Health.

    Wrapped in a broad try/except so that any unexpected error cannot crash
    the Frappe scheduler process.
    """
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")

    try:
        import httpx

        try:
            port = frappe.get_doc("AI Assistant Settings").sidecar_port
            port = int(port) if port else 8100
        except Exception:
            port = 8100

        t0 = time.time()
        status = "Unreachable"
        response_time_ms = 0

        try:
            r = httpx.get(f"http://localhost:{port}/health", timeout=5.0)
            response_time_ms = int((time.time() - t0) * 1000)
            status = "Reachable" if r.status_code == 200 else "Unreachable"
        except (httpx.ConnectError, httpx.TimeoutException):
            response_time_ms = int((time.time() - t0) * 1000)
            status = "Unreachable"

        doc = frappe.get_doc("RAG System Health")
        doc.last_checked = frappe.utils.now_datetime()
        doc.sidecar_status = status
        doc.sidecar_response_time_ms = response_time_ms
        doc.save(ignore_permissions=True)
        frappe.db.commit()

        logger.info(f"[HEALTH_CHECK] status={status} response_time_ms={response_time_ms}")

    except Exception as exc:
        logger.error(f"[HEALTH_CHECK] error={exc}")
