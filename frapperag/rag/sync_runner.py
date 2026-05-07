"""Background job functions for incremental vector index sync.

All heavy imports (httpx, Gemini SDK modules, etc.) MUST be inside function
bodies — never at module level (Constitution Principle I).

Workers MUST NOT import lancedb or sentence_transformers directly; all
vector/embedding operations go through the sidecar HTTP API via sidecar_client.
"""

import traceback
import frappe
from frappe.utils import now_datetime, add_to_date


def _log():
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger


def run_sync_job(
    sync_log_id: str,
    doctype: str,
    name: str,
    trigger_type: str,
    user: str,
    **kwargs,
) -> None:
    """Worker: upsert (Create/Update/Rename) or delete (Delete) one record."""
    _log().info(f"[SYNC_START] sync_log_id={sync_log_id} doctype={doctype} name={name} trigger_type={trigger_type}")
    frappe.set_user(user)

    frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Running")
    frappe.db.commit()

    try:
        if trigger_type == "Delete":
            from frapperag.rag.sidecar_client import delete_record, SidecarError
            try:
                delete_record(doctype, name)
                frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Success")
                _log().info(f"[SYNC_SUCCESS] sync_log_id={sync_log_id} trigger_type=Delete")
            except SidecarError as exc:
                frappe.db.set_value("Sync Event Log", sync_log_id, {
                    "outcome": "Failed",
                    "error_message": str(exc),
                })
                _log().warning(f"[SYNC_FAIL] sync_log_id={sync_log_id} trigger_type=Delete failure_reason=Sidecar unavailable")
            frappe.db.commit()
            return

        if trigger_type == "Rename":
            old_name = kwargs.get("old_name", "")
            # Permission check on the renamed (new) document
            if not frappe.has_permission(doctype, doc=name, ptype="read", user=user):
                frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Skipped")
                frappe.db.commit()
                return

            doc = frappe.get_doc(doctype, name)
            from frapperag.rag.text_converter import to_text
            text = to_text(doctype, doc.as_dict())

            try:
                _api_key = frappe.get_cached_doc("AI Assistant Settings").get_password("gemini_api_key") or None
            except Exception:
                _api_key = None

            from frapperag.rag.sidecar_client import delete_record, upsert_record, SidecarError
            try:
                if old_name:
                    delete_record(doctype, old_name)
                if text:
                    upsert_record(doctype, name, text, api_key=_api_key)
                frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Success")
                _log().info(f"[SYNC_SUCCESS] sync_log_id={sync_log_id} trigger_type=Rename")
            except SidecarError as exc:
                frappe.db.set_value("Sync Event Log", sync_log_id, {
                    "outcome": "Failed",
                    "error_message": str(exc),
                })
                _log().warning(f"[SYNC_FAIL] sync_log_id={sync_log_id} trigger_type=Rename failure_reason=Sidecar unavailable")
            frappe.db.commit()
            return

        # Create / Update / Retry
        if not frappe.has_permission(doctype, doc=name, ptype="read", user=user):
            frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Skipped")
            frappe.db.commit()
            return

        doc = frappe.get_doc(doctype, name)
        from frapperag.rag.text_converter import to_text
        text = to_text(doctype, doc.as_dict())

        if not text:
            frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Skipped")
            frappe.db.commit()
            return

        try:
            _api_key = frappe.get_cached_doc("AI Assistant Settings").get_password("gemini_api_key") or None
        except Exception:
            _api_key = None

        from frapperag.rag.sidecar_client import upsert_record, SidecarError
        try:
            upsert_record(doctype, name, text, api_key=_api_key)
            frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Success")
            _log().info(f"[SYNC_SUCCESS] sync_log_id={sync_log_id} trigger_type={trigger_type}")
        except SidecarError as exc:
            frappe.db.set_value("Sync Event Log", sync_log_id, {
                "outcome": "Failed",
                "error_message": str(exc),
            })
            _log().warning(f"[SYNC_FAIL] sync_log_id={sync_log_id} trigger_type={trigger_type} failure_reason=Sidecar unavailable")
        frappe.db.commit()

    except Exception:
        tb = traceback.format_exc()
        frappe.db.set_value("Sync Event Log", sync_log_id, {
            "outcome": "Failed",
            "error_message": tb,
        })
        frappe.db.commit()
        _log().warning(f"[SYNC_FAIL] sync_log_id={sync_log_id} failure_reason=Unknown error error={tb[:200]}")


def run_purge_job(sync_log_id: str, doctype: str, user: str, **kwargs) -> None:
    """Worker: drop the entire v4_ LanceDB table for a removed-from-whitelist DocType."""
    frappe.set_user(user)

    frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Running")
    frappe.db.commit()

    try:
        from frapperag.rag.sidecar_client import drop_table, SidecarError
        try:
            drop_table(doctype)
            frappe.db.set_value("Sync Event Log", sync_log_id, "outcome", "Success")
        except SidecarError as exc:
            frappe.db.set_value("Sync Event Log", sync_log_id, {
                "outcome": "Failed",
                "error_message": str(exc),
            })
        frappe.db.commit()

    except Exception:
        frappe.db.set_value("Sync Event Log", sync_log_id, {
            "outcome": "Failed",
            "error_message": traceback.format_exc(),
        })
        frappe.db.commit()


def mark_stalled_sync_jobs() -> None:
    """Scheduler cron (*/5 * * * *): mark Running sync log entries stalled > 10 min as Failed."""
    cutoff = add_to_date(now_datetime(), minutes=-10)
    stalled = frappe.db.get_all(
        "Sync Event Log",
        filters={"outcome": "Running", "modified": ["<", cutoff]},
        fields=["name"],
    )
    if not stalled:
        return
    for entry in stalled:
        frappe.db.set_value("Sync Event Log", entry.name, {
            "outcome": "Failed",
            "error_message": "Stalled: no update for >10 minutes",
        })
    frappe.db.commit()


def prune_sync_event_log() -> None:
    """Scheduler daily: delete Sync Event Log entries older than 30 days (FR-013)."""
    cutoff = add_to_date(now_datetime(), days=-30)
    frappe.db.delete("Sync Event Log", {"creation": ["<", cutoff]})
    frappe.db.commit()
