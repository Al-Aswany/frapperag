import os
import frappe


def after_install():
    rag_path = frappe.get_site_path("private", "files", "rag")
    os.makedirs(rag_path, exist_ok=True)
    frappe.db.commit()
