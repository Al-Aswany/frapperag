from __future__ import annotations

import re
from typing import Any, Sequence

import frappe
from frappe.utils import cint

from frapperag.assistant.schema_catalog import load_schema_catalog


DEFAULT_LIMIT = 20
MAX_LIMIT = 200
DEFAULT_SORT = "modified desc"

_SORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\s+(?:asc|desc))?$", re.IGNORECASE)
_UNSAFE_FIELDTYPES = {
    "Attach",
    "Attach Image",
    "Code",
    "HTML Editor",
    "Password",
    "Signature",
    "Table",
    "Table MultiSelect",
    "Text Editor",
}
_LONG_TEXT_FIELDTYPES = {
    "Long Text",
    "Markdown Editor",
    "Small Text",
    "Text",
}
_SENSITIVE_NAME_PARTS = (
    "access_token",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "cookie",
    "hash",
    "otp",
    "passwd",
    "password",
    "pin",
    "private_key",
    "refresh_token",
    "salt",
    "secret",
    "session",
    "token",
)


def load_allowed_doctype_policies(settings: Any | None = None) -> dict[str, dict[str, Any]]:
    settings = settings or frappe.get_cached_doc("AI Assistant Settings")
    policies: dict[str, dict[str, Any]] = {}

    for row in (getattr(settings, "allowed_doctypes", None) or []):
        doctype_name = (getattr(row, "doctype_name", None) or "").strip()
        if not doctype_name:
            continue

        legacy_date_field = _clean_optional(getattr(row, "date_field", None))
        policy = {
            "doctype_name": doctype_name,
            "enabled": _normalize_flag(getattr(row, "enabled", None), default=1),
            "legacy_date_field": legacy_date_field,
            "default_date_field": _clean_optional(getattr(row, "default_date_field", None)) or legacy_date_field,
            "default_title_field": _clean_optional(getattr(row, "default_title_field", None)),
            "allow_get_list": _normalize_flag(getattr(row, "allow_get_list", None), default=1),
            "allow_query_builder": _normalize_flag(getattr(row, "allow_query_builder", None), default=0),
            "allow_child_tables": _normalize_flag(getattr(row, "allow_child_tables", None), default=0),
            "default_sort": _normalize_sort(getattr(row, "default_sort", None)),
            "default_limit": _normalize_limit(getattr(row, "default_limit", None)),
            "large_table_requires_date_filter": _normalize_flag(
                getattr(row, "large_table_requires_date_filter", None),
                default=0,
            ),
        }
        policies[doctype_name] = policy

    return policies


def get_allowed_doctype_policy(doctype_name: str, settings: Any | None = None) -> dict[str, Any] | None:
    return load_allowed_doctype_policies(settings=settings).get((doctype_name or "").strip())


def build_safe_schema_slice(
    doctype_names: Sequence[str] | None = None,
    *,
    catalog: dict[str, Any] | None = None,
    settings: Any | None = None,
    include_unsafe_fields: bool = False,
    include_permissions: bool = False,
) -> dict[str, Any]:
    catalog = catalog or load_schema_catalog() or {}
    policies = load_allowed_doctype_policies(settings=settings)
    catalog_entries = {
        entry.get("name"): entry
        for entry in (catalog.get("doctypes") or [])
        if entry.get("name")
    }

    requested = _resolve_requested_doctypes(doctype_names, policies)
    doctypes: list[dict[str, Any]] = []
    missing: list[str] = []
    excluded_field_count = 0

    for doctype_name in requested:
        entry = catalog_entries.get(doctype_name)
        if not entry:
            missing.append(doctype_name)
            continue

        policy = policies[doctype_name]
        sliced = _build_doctype_slice(
            entry,
            policy,
            include_unsafe_fields=include_unsafe_fields,
            include_permissions=include_permissions,
        )
        doctypes.append(sliced)
        excluded_field_count += sliced["field_summary"]["excluded_field_count"]

    return {
        "generated_at": catalog.get("generated_at"),
        "site": catalog.get("site"),
        "summary": {
            "requested_doctype_count": len(requested),
            "returned_doctype_count": len(doctypes),
            "missing_doctype_count": len(missing),
            "excluded_field_count": excluded_field_count,
            "include_unsafe_fields": cint(include_unsafe_fields),
        },
        "missing_doctypes": missing,
        "doctypes": doctypes,
    }


def classify_field_safety(field: dict[str, Any]) -> dict[str, Any]:
    fieldtype = (field.get("fieldtype") or "").strip()
    fieldname = (field.get("fieldname") or "").strip()
    label = (field.get("label") or "").strip()
    reasons: list[str] = []

    if cint(field.get("hidden")):
        reasons.append("hidden")
    if fieldtype in _UNSAFE_FIELDTYPES:
        reasons.append(f"fieldtype:{fieldtype}")
    elif fieldtype in _LONG_TEXT_FIELDTYPES:
        reasons.append(f"fieldtype:{fieldtype}")
    if _looks_sensitive(fieldname) or _looks_sensitive(label):
        reasons.append("sensitive_name")

    return {
        "safe_for_ai": not reasons,
        "unsafe_reasons": reasons,
    }


