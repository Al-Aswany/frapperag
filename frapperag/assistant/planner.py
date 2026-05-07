from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.intent_router import INTENT_STRUCTURED_QUERY
from frapperag.assistant.schema_policy import build_safe_schema_slice
from frapperag.assistant.tool_call_log import log_tool_call


PLAN_VERSION = "phase3_v1"
PLANNER_MODE = "manual_scaffold"
HYBRID_PLANNER_MODE = "hybrid_llm_json_v1"
SUPPORTED_TOOL = "get_list"
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def build_get_list_plan(
    *,
    question: str,
    doctype: str,
    fields: list[str],
    filters: list[dict[str, Any]] | None = None,
    order_by: dict[str, Any] | None = None,
    limit: int | None = None,
    request_id: str | None = None,
    final_answer_shape: str = "table",
    planner_mode: str = PLANNER_MODE,
    confidence: float = 1.0,
    log_operation: str = "planner.build_get_list_plan",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = (request_id or _new_request_id()).strip()
    step = {
        "step_id": "step_1",
        "tool": SUPPORTED_TOOL,
        "doctype": (doctype or "").strip(),
        "fields": [str(field).strip() for field in (fields or []) if str(field).strip()],
        "filters": filters or [],
        "limit": cint(limit or 0) or None,
    }
    if order_by:
        step["order_by"] = order_by

    plan = {
        "plan_version": PLAN_VERSION,
        "planner_mode": (planner_mode or PLANNER_MODE).strip() or PLANNER_MODE,
        "request_id": request_id,
        "intent": INTENT_STRUCTURED_QUERY,
        "confidence": _coerce_confidence(confidence),
        "question": (question or "").strip(),
        "steps": [step],
        "final_answer_shape": (final_answer_shape or "table").strip() or "table",
        "needs_clarification": 0,
        "clarification_question": "",
    }
    log_tool_call(
        log_operation,
        "Success",
        tool_name=SUPPORTED_TOOL,
        doctype_name=step["doctype"],
        request_id=request_id,
        intent=plan["intent"],
        plan=plan,
        details={
            "step_count": len(plan["steps"]),
            **(details or {}),
        },
    )
    return plan


def plan_structured_query(
    question: str,
    route: dict[str, Any],
    *,
    settings: Any | None = None,
    request_id: str | None = None,
) -> dict[str, Any] | None:
    request_id = (request_id or _new_request_id_for_prefix("hybrid")).strip()
    candidate_doctypes = [
        str(name).strip()
        for name in (route.get("candidate_doctypes") or [])
        if str(name).strip()
    ][:3]
    if not candidate_doctypes:
        _log_planner_rejection(
            request_id=request_id,
            reason="Router produced no candidate DocTypes for hybrid planning.",
            route=route,
        )
        return None

    settings = settings or frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    api_key = settings.get_password("gemini_api_key")
    if not api_key:
        _log_planner_rejection(
            request_id=request_id,
            reason="Gemini API key is not configured for hybrid planning.",
            route=route,
        )
        return None

    schema_snippets = _build_planner_schema_snippets(candidate_doctypes)
    if not schema_snippets:
        _log_planner_rejection(
            request_id=request_id,
            reason="No safe schema snippets were available for hybrid planning.",
            route=route,
        )
        return None

    from frapperag.rag.chat_engine import get_chat_runtime_settings
    from frapperag.rag.sidecar_client import chat

    runtime = get_chat_runtime_settings()
    response = chat(
        messages=_build_planner_messages(question, route, schema_snippets),
        api_key=api_key,
        model=runtime["model"],
        tools=None,
    )
    raw_text = (response.get("text") or "").strip()
    parsed = _parse_planner_response(raw_text)
    if not parsed:
        _log_planner_rejection(
            request_id=request_id,
            reason="Planner did not return valid JSON.",
            route=route,
            details={"raw_text": raw_text[:1000]},
        )
        return None

    if cint(parsed.get("needs_clarification")):
        _log_planner_rejection(
            request_id=request_id,
            reason=(parsed.get("clarification_question") or "Planner requested clarification.").strip(),
            route=route,
            details={"planner_payload": parsed},
        )
        return None

    doctype = (parsed.get("doctype") or "").strip()
    if doctype not in {entry["name"] for entry in schema_snippets}:
        _log_planner_rejection(
            request_id=request_id,
            reason=f"Planner selected unsupported DocType '{doctype or '<empty>'}'.",
            route=route,
            details={"planner_payload": parsed},
        )
        return None

    fields = [str(field).strip() for field in (parsed.get("fields") or []) if str(field).strip()]
    if not fields:
        _log_planner_rejection(
            request_id=request_id,
            reason="Planner returned no fields for get_list.",
            route=route,
            details={"planner_payload": parsed},
        )
        return None

    return build_get_list_plan(
        question=question,
        doctype=doctype,
        fields=fields,
        filters=parsed.get("filters") or [],
        order_by=parsed.get("order_by"),
        limit=parsed.get("limit"),
        request_id=request_id,
        final_answer_shape=parsed.get("final_answer_shape") or "table",
        planner_mode=HYBRID_PLANNER_MODE,
        confidence=parsed.get("confidence", route.get("confidence") or 0.0),
        log_operation="planner.plan_structured_query",
        details={
            "candidate_doctypes": candidate_doctypes,
            "route_confidence": route.get("confidence"),
            "schema_doctype_count": len(schema_snippets),
        },
    )


def debug_create_get_list_plan(
    question: str,
    doctype: str,
    fields_json: str,
    filters_json: str | None = None,
    order_by_json: str | None = None,
    limit: int | None = None,
    final_answer_shape: str = "table",
    request_id: str | None = None,
) -> dict[str, Any]:
    fields = _parse_json_arg(fields_json, expect_type=list, arg_name="fields_json")
    filters = _parse_json_arg(filters_json, expect_type=list, arg_name="filters_json", default=[])
    order_by = _parse_json_arg(order_by_json, expect_type=dict, arg_name="order_by_json", default=None)
    return build_get_list_plan(
        question=question,
        doctype=doctype,
        fields=fields,
        filters=filters,
        order_by=order_by,
        limit=limit,
        request_id=request_id,
        final_answer_shape=final_answer_shape,
    )


def _parse_json_arg(
    raw_value: str | None,
    *,
    expect_type: type,
    arg_name: str,
    default: Any = None,
) -> Any:
    if raw_value in (None, ""):
        return default

    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        frappe.throw(f"{arg_name} is not valid JSON: {exc}", frappe.ValidationError)

    if not isinstance(value, expect_type):
        frappe.throw(
            f"{arg_name} must decode to {expect_type.__name__}.",
            frappe.ValidationError,
        )
    return value


def _new_request_id() -> str:
    return _new_request_id_for_prefix("phase3")


def _new_request_id_for_prefix(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _build_planner_messages(
    question: str,
    route: dict[str, Any],
    schema_snippets: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    payload = {
        "question": (question or "").strip(),
        "today": _dt.date.today().isoformat(),
        "route": {
            "selected_intent": route.get("selected_intent"),
            "confidence": route.get("confidence"),
            "reason": route.get("reason"),
            "candidate_doctypes": route.get("candidate_doctypes") or [],
        },
        "tool": SUPPORTED_TOOL,
        "allowed_operators": ["=", "in", "between", ">=", "<=", "like_prefix"],
        "constraints": {
            "single_step_only": True,
            "read_only": True,
            "max_fields_hint": 6,
            "max_limit_hint": 20,
            "no_sql": True,
            "no_query_builder": True,
            "no_child_tables": True,
            "no_writes": True,
        },
        "schema_snippets": schema_snippets,
        "response_shape": {
            "doctype": "DocType name from schema_snippets",
            "fields": ["safe_fieldname_1", "safe_fieldname_2"],
            "filters": [{"field": "fieldname", "operator": "=", "value": "value"}],
            "order_by": {"field": "fieldname", "direction": "asc|desc"},
            "limit": 10,
            "confidence": 0.85,
            "final_answer_shape": "table",
            "needs_clarification": 0,
            "clarification_question": "",
        },
    }
    return [
        {
            "role": "user",
            "parts": [
                "You plan one safe ERP read-only query step. Return JSON only. "
                "Use exactly one get_list step over one listed DocType. "
                "Use only the provided safe fieldnames and only the allowed operators. "
                "Never output SQL, joins, child-table access, write actions, or explanations outside JSON. "
                "If the request is unclear or unsupported, set needs_clarification=1 and explain why."
            ],
        },
        {"role": "model", "parts": ["Understood. I will return JSON only."]},
        {"role": "user", "parts": [json.dumps(payload, sort_keys=True, default=str)]},
    ]


def _build_planner_schema_snippets(doctype_names: list[str]) -> list[dict[str, Any]]:
    safe_slice = build_safe_schema_slice(doctype_names)
    snippets: list[dict[str, Any]] = []
    for entry in safe_slice.get("doctypes") or []:
        fields = []
        for field in (entry.get("fields") or [])[:12]:
            fields.append(
                {
                    "fieldname": field.get("fieldname"),
                    "label": field.get("label"),
                    "fieldtype": field.get("fieldtype"),
                    "in_list_view": cint(field.get("in_list_view")),
                    "in_standard_filter": cint(field.get("in_standard_filter")),
                }
            )
        snippets.append(
            {
                "name": entry.get("name"),
                "module": entry.get("module"),
                "is_single": cint(entry.get("is_single")),
                "is_child_table": cint(entry.get("is_child_table")),
                "query_policy": {
                    "allow_get_list": entry.get("query_policy", {}).get("allow_get_list"),
                    "default_date_field": entry.get("query_policy", {}).get("default_date_field"),
                    "default_title_field": entry.get("query_policy", {}).get("default_title_field"),
                    "default_sort": entry.get("query_policy", {}).get("default_sort"),
                    "default_limit": entry.get("query_policy", {}).get("default_limit"),
                    "large_table_requires_date_filter": entry.get("query_policy", {}).get("large_table_requires_date_filter"),
                },
                "fields": fields,
            }
        )
    return snippets


def _parse_planner_response(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None

    candidate = raw_text.strip()
    if not candidate.startswith("{"):
        match = _JSON_OBJECT_RE.search(candidate)
        if not match:
            return None
        candidate = match.group(0)

    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    if isinstance(payload.get("steps"), list) and payload.get("steps"):
        first_step = payload["steps"][0]
        if not isinstance(first_step, dict):
            return None
        payload = {
            "doctype": first_step.get("doctype"),
            "fields": first_step.get("fields"),
            "filters": first_step.get("filters"),
            "order_by": first_step.get("order_by"),
            "limit": first_step.get("limit"),
            "confidence": payload.get("confidence"),
            "final_answer_shape": payload.get("final_answer_shape"),
            "needs_clarification": payload.get("needs_clarification"),
            "clarification_question": payload.get("clarification_question"),
        }

    filters = payload.get("filters")
    if filters in (None, ""):
        filters = []
    if not isinstance(filters, list):
        return None

    order_by = payload.get("order_by")
    if order_by not in (None, "") and not isinstance(order_by, (dict, str)):
        return None

    limit = payload.get("limit")
    if limit not in (None, ""):
        try:
            limit = cint(limit)
        except Exception:
            return None

    return {
        "doctype": str(payload.get("doctype") or "").strip(),
        "fields": payload.get("fields") if isinstance(payload.get("fields"), list) else [],
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
        "confidence": _coerce_confidence(payload.get("confidence")),
        "final_answer_shape": str(payload.get("final_answer_shape") or "table").strip() or "table",
        "needs_clarification": cint(payload.get("needs_clarification") or 0),
        "clarification_question": str(payload.get("clarification_question") or "").strip(),
    }


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = 1.0
    return max(0.0, min(confidence, 1.0))


def _log_planner_rejection(
    *,
    request_id: str,
    reason: str,
    route: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    log_tool_call(
        "planner.plan_structured_query",
        "Rejected",
        tool_name=SUPPORTED_TOOL,
        doctype_name=",".join((route.get("candidate_doctypes") or [])[:3]),
        request_id=request_id,
        intent=INTENT_STRUCTURED_QUERY,
        error_message=(reason or "Planner rejected the request.").strip(),
        details={
            "route": {
                "selected_intent": route.get("selected_intent"),
                "confidence": route.get("confidence"),
                "reason": route.get("reason"),
                "candidate_doctypes": route.get("candidate_doctypes") or [],
            },
            **(details or {}),
        },
    )
