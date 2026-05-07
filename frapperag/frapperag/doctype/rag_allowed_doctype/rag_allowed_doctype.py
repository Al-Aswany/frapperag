import re

import frappe
from frappe.model.document import Document
from frappe.utils import cint


_DEFAULT_LIMIT = 20
_MAX_LIMIT = 200
_DEFAULT_SORT = "modified desc"
_SORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\s+(?:asc|desc))?$", re.IGNORECASE)


class RAGAllowedDocType(Document):
    def validate(self):
        self._normalize_policy_fields()

    def _normalize_policy_fields(self) -> None:
        self.doctype_name = (self.doctype_name or "").strip()
        self.date_field = (self.date_field or "").strip()

        if hasattr(self, "default_date_field"):
            self.default_date_field = (self.default_date_field or self.date_field or "").strip()

        if hasattr(self, "default_title_field"):
            self.default_title_field = (self.default_title_field or "").strip()

        for fieldname in (
            "enabled",
            "allow_get_list",
            "allow_query_builder",
            "allow_child_tables",
            "large_table_requires_date_filter",
        ):
            if hasattr(self, fieldname):
                setattr(self, fieldname, cint(getattr(self, fieldname) or 0))

        if hasattr(self, "default_limit"):
            limit = cint(getattr(self, "default_limit", None) or _DEFAULT_LIMIT)
            self.default_limit = max(1, min(limit, _MAX_LIMIT))

        if hasattr(self, "default_sort"):
            default_sort = " ".join(((self.default_sort or _DEFAULT_SORT).strip()).split())
            if default_sort and not _SORT_RE.fullmatch(default_sort):
                frappe.throw(
                    "Default Sort must use the form 'fieldname asc' or 'fieldname desc'.",
                    frappe.ValidationError,
                )
            self.default_sort = default_sort or _DEFAULT_SORT
