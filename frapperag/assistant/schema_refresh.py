from __future__ import annotations

import frappe

from frapperag.assistant.schema_catalog import build_schema_catalog, write_schema_catalog


STATUS_NOT_REFRESHED = "Not Refreshed"
STATUS_QUEUED = "Queued"
STATUS_RUNNING = "Running"
STATUS_READY = "Ready"
STATUS_FAILED = "Failed"


def _logger():
    return frappe.logger("frapperag", allow_site=True)


def _log(level: str, message: str, *args) -> None:
    getattr(_logger(), level)(message, *args)
    getattr(frappe.logger(), level)("frapperag: " + message, *args)


def enqueue_schema_catalog_refresh(reason: str = "manual", requested_by: str | None = None) -> dict:
    status = frappe.db.get_single_value("AI Assistant Settings", "schema_catalog_status") or STATUS_NOT_REFRESHED
    if status in {STATUS_QUEUED, STATUS_RUNNING}:
        _log(
            "info",
            "schema catalog refresh enqueue skipped: site=%s reason=%s requested_by=%s status=%s",
            frappe.local.site,
            reason,
            requested_by or _current_user(),
            status,
        )
        return {"queued": False, "status": status}

    actor = requested_by or _current_user()
    _update_settings(
        {
            "schema_catalog_status": STATUS_QUEUED,
            "schema_catalog_last_error": "",
            "schema_catalog_refreshed_by": actor,
            "schema_catalog_last_reason": reason,
        }
    )
    frappe.db.commit()

    try:
        frappe.enqueue(
            "frapperag.assistant.schema_refresh.run_schema_catalog_refresh_job",
            queue="short",
            timeout=600,
            job_name="frapperag_schema_catalog_refresh",
            site=frappe.local.site,
            reason=reason,
            requested_by=actor,
        )
    except Exception:
        error_message = frappe.get_traceback(with_context=False)
        _update_settings(
            {
                "schema_catalog_status": STATUS_FAILED,
                "schema_catalog_last_error": error_message,
                "schema_catalog_refreshed_by": actor,
                "schema_catalog_last_reason": reason,
            }
        )
        frappe.db.commit()
        frappe.log_error(error_message, "FrappeRAG schema catalog enqueue failed")
        _log(
            "error",
            "schema catalog refresh enqueue failed: site=%s reason=%s requested_by=%s",
            frappe.local.site,
            reason,
            actor,
        )
        raise
    _log(
        "info",
        "schema catalog refresh queued: site=%s reason=%s requested_by=%s",
        frappe.local.site,
        reason,
        actor,
    )
    return {"queued": True, "status": STATUS_QUEUED}


def run_schema_catalog_refresh_job(reason: str = "manual", requested_by: str | None = None) -> dict:
    return refresh_schema_catalog(reason=reason, requested_by=requested_by, throw=False)


def refresh_schema_catalog(
    reason: str = "manual",
    requested_by: str | None = None,
    throw: bool = True,
) -> dict:
    actor = requested_by or _current_user()
    _log(
        "info",
        "schema catalog refresh started: site=%s reason=%s requested_by=%s",
        frappe.local.site,
        reason,
        actor,
    )
    _update_settings(
        {
            "schema_catalog_status": STATUS_RUNNING,
            "schema_catalog_last_error": "",
            "schema_catalog_refreshed_by": actor,
        }
    )
    frappe.db.commit()

    try:
        catalog = build_schema_catalog()
        file_meta = write_schema_catalog(catalog)
        summary = catalog.get("summary", {})
        settings_update = {
            "schema_catalog_refreshed_on": frappe.utils.now_datetime(),
            "schema_catalog_refreshed_by": actor,
            "schema_catalog_last_error": "",
            "schema_catalog_doctype_count": summary.get("doctype_count", 0),
            "schema_catalog_report_count": summary.get("report_count", 0),
            "schema_catalog_workflow_count": summary.get("workflow_count", 0),
            "schema_catalog_bytes": file_meta["bytes"],
            "schema_catalog_digest": file_meta["digest"],
            "schema_catalog_path": file_meta["path"],
            "schema_catalog_status": STATUS_READY,
            "schema_catalog_last_reason": reason,
        }
        _update_settings(settings_update)
        frappe.db.commit()
        _log(
            "info",
            "schema catalog refresh succeeded: site=%s reason=%s requested_by=%s path=%s doctypes=%s reports=%s workflows=%s bytes=%s",
            frappe.local.site,
            reason,
            actor,
            file_meta["path"],
            settings_update["schema_catalog_doctype_count"],
            settings_update["schema_catalog_report_count"],
            settings_update["schema_catalog_workflow_count"],
            settings_update["schema_catalog_bytes"],
        )
        return {"queued": False, "status": STATUS_READY, **settings_update}
    except Exception:
        error_message = frappe.get_traceback(with_context=False)
        _update_settings(
            {
                "schema_catalog_status": STATUS_FAILED,
                "schema_catalog_last_error": error_message,
                "schema_catalog_refreshed_by": actor,
                "schema_catalog_last_reason": reason,
            }
        )
        frappe.db.commit()
        frappe.log_error(error_message, "FrappeRAG schema catalog refresh failed")
        _log(
            "error",
            "schema catalog refresh failed: site=%s reason=%s requested_by=%s",
            frappe.local.site,
            reason,
            actor,
        )
        if throw:
            raise
        return {"queued": False, "status": STATUS_FAILED, "error": error_message}


def _update_settings(values: dict) -> None:
    if not frappe.db.exists("DocType", "AI Assistant Settings"):
        return

    for fieldname, value in values.items():
        frappe.db.set_single_value(
            "AI Assistant Settings",
            fieldname,
            value,
            update_modified=False,
        )

    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")


def _current_user() -> str:
    user = getattr(frappe.session, "user", None)
    if user and user != "Guest":
        return user
    return "System"
