"""Frappe doc_events handlers for incremental vector index sync.

These functions are called synchronously by Frappe inside the document
lifecycle transaction.  They MUST be fast (whitelist check + DB insert only)
and MUST NOT call frappe.db.commit() — doing so would prematurely commit the
outer doc.save() transaction.

The enqueue_after_commit=True flag guarantees each background job is dispatched
only after the outer transaction commits, so the worker always finds the Sync
Event Log entry already persisted in the database.
"""

import frappe

INTERNAL_DOCTYPES = {
    "Chat Session",
    "Chat Message",
    "AI Indexing Job",
    "AI Assistant Settings",
    "Sync Event Log",
}


def _get_settings():
    """Return cached AI Assistant Settings or None on any error."""
    try:
        return frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    except Exception:
        return None


def _is_whitelisted(settings, doctype: str) -> bool:
    """Return True if the DocType is in the allowed_doctypes whitelist."""
    if not settings or not settings.is_enabled:
        return False
    allowed = {r.doctype_name for r in getattr(settings, "allowed_doctypes", [])}
    return doctype in allowed


def _current_user() -> str:
    try:
        return frappe.session.user or "Administrator"
    except Exception:
        return "Administrator"


def on_document_save(doc, method=None) -> None:
    """on_update hook — queue upsert sync job if DocType is whitelisted."""
    if doc.doctype in INTERNAL_DOCTYPES:
        return
    settings = _get_settings()
    if not _is_whitelisted(settings, doc.doctype):
        return

    trigger_type = "Create" if doc.is_new() else "Update"

    log = frappe.get_doc({
        "doctype": "Sync Event Log",
        "doctype_name": doc.doctype,
        "record_name": doc.name,
        "trigger_type": trigger_type,
        "outcome": "Queued",
    })
    log.insert(ignore_permissions=True)
    # DO NOT call frappe.db.commit() here — we are inside the outer doc.save() transaction.

    table_key = f"{doc.doctype.lower().replace(' ', '_')}_{doc.name}"
    frappe.enqueue(
        "frapperag.rag.sync_runner.run_sync_job",
        queue="short",
        timeout=120,
        job_name=f"rag_sync_{table_key}",
        site=frappe.local.site,
        enqueue_after_commit=True,
        sync_log_id=log.name,
        doctype=doc.doctype,
        name=doc.name,
        trigger_type=trigger_type,
        user=_current_user(),
    )


def on_document_rename(doc, old_name, new_name, merge=False) -> None:
    """after_rename hook — queue rename sync job if DocType is whitelisted.

    Frappe calls this as hook_fn(doc, old_name, new_name, merge=False).
    doc is already in its new-name state; old_name and new_name are positional args.
    """
    if doc.doctype in INTERNAL_DOCTYPES:
        return
    settings = _get_settings()
    if not _is_whitelisted(settings, doc.doctype):
        return

    log = frappe.get_doc({
        "doctype": "Sync Event Log",
        "doctype_name": doc.doctype,
        "record_name": new_name,
        "trigger_type": "Rename",
        "outcome": "Queued",
    })
    log.insert(ignore_permissions=True)
    # DO NOT call frappe.db.commit() here.

    table_key = f"{doc.doctype.lower().replace(' ', '_')}_{new_name}"
    frappe.enqueue(
        "frapperag.rag.sync_runner.run_sync_job",
        queue="short",
        timeout=120,
        job_name=f"rag_sync_{table_key}",
        site=frappe.local.site,
        enqueue_after_commit=True,
        sync_log_id=log.name,
        doctype=doc.doctype,
        name=new_name,
        trigger_type="Rename",
        old_name=old_name,
        user=_current_user(),
    )


def on_document_trash(doc, method=None) -> None:
    """on_trash hook — queue delete sync job if DocType is whitelisted."""
    if doc.doctype in INTERNAL_DOCTYPES:
        return
    settings = _get_settings()
    if not _is_whitelisted(settings, doc.doctype):
        return

    log = frappe.get_doc({
        "doctype": "Sync Event Log",
        "doctype_name": doc.doctype,
        "record_name": doc.name,
        "trigger_type": "Delete",
        "outcome": "Queued",
    })
    log.insert(ignore_permissions=True)
    # DO NOT call frappe.db.commit() here.

    table_key = f"{doc.doctype.lower().replace(' ', '_')}_{doc.name}"
    frappe.enqueue(
        "frapperag.rag.sync_runner.run_sync_job",
        queue="short",
        timeout=60,
        job_name=f"rag_sync_{table_key}",
        site=frappe.local.site,
        enqueue_after_commit=True,
        sync_log_id=log.name,
        doctype=doc.doctype,
        name=doc.name,
        trigger_type="Delete",
        user=_current_user(),
    )
