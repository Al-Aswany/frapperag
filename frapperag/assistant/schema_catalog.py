from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import frappe
from frappe.utils import cint, now_datetime


_LAYOUT_FIELDTYPES = {
    "Section Break",
    "Column Break",
    "Tab Break",
    "HTML",
    "Button",
    "Fold",
    "Heading",
}


def get_catalog_path() -> str:
    catalog_dir = frappe.utils.get_site_path("private", "frapperag")
    os.makedirs(catalog_dir, exist_ok=True)
    return os.path.join(catalog_dir, "schema_catalog.json")


def build_schema_catalog() -> dict[str, Any]:
    doctypes = _build_doctype_entries()
    reports = _build_report_entries()
    workflows = _build_workflow_entries()

    return {
        "generated_at": str(now_datetime()),
        "site": frappe.local.site,
        "summary": {
            "doctype_count": len(doctypes),
            "report_count": len(reports),
            "workflow_count": len(workflows),
        },
        "doctypes": doctypes,
        "reports": reports,
        "workflows": workflows,
    }


def load_schema_catalog() -> dict[str, Any] | None:
    path = get_catalog_path()
    if not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_schema_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    path = get_catalog_path()
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    temp_path = f"{path}.tmp"

    with open(temp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)

    os.replace(temp_path, path)

    return {
        "path": path,
        "digest": digest,
        "bytes": len(payload.encode("utf-8")),
    }


def _build_doctype_entries() -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "DocType",
        fields=[
            "name",
            "module",
            "custom",
            "istable",
            "issingle",
            "is_submittable",
            "track_changes",
        ],
        order_by="name asc",
        ignore_permissions=True,
    )

    entries: list[dict[str, Any]] = []
    for row in rows:
        meta = frappe.get_meta(row.name, cached=False)
        fields = [_serialize_field(field) for field in meta.fields if field.fieldtype not in _LAYOUT_FIELDTYPES]
        child_tables = sorted(
            {
                field.options
                for field in meta.fields
                if field.fieldtype == "Table" and field.options
            }
        )

        entries.append(
            {
                "name": row.name,
                "module": row.module,
                "custom": cint(row.custom),
                "is_child_table": cint(row.istable),
                "is_single": cint(row.issingle),
                "is_submittable": cint(row.is_submittable),
                "track_changes": cint(row.track_changes),
                "fields": fields,
                "links": sorted(
                    {
                        field["options"]
                        for field in fields
                        if field["fieldtype"] == "Link" and field.get("options")
                    }
                ),
                "child_tables": child_tables,
                "permissions": _serialize_permissions(meta),
            }
        )

    return entries


def _build_report_entries() -> list[dict[str, Any]]:
    rows = frappe.get_all(
        "Report",
        fields=["name", "module", "ref_doctype", "report_type", "is_standard"],
        order_by="name asc",
        ignore_permissions=True,
    )
    return [
        {
            "name": row.name,
            "module": row.module,
            "ref_doctype": row.ref_doctype,
            "report_type": row.report_type,
            "is_standard": cint(row.is_standard),
        }
        for row in rows
    ]


def _build_workflow_entries() -> list[dict[str, Any]]:
    if not frappe.db.exists("DocType", "Workflow"):
        return []

    rows = frappe.get_all(
        "Workflow",
        fields=["name", "document_type", "is_active", "workflow_state_field"],
        order_by="name asc",
        ignore_permissions=True,
    )

    entries: list[dict[str, Any]] = []
    for row in rows:
        workflow = frappe.get_doc("Workflow", row.name)
        entries.append(
            {
                "name": workflow.name,
                "document_type": workflow.document_type,
                "is_active": cint(row.is_active),
                "workflow_state_field": workflow.workflow_state_field,
                "states": [
                    {
                        "state": state.state,
                        "doc_status": cint(state.doc_status),
                        "allow_edit": state.allow_edit,
                    }
                    for state in workflow.states
                ],
                "transitions": [
                    {
                        "state": transition.state,
                        "action": transition.action,
                        "next_state": transition.next_state,
                        "allowed": transition.allowed,
                    }
                    for transition in workflow.transitions
                ],
            }
        )

    return entries


def _serialize_field(field: Any) -> dict[str, Any]:
    options = None
    if field.fieldtype == "Select" and field.options:
        options = [option for option in field.options.split("\n") if option]
    elif field.options:
        options = field.options

    return {
        "fieldname": field.fieldname,
        "label": field.label or field.fieldname,
        "fieldtype": field.fieldtype,
        "options": options,
        "reqd": cint(field.reqd),
        "hidden": cint(field.hidden),
        "read_only": cint(field.read_only),
        "in_list_view": cint(field.in_list_view),
        "in_standard_filter": cint(field.in_standard_filter),
    }


def _serialize_permissions(meta: Any) -> list[dict[str, Any]]:
    permissions: list[dict[str, Any]] = []
    for perm in meta.permissions:
        permissions.append(
            {
                "role": perm.role,
                "permlevel": cint(getattr(perm, "permlevel", 0)),
                "read": cint(getattr(perm, "read", 0)),
                "write": cint(getattr(perm, "write", 0)),
                "create": cint(getattr(perm, "create", 0)),
                "delete": cint(getattr(perm, "delete", 0)),
                "submit": cint(getattr(perm, "submit", 0)),
                "cancel": cint(getattr(perm, "cancel", 0)),
                "amend": cint(getattr(perm, "amend", 0)),
                "report": cint(getattr(perm, "report", 0)),
                "export": cint(getattr(perm, "export", 0)),
            }
        )
    return permissions
