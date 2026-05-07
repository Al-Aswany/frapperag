from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Any

import frappe
from frappe.utils import cint, get_datetime, getdate

from frapperag.assistant.planner import PLAN_VERSION, SUPPORTED_TOOL, debug_create_get_list_plan
from frapperag.assistant.schema_policy import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    classify_field_safety,
    get_allowed_doctype_policy,
)
from frapperag.assistant.tool_call_log import log_tool_call


VALIDATOR_VERSION = "phase3_v1"
_ALLOWED_PLAN_KEYS = {
    "clarification_question",
    "confidence",
    "final_answer_shape",
    "intent",
    "needs_clarification",
    "plan_version",
    "planner_mode",
    "question",
    "request_id",
    "steps",
    "validated",
    "validated_at",
    "validator_version",
}
_ALLOWED_STEP_KEYS = {
    "doctype",
    "fields",
    "filters",
    "limit",
    "order_by",
    "step_id",
    "tool",
}
_SAFE_STANDARD_FIELDS = {
    "name": {"fieldname": "name", "label": "ID", "fieldtype": "Data", "standard": 1},
    "creation": {"fieldname": "creation", "label": "Created On", "fieldtype": "Datetime", "standard": 1},
    "modified": {"fieldname": "modified", "label": "Modified On", "fieldtype": "Datetime", "standard": 1},
    "docstatus": {"fieldname": "docstatus", "label": "Document Status", "fieldtype": "Int", "standard": 1},
}
_DATE_FIELDTYPES = {"Date", "Datetime"}
_NUMERIC_FIELDTYPES = {"Currency", "Float", "Int", "Percent"}
_STRING_FIELDTYPES = {"Data", "Dynamic Link", "Link", "Read Only", "Select"}
_BOOLEAN_FIELDTYPES = {"Check"}
_SAFE_OPERATORS = {"=", "in", "between", ">=", "<=", "like_prefix"}
_SORT_RE = re.compile(r"^(?P<field>[A-Za-z][A-Za-z0-9_]*)(?:\s+(?P<direction>asc|desc))?$", re.IGNORECASE)


