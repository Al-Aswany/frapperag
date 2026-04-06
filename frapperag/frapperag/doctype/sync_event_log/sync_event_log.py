from frappe.model.document import Document


class SyncEventLog(Document):
    """Sync Event Log — records every incremental sync attempt.

    No custom logic required; all state transitions happen via
    frappe.db.set_value() in sync_runner.py to avoid triggering
    additional doc_events during background job execution.
    """
    pass
