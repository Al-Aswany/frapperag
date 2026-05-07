import frappe

from frapperag.assistant.schema_refresh import enqueue_schema_catalog_refresh, refresh_schema_catalog as run_schema_catalog_refresh


def _require_rag_admin() -> None:
    roles = set(frappe.get_roles())
    if not (roles & {"RAG Admin", "System Manager"}):
        frappe.throw(
            "You do not have permission to refresh the schema catalog.",
            frappe.PermissionError,
        )


@frappe.whitelist()
def refresh_schema_catalog(run_in_background: int = 1) -> dict:
    _require_rag_admin()

    if int(run_in_background):
        return enqueue_schema_catalog_refresh(
            reason="manual",
            requested_by=frappe.session.user,
        )

    return run_schema_catalog_refresh(
        reason="manual",
        requested_by=frappe.session.user,
        throw=True,
    )
