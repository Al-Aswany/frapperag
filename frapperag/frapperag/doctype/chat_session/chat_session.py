import frappe
from frappe.model.document import Document


class ChatSession(Document):
    pass


def permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    if "System Manager" in frappe.get_roles(user):
        return ""
    return f"`tabChat Session`.`owner` = {frappe.db.escape(user)}"
