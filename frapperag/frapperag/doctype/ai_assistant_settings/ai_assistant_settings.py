import frappe
from frappe.model.document import Document


class AIAssistantSettings(Document):
    def validate(self):
        if not self.is_enabled:
            return
        if not self.gemini_api_key:
            frappe.throw(
                "Gemini API Key is required when the AI Assistant is enabled.",
                frappe.ValidationError,
            )
        if not self.allowed_doctypes:
            frappe.throw(
                "At least one Allowed Document Type is required when the AI Assistant is enabled.",
                frappe.ValidationError,
            )
        if not self.allowed_roles:
            frappe.throw(
                "At least one Allowed Role is required when the AI Assistant is enabled.",
                frappe.ValidationError,
            )

    def on_update(self):
        """Detect removed DocTypes and enqueue a purge job for each one (US3 / FR-005)."""
        old = self.get_doc_before_save()
        old_allowed = {r.doctype_name for r in old.allowed_doctypes} if old else set()
        new_allowed = {r.doctype_name for r in self.allowed_doctypes}

        removed = old_allowed - new_allowed
        if not removed:
            return

        for dt in removed:
            log = frappe.get_doc({
                "doctype": "Sync Event Log",
                "doctype_name": dt,
                "record_name": "*",
                "trigger_type": "Purge",
                "outcome": "Queued",
            })
            log.insert(ignore_permissions=True)
            frappe.db.commit()

            frappe.enqueue(
                "frapperag.rag.sync_runner.run_purge_job",
                queue="short",
                timeout=120,
                job_name=f"rag_purge_{dt.lower().replace(' ', '_')}",
                site=frappe.local.site,
                sync_log_id=log.name,
                doctype=dt,
                user=frappe.session.user,
            )