def validate_plan(
    plan: dict[str, Any] | str,
    *,
    require_validated_flag: bool = False,
    log_result: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    raw_plan = _coerce_plan(plan)
    request_id = (raw_plan.get("request_id") or "").strip()
    doctype_names = _extract_doctype_names(raw_plan)
    try:
        validated = _validate_plan(raw_plan, require_validated_flag=require_validated_flag)
    except Exception as exc:
        if log_result:
            log_tool_call(
                "validator.validate_plan",
                "Rejected",
                tool_name=SUPPORTED_TOOL,
                doctype_name=",".join(doctype_names[:3]),
                request_id=request_id,
                intent=(raw_plan.get("intent") or "").strip(),
                duration_ms=_duration_ms(started),
                error_message=str(exc),
                plan=raw_plan,
                details={"require_validated_flag": cint(require_validated_flag)},
            )
        raise

    if log_result:
        log_tool_call(
            "validator.validate_plan",
            "Success",
            tool_name=SUPPORTED_TOOL,
            doctype_name=",".join(doctype_names[:3]),
            request_id=validated.get("request_id"),
            intent=validated.get("intent"),
            duration_ms=_duration_ms(started),
            plan=validated,
            details={"step_count": len(validated.get("steps") or [])},
        )
    return validated


def debug_validate_plan(plan_json: str, require_validated_flag: int = 0) -> dict[str, Any]:
    return validate_plan(plan_json, require_validated_flag=bool(cint(require_validated_flag)))


def debug_build_and_validate_get_list_plan(
    question: str,
    doctype: str,
    fields_json: str,
    filters_json: str | None = None,
    order_by_json: str | None = None,
    limit: int | None = None,
    final_answer_shape: str = "table",
    request_id: str | None = None,
) -> dict[str, Any]:
    plan = debug_create_get_list_plan(
        question=question,
        doctype=doctype,
        fields_json=fields_json,
        filters_json=filters_json,
        order_by_json=order_by_json,
        limit=limit,
        final_answer_shape=final_answer_shape,
        request_id=request_id,
    )
    return validate_plan(plan, require_validated_flag=False, log_result=True)


def debug_describe_queryable_doctype(doctype: str) -> dict[str, Any]:
    context = _build_doctype_context((doctype or "").strip())
    return {
        "doctype": context["doctype"],
        "policy": context["policy"],
        "default_date_field": context["default_date_field"],
        "safe_field_count": len(context["safe_fields"]),
        "safe_fields": sorted(context["safe_fields"].keys()),
        "sortable_fields": sorted(context["sortable_fields"]),
        "filterable_fields": sorted(context["filterable_fields"]),
        "unsafe_fields": context["unsafe_fields"],
    }


def _validate_plan(raw_plan: dict[str, Any], *, require_validated_flag: bool) -> dict[str, Any]:
    _reject_unknown_keys(raw_plan, _ALLOWED_PLAN_KEYS, label="Plan")

    plan_version = (raw_plan.get("plan_version") or "").strip()
    if plan_version != PLAN_VERSION:
        frappe.throw(
            f"Plan version '{plan_version or '<empty>'}' is not supported.",
            frappe.ValidationError,
        )

    if require_validated_flag and not cint(raw_plan.get("validated")):
        frappe.throw("Executor requires a previously validated plan.", frappe.ValidationError)

    if cint(raw_plan.get("needs_clarification")):
        frappe.throw("Plans that need clarification cannot be executed.", frappe.ValidationError)

    steps = raw_plan.get("steps")
    if not isinstance(steps, list) or not steps:
        frappe.throw("Plan must include at least one step.", frappe.ValidationError)

    validated_steps = [_validate_step(step, index) for index, step in enumerate(steps, start=1)]
    return {
        "plan_version": PLAN_VERSION,
        "planner_mode": (raw_plan.get("planner_mode") or "").strip() or "manual_scaffold",
        "request_id": (raw_plan.get("request_id") or "").strip(),
        "intent": (raw_plan.get("intent") or "structured_query").strip() or "structured_query",
        "confidence": _coerce_confidence(raw_plan.get("confidence")),
        "question": (raw_plan.get("question") or "").strip(),
        "steps": validated_steps,
        "final_answer_shape": (raw_plan.get("final_answer_shape") or "table").strip() or "table",
        "needs_clarification": 0,
        "clarification_question": "",
        "validated": 1,
        "validator_version": VALIDATOR_VERSION,
        "validated_at": str(frappe.utils.now_datetime()),
    }


def _validate_step(step: Any, index: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        frappe.throw(f"Step {index} must be an object.", frappe.ValidationError)

    _reject_unknown_keys(step, _ALLOWED_STEP_KEYS, label=f"Step {index}")
    tool = (step.get("tool") or "").strip()
    if tool != SUPPORTED_TOOL:
        frappe.throw(
            f"Step {index} uses unsupported tool '{tool or '<empty>'}'.",
            frappe.ValidationError,
        )

    doctype = (step.get("doctype") or "").strip()
    if not doctype:
        frappe.throw(f"Step {index} is missing doctype.", frappe.ValidationError)

    context = _build_doctype_context(doctype)
    fields = _validate_fields(step.get("fields"), context, index=index)
    filters = _validate_filters(step.get("filters"), context, index=index)
    limit = _validate_limit(step.get("limit"), context, index=index)
    order_by = _validate_order_by(step.get("order_by"), context, index=index)
    _enforce_large_table_date_guard(filters, context, index=index)

    return {
        "step_id": (step.get("step_id") or f"step_{index}").strip() or f"step_{index}",
        "tool": SUPPORTED_TOOL,
        "doctype": doctype,
        "fields": fields,
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
    }


def _build_doctype_context(doctype: str) -> dict[str, Any]:
    policy = get_allowed_doctype_policy(doctype)
    if not policy or not cint(policy.get("enabled")):
        frappe.throw(f"DocType '{doctype}' is not enabled for live queries.", frappe.ValidationError)
    if not cint(policy.get("allow_get_list")):
        frappe.throw(f"DocType '{doctype}' does not allow get_list.", frappe.ValidationError)

    meta = frappe.get_meta(doctype)
    if cint(getattr(meta, "istable", 0)):
        frappe.throw(
            f"Child table DocType '{doctype}' is not supported by the Phase 3 get_list executor.",
            frappe.ValidationError,
        )
    if cint(getattr(meta, "issingle", 0)):
        frappe.throw(
            f"Single DocType '{doctype}' is not supported by the Phase 3 get_list executor.",
            frappe.ValidationError,
        )

    safe_fields = dict(_SAFE_STANDARD_FIELDS)
    unsafe_fields: list[dict[str, Any]] = []
    for field in meta.fields:
        serialized = {
            "fieldname": field.fieldname,
            "label": field.label or field.fieldname,
            "fieldtype": field.fieldtype,
            "options": field.options,
            "hidden": cint(field.hidden),
            "read_only": cint(field.read_only),
            "in_list_view": cint(field.in_list_view),
            "in_standard_filter": cint(field.in_standard_filter),
        }
        classification = classify_field_safety(serialized)
        if classification["safe_for_ai"]:
            safe_fields[field.fieldname] = {
                "fieldname": field.fieldname,
                "label": field.label or field.fieldname,
                "fieldtype": field.fieldtype,
                "options": field.options,
            }
            continue

        unsafe_fields.append(
            {
                "fieldname": field.fieldname,
                "fieldtype": field.fieldtype,
                "unsafe_reasons": classification["unsafe_reasons"],
            }
        )

    filterable_fields = set(safe_fields.keys())
    sortable_fields = {
        fieldname
        for fieldname, field in safe_fields.items()
        if field.get("fieldtype") not in {"JSON"}
    }
    default_date_field = (policy.get("default_date_field") or "").strip()
    if default_date_field and default_date_field not in safe_fields:
        frappe.throw(
            f"DocType '{doctype}' has an unsafe or missing default_date_field '{default_date_field}'.",
            frappe.ValidationError,
        )

    return {
        "doctype": doctype,
        "meta": meta,
        "policy": {
            "enabled": cint(policy.get("enabled")),
            "allow_get_list": cint(policy.get("allow_get_list")),
            "default_date_field": default_date_field,
            "default_limit": _coerce_policy_limit(policy.get("default_limit")),
            "default_sort": _normalize_policy_sort(policy.get("default_sort"), sortable_fields),
            "large_table_requires_date_filter": cint(policy.get("large_table_requires_date_filter")),
        },
        "safe_fields": safe_fields,
        "unsafe_fields": unsafe_fields,
        "filterable_fields": filterable_fields,
        "sortable_fields": sortable_fields,
        "default_date_field": default_date_field,
    }


def _validate_fields(raw_fields: Any, context: dict[str, Any], *, index: int) -> list[str]:
    if not isinstance(raw_fields, list) or not raw_fields:
        frappe.throw(f"Step {index} must include a non-empty fields list.", frappe.ValidationError)

    validated: list[str] = []
    seen: set[str] = set()
    for raw_field in raw_fields:
        fieldname = str(raw_field or "").strip()
        if not fieldname or fieldname in seen:
            continue
        if fieldname not in context["safe_fields"]:
            frappe.throw(
                f"Step {index} field '{fieldname}' is not allowed for DocType '{context['doctype']}'.",
                frappe.ValidationError,
            )
        validated.append(fieldname)
        seen.add(fieldname)

    if not validated:
        frappe.throw(f"Step {index} has no safe fields to query.", frappe.ValidationError)
    return validated


def _validate_filters(raw_filters: Any, context: dict[str, Any], *, index: int) -> list[dict[str, Any]]:
    if raw_filters in (None, ""):
        return []
    if not isinstance(raw_filters, list):
        frappe.throw(f"Step {index} filters must be a list.", frappe.ValidationError)

    validated_filters: list[dict[str, Any]] = []
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, dict):
            frappe.throw(f"Step {index} filters must contain objects.", frappe.ValidationError)

        fieldname = str(raw_filter.get("field") or "").strip()
        operator = str(raw_filter.get("operator") or "").strip()
        if fieldname not in context["filterable_fields"]:
            frappe.throw(
                f"Step {index} filter field '{fieldname}' is not allowed for DocType '{context['doctype']}'.",
                frappe.ValidationError,
            )
        if operator not in _SAFE_OPERATORS:
            frappe.throw(
                f"Step {index} filter operator '{operator}' is not allowed.",
                frappe.ValidationError,
            )

        field_meta = context["safe_fields"][fieldname]
        value = _validate_filter_value(
            raw_filter.get("value"),
            operator,
            field_meta,
            step_index=index,
            fieldname=fieldname,
        )
        validated_filters.append(
            {
                "field": fieldname,
                "operator": operator,
                "value": value,
            }
        )
    return validated_filters


def _validate_filter_value(
    raw_value: Any,
    operator: str,
    field_meta: dict[str, Any],
    *,
    step_index: int,
    fieldname: str,
) -> Any:
    fieldtype = field_meta.get("fieldtype") or ""
    if operator == "in":
        if not isinstance(raw_value, list) or not raw_value:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' with operator 'in' needs a non-empty list.",
                frappe.ValidationError,
            )
        if len(raw_value) > 50:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' exceeds the 50-value cap for 'in'.",
                frappe.ValidationError,
            )
        return [_coerce_scalar(value, fieldtype, fieldname=fieldname, step_index=step_index) for value in raw_value]

    if operator == "between":
        if not isinstance(raw_value, list) or len(raw_value) != 2:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' with operator 'between' needs a two-item list.",
                frappe.ValidationError,
            )
        if fieldtype not in _DATE_FIELDTYPES:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' only supports 'between' on Date or Datetime fields.",
                frappe.ValidationError,
            )
        return [
            _coerce_scalar(raw_value[0], fieldtype, fieldname=fieldname, step_index=step_index),
            _coerce_scalar(raw_value[1], fieldtype, fieldname=fieldname, step_index=step_index),
        ]

    if operator in {">=", "<="}:
        if fieldtype not in (_DATE_FIELDTYPES | _NUMERIC_FIELDTYPES):
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' only supports '{operator}' on date or numeric fields.",
                frappe.ValidationError,
            )
        return _coerce_scalar(raw_value, fieldtype, fieldname=fieldname, step_index=step_index)

    if operator == "like_prefix":
        if fieldtype not in _STRING_FIELDTYPES:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' only supports 'like_prefix' on string-like fields.",
                frappe.ValidationError,
            )
        value = str(raw_value or "").strip()
        if not value:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' with operator 'like_prefix' needs a value.",
                frappe.ValidationError,
            )
        if "%" in value or "_" in value:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' cannot include SQL wildcard characters.",
                frappe.ValidationError,
            )
        return value

    return _coerce_scalar(raw_value, fieldtype, fieldname=fieldname, step_index=step_index)


