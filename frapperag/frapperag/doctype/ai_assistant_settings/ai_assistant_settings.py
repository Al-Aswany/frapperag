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