def debug_query_policy_snapshot() -> dict[str, Any]:
    policies = load_allowed_doctype_policies()
    return {
        "doctype_count": len(policies),
        "doctypes": policies,
    }


def debug_safe_schema_slice(
    doctype_names: str | None = None,
    include_unsafe_fields: int = 0,
) -> dict[str, Any]:
    requested = None
    if doctype_names:
        requested = [name.strip() for name in doctype_names.split(",") if name.strip()]

    return build_safe_schema_slice(
        requested,
        include_unsafe_fields=bool(cint(include_unsafe_fields)),
    )


def _build_doctype_slice(
    entry: dict[str, Any],
    policy: dict[str, Any],
    *,
    include_unsafe_fields: bool,
    include_permissions: bool,
) -> dict[str, Any]:
    safe_fields: list[dict[str, Any]] = []
    excluded_fields: list[dict[str, Any]] = []
    safe_links: set[str] = set()

    for field in entry.get("fields") or []:
        classification = classify_field_safety(field)
        serialized_field = _serialize_field_for_slice(field, classification)

        if classification["safe_for_ai"]:
            safe_fields.append(serialized_field)
            if field.get("fieldtype") == "Link" and field.get("options"):
                safe_links.add(field["options"])
            continue

        excluded_fields.append(
            {
                "fieldname": field.get("fieldname"),
                "label": field.get("label"),
                "unsafe_reasons": classification["unsafe_reasons"],
            }
        )
        if include_unsafe_fields:
            safe_fields.append(serialized_field)

    doctype_slice = {
        "name": entry.get("name"),
        "module": entry.get("module"),
        "custom": cint(entry.get("custom")),
        "is_child_table": cint(entry.get("is_child_table")),
        "is_single": cint(entry.get("is_single")),
        "is_submittable": cint(entry.get("is_submittable")),
        "track_changes": cint(entry.get("track_changes")),
        "query_policy": {
            key: value
            for key, value in policy.items()
            if key != "doctype_name"
        },
        "fields": safe_fields,
        "links": sorted(safe_links),
        "child_tables": list(entry.get("child_tables") or []) if policy["allow_child_tables"] else [],
        "field_summary": {
            "included_field_count": len(safe_fields),
            "excluded_field_count": len(excluded_fields),
            "has_unsafe_fields": cint(bool(excluded_fields)),
        },
    }

    if excluded_fields:
        doctype_slice["excluded_fields"] = excluded_fields
    if include_permissions:
        doctype_slice["permissions"] = entry.get("permissions") or []

    return doctype_slice


def _serialize_field_for_slice(field: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
    return {
        "fieldname": field.get("fieldname"),
        "label": field.get("label"),
        "fieldtype": field.get("fieldtype"),
        "options": field.get("options"),
        "reqd": cint(field.get("reqd")),
        "read_only": cint(field.get("read_only")),
        "in_list_view": cint(field.get("in_list_view")),
        "in_standard_filter": cint(field.get("in_standard_filter")),
        "safe_for_ai": cint(classification["safe_for_ai"]),
        "unsafe_reasons": classification["unsafe_reasons"],
    }


def _resolve_requested_doctypes(
    doctype_names: Sequence[str] | None,
    policies: dict[str, dict[str, Any]],
) -> list[str]:
    if doctype_names is None:
        return [
            doctype_name
            for doctype_name, policy in policies.items()
            if policy["enabled"]
        ]

    requested: list[str] = []
    seen: set[str] = set()
    for doctype_name in doctype_names:
        normalized = (doctype_name or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in policies and policies[normalized]["enabled"]:
            requested.append(normalized)
    return requested


def _normalize_flag(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    return cint(value)


def _normalize_limit(value: Any) -> int:
    limit = cint(value or DEFAULT_LIMIT)
    return max(1, min(limit, MAX_LIMIT))


def _normalize_sort(value: Any) -> str:
    candidate = " ".join(((value or DEFAULT_SORT).strip()).split())
    if not candidate or not _SORT_RE.fullmatch(candidate):
        return DEFAULT_SORT
    return candidate


def _clean_optional(value: Any) -> str | None:
    candidate = (value or "").strip()
    return candidate or None


def _looks_sensitive(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower()).strip("_")
    if not normalized:
        return False

    tokens = [token for token in normalized.split("_") if token]
    token_pairs = {
        f"{tokens[index]}_{tokens[index + 1]}"
        for index in range(len(tokens) - 1)
    }
    searchable = set(tokens) | token_pairs | {normalized}

    return any(part in searchable for part in _SENSITIVE_NAME_PARTS)