def _coerce_scalar(value: Any, fieldtype: str, *, fieldname: str, step_index: int) -> Any:
    if value in (None, ""):
        frappe.throw(
            f"Step {step_index} filter '{fieldname}' requires a non-empty value.",
            frappe.ValidationError,
        )

    if fieldtype in _DATE_FIELDTYPES:
        try:
            if fieldtype == "Datetime":
                return str(get_datetime(value))
            return str(getdate(value))
        except Exception:
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' requires a valid {fieldtype} value.",
                frappe.ValidationError,
            )

    if fieldtype in _NUMERIC_FIELDTYPES:
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, ValueError, TypeError):
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' requires a numeric value.",
                frappe.ValidationError,
            )

    if fieldtype in _BOOLEAN_FIELDTYPES:
        if value not in (0, 1, True, False, "0", "1", "true", "false", "True", "False"):
            frappe.throw(
                f"Step {step_index} filter '{fieldname}' only accepts 0/1 or true/false.",
                frappe.ValidationError,
            )
        if value in (True, "true", "True", 1, "1"):
            return 1
        return 0

    return str(value).strip()


def _validate_limit(raw_limit: Any, context: dict[str, Any], *, index: int) -> int:
    if raw_limit in (None, ""):
        return context["policy"]["default_limit"]

    limit = cint(raw_limit)
    if limit < 1 or limit > MAX_LIMIT:
        frappe.throw(
            f"Step {index} limit must be between 1 and {MAX_LIMIT}.",
            frappe.ValidationError,
        )
    return limit


