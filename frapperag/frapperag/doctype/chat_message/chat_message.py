import frappe
from frappe.model.document import Document


class ChatMessage(Document):
    pass


def permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    escaped = frappe.db.escape(user)
    return (
        f"`tabChat Message`.`session` IN "
        f"(SELECT `name` FROM `tabChat Session` WHERE `owner` = {escaped})"
    )
