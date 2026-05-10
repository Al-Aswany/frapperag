from __future__ import annotations

import frappe

from frapperag.setup.install import seed_all_settings


def execute() -> None:
    if not frappe.db.exists("DocType", "AI Assistant Settings"):
        return

    seed_all_settings()
    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
    frappe.clear_cache(doctype="AI Assistant Settings")