def _validate_order_by(raw_order_by: Any, context: dict[str, Any], *, index: int) -> str:
    if raw_order_by in (None, ""):
        return context["policy"]["default_sort"]

    if isinstance(raw_order_by, dict):
        fieldname = str(raw_order_by.get("field") or "").strip()
        direction = str(raw_order_by.get("direction") or "asc").strip().lower() or "asc"
    elif isinstance(raw_order_by, str):
        match = _SORT_RE.fullmatch(" ".join(raw_order_by.split()))
        if not match:
            frappe.throw(
                f"Step {index} order_by must use the form 'fieldname asc|desc'.",
                frappe.ValidationError,
            )
        fieldname = match.group("field")
        direction = (match.group("direction") or "asc").lower()
    else:
        frappe.throw(f"Step {index} order_by must be a string or object.", frappe.ValidationError)

    if fieldname not in context["sortable_fields"]:
        frappe.throw(
            f"Step {index} sort field '{fieldname}' is not allowed for DocType '{context['doctype']}'.",
            frappe.ValidationError,
        )
    if direction not in {"asc", "desc"}:
        frappe.throw(f"Step {index} sort direction must be asc or desc.", frappe.ValidationError)
    return f"{fieldname} {direction}"


def _enforce_large_table_date_guard(filters: list[dict[str, Any]], context: dict[str, Any], *, index: int) -> None:
    if not cint(context["policy"]["large_table_requires_date_filter"]):
        return

    default_date_field = context["default_date_field"]
    if not default_date_field:
        frappe.throw(
            f"DocType '{context['doctype']}' requires a date filter but has no safe default_date_field configured.",
            frappe.ValidationError,
        )

    for current in filters:
        if current["field"] != default_date_field:
            continue
        if current["operator"] in {"between", ">=", "<=", "="}:
            return

    frappe.throw(
        f"Step {index} must include a date filter on '{default_date_field}' for DocType '{context['doctype']}'.",
        frappe.ValidationError,
    )


def _coerce_plan(plan: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(plan, dict):
        return plan
    if isinstance(plan, str):
        try:
            parsed = json.loads(plan)
        except json.JSONDecodeError as exc:
            frappe.throw(f"plan_json is not valid JSON: {exc}", frappe.ValidationError)
        if not isinstance(parsed, dict):
            frappe.throw("plan_json must decode to an object.", frappe.ValidationError)
        return parsed
    frappe.throw("Plan must be a dict or JSON object string.", frappe.ValidationError)


def _reject_unknown_keys(payload: dict[str, Any], allowed_keys: set[str], *, label: str) -> None:
    unknown = sorted(set(payload.keys()) - allowed_keys)
    if unknown:
        frappe.throw(
            f"{label} contains unsupported keys: {', '.join(unknown)}.",
            frappe.ValidationError,
        )


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = 1.0
    return max(0.0, min(confidence, 1.0))


def _coerce_policy_limit(value: Any) -> int:
    limit = cint(value or DEFAULT_LIMIT)
    if limit < 1:
        return DEFAULT_LIMIT
    return min(limit, MAX_LIMIT)


def _normalize_policy_sort(value: Any, sortable_fields: set[str]) -> str:
    candidate = " ".join(str(value or "modified desc").split())
    match = _SORT_RE.fullmatch(candidate)
    if not match:
        return "modified desc"
    fieldname = match.group("field")
    direction = (match.group("direction") or "desc").lower()
    if fieldname not in sortable_fields:
        return "modified desc"
    return f"{fieldname} {direction}"


def _extract_doctype_names(plan: dict[str, Any]) -> list[str]:
    doctypes: list[str] = []
    for step in plan.get("steps") or []:
        if not isinstance(step, dict):
            continue
        doctype = (step.get("doctype") or "").strip()
        if doctype and doctype not in doctypes:
            doctypes.append(doctype)
    return doctypes


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
