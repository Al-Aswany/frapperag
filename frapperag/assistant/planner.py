from __future__ import annotations

import datetime as _dt
import json
import re
import uuid
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.analytics.analytics_plan_schema import (
    ANALYSIS_TYPE_CO_OCCURRENCE,
    INTENT as ANALYTICS_INTENT,
    PLAN_VERSION as ANALYTICS_PLAN_VERSION,
    SUPPORTED_ANALYSIS_TYPES,
    TOOL_NAME as ANALYTICS_TOOL_NAME,
)
from frapperag.assistant.analytics.metric_registry import list_metrics
from frapperag.assistant.analytics.relationship_graph import get_allowed_relationship_fields, list_relationships
from frapperag.assistant.intent_router import INTENT_STRUCTURED_QUERY
from frapperag.assistant.schema_policy import (
    DEFAULT_LIMIT,
    build_safe_schema_slice,
    classify_field_safety,
    get_allowed_doctype_policy,
    get_analytics_field_policy,
    get_phase4f_analytics_source_doctypes,
    load_allowed_doctype_policies,
)
from frapperag.assistant.tool_call_log import build_analytics_log_details, log_tool_call


PLAN_VERSION = "phase3_v1"
PLANNER_MODE = "manual_scaffold"
HYBRID_PLANNER_MODE = "hybrid_llm_json_v1"
SUPPORTED_TOOL = "get_list"
HYBRID_PLANNER_OPERATION = "planner.plan_hybrid_query"
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_PLANNER_SCHEMA_FIELD_LIMIT = 16
_PLANNER_SUGGESTED_FIELD_LIMIT = 6
_ANALYTICS_DIMENSION_FIELD_LIMIT = 6
_PLANNER_STANDARD_FIELDS = (
    {
        "fieldname": "name",
        "label": "ID",
        "fieldtype": "Data",
        "in_list_view": 1,
        "in_standard_filter": 1,
        "reqd": 1,
    },
    {
        "fieldname": "modified",
        "label": "Modified On",
        "fieldtype": "Datetime",
        "in_list_view": 0,
        "in_standard_filter": 0,
        "reqd": 0,
    },
    {
        "fieldname": "creation",
        "label": "Created On",
        "fieldtype": "Datetime",
        "in_list_view": 0,
        "in_standard_filter": 0,
        "reqd": 0,
    },
    {
        "fieldname": "docstatus",
        "label": "Document Status",
        "fieldtype": "Int",
        "in_list_view": 0,
        "in_standard_filter": 0,
        "reqd": 0,
    },
)
_PLANNER_FIELD_KEYWORDS = {
    "amount": 18,
    "balance": 18,
    "company": 16,
    "customer": 24,
    "date": 28,
    "grand": 10,
    "group": 12,
    "invoice": 14,
    "modified": 14,
    "name": 36,
    "outstanding": 18,
    "party": 16,
    "posting": 24,
    "status": 26,
    "supplier": 24,
    "territory": 12,
    "title": 34,
    "total": 24,
}
_PHASE4F_ANALYTICS_LIMIT_CAPS = {
    "number": 1,
    "ranking": 10,
    "table": 10,
    "comparison": 10,
    "time_series": 12,
}
_GET_LIST_PREFERENCE_PHRASES = (
    "list ",
    "latest ",
    "most recent",
    "recent ",
    "show invoices",
    "show invoice",
    "show records",
    "open invoices",
    "open invoice",
)
_GET_LIST_PREFERENCE_TOKENS = {
    "latest",
    "list",
    "lists",
    "open",
    "recent",
    "record",
    "records",
}
_AGGREGATE_HINT_TOKENS = {
    "average",
    "compare",
    "comparison",
    "count",
    "counts",
    "declining",
    "grouped",
    "monthly",
    "outstanding",
    "pairs",
    "ratio",
    "revenue",
    "sum",
    "top",
    "totals",
    "trend",
    "unpaid",
    "value",
}
_CO_OCCURRENCE_CUE_PHRASES = (
    "bought with",
    "pairs",
    "sold together",
    "together",
)
_METRIC_GUARDRAIL_PHRASES = {
    "outstanding_amount": (
        "open balance",
        "outstanding",
        "overdue",
        "unpaid",
        "غير مدفوع",
        "مستحق",
    ),
    "avg_invoice_value": (
        "average invoice",
        "average value",
        "avg invoice",
        "mean invoice",
        "متوسط",
    ),
    "invoice_count": (
        "count invoices",
        "how many invoices",
        "invoice count",
        "number of invoices",
        "عدد الفواتير",
    ),
    "sales_qty": (
        "qty sold",
        "quantity sold",
        "sold quantity",
        "units sold",
        "الكمية المباعة",
    ),
    "purchase_qty": (
        "purchased quantity",
        "qty purchased",
        "quantity purchased",
        "units purchased",
        "الكمية المشتراة",
    ),
    "sales_amount": (
        "revenue",
        "sales amount",
        "sales value",
        "total sales",
        "قيمة المبيعات",
    ),
    "purchase_amount": (
        "purchase amount",
        "purchase value",
        "total purchases",
        "قيمة المشتريات",
    ),
}


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
        "filters": _normalize_filters(filters or []),
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


def build_analytics_plan(
    *,
    question: str,
    source_doctype: str,
    analysis_type: str,
    metrics: list[str] | None = None,
    dimensions: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    relationships: list[str] | None = None,
    time_bucket: dict[str, Any] | None = None,
    comparison: dict[str, Any] | None = None,
    numerator_metric: str | None = None,
    denominator_metric: str | None = None,
    sort: list[dict[str, Any]] | None = None,
    limit: int | None = None,
    request_id: str | None = None,
    final_answer_shape: str = "table",
    planner_mode: str = HYBRID_PLANNER_MODE,
    confidence: float = 1.0,
    log_operation: str = "planner.plan_analytics_query",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request_id = (request_id or _new_request_id_for_prefix("hybrid")).strip()
    requested_limit = cint(limit or 0) or 0
    effective_limit = _clamp_analytics_limit(
        requested_limit=requested_limit,
        final_answer_shape=final_answer_shape,
        policy_limit=DEFAULT_LIMIT,
    )
    plan = {
        "plan_version": ANALYTICS_PLAN_VERSION,
        "planner_mode": (planner_mode or HYBRID_PLANNER_MODE).strip() or HYBRID_PLANNER_MODE,
        "request_id": request_id,
        "intent": ANALYTICS_INTENT,
        "analysis_type": (analysis_type or "").strip(),
        "confidence": _coerce_confidence(confidence),
        "question": (question or "").strip(),
        "source_doctype": (source_doctype or "").strip(),
        "relationships": [str(value).strip() for value in (relationships or []) if str(value).strip()],
        "metrics": [str(value).strip() for value in (metrics or []) if str(value).strip()],
        "dimensions": [str(value).strip() for value in (dimensions or []) if str(value).strip()],
        "filters": _normalize_filters(filters or []),
        "time_bucket": dict(time_bucket or {}),
        "comparison": dict(comparison or {}),
        "numerator_metric": (numerator_metric or "").strip(),
        "denominator_metric": (denominator_metric or "").strip(),
        "sort": _normalize_analytics_sort(
            analysis_type=(analysis_type or "").strip(),
            time_bucket=time_bucket,
            sort=sort,
        ),
        "limit": effective_limit,
        "final_answer_shape": (final_answer_shape or "table").strip() or "table",
        "needs_clarification": 0,
        "clarification_question": "",
    }
    if plan["analysis_type"] == "co_occurrence":
        plan["metrics"] = []
        plan["numerator_metric"] = ""
        plan["denominator_metric"] = ""
    log_tool_call(
        log_operation,
        "Success",
        tool_name=ANALYTICS_TOOL_NAME,
        doctype_name=plan["source_doctype"],
        request_id=request_id,
        intent=plan["intent"],
        plan=plan,
        details={
            **build_analytics_log_details(
                hybrid_branch="analytics",
                analysis_type=plan["analysis_type"],
                source_doctype=plan["source_doctype"],
                planner_mode=plan["planner_mode"],
                requested_limit=requested_limit,
                effective_limit=plan["limit"],
                policy_limit=DEFAULT_LIMIT,
                metrics=plan["metrics"],
                dimensions=plan["dimensions"],
                relationships=plan["relationships"],
                result_status="planned",
            ),
            "route_confidence": 0.0,
            "candidate_doctypes": [],
            "final_answer_shape": plan["final_answer_shape"],
            **(details or {}),
        },
    )
    return plan


def plan_hybrid_query(
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
    settings = settings or frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    api_key = settings.get_password("gemini_api_key")
    if not api_key:
        _log_planner_rejection(
            request_id=request_id,
            reason="Gemini API key is not configured for hybrid planning.",
            route=route,
            log_operation=HYBRID_PLANNER_OPERATION,
        )
        return None

    schema_snippets = _build_planner_schema_snippets(candidate_doctypes) if candidate_doctypes else []
    analytics_capabilities = _build_analytics_capability_snippets(settings=settings)
    if not schema_snippets and not analytics_capabilities:
        _log_planner_rejection(
            request_id=request_id,
            reason="No safe hybrid planning context was available.",
            route=route,
            log_operation=HYBRID_PLANNER_OPERATION,
        )
        return None

    preferred_tool_hint = _preferred_tool_hint(question)
    raw_text = ""
    parsed = None
    for attempt in range(1, 3):
        try:
            raw_text = _run_planner_chat(
                messages=_build_hybrid_planner_messages(
                    question=question,
                    route=route,
                    schema_snippets=schema_snippets,
                    analytics_capabilities=analytics_capabilities,
                    preferred_tool_hint=preferred_tool_hint,
                    retry_on_invalid_json=attempt > 1,
                ),
                api_key=api_key,
            )
        except Exception as exc:
            _log_planner_rejection(
                request_id=request_id,
                reason=f"Planner request failed: {exc}",
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
                details={
                    **build_analytics_log_details(
                        hybrid_branch="planner",
                        planner_mode=HYBRID_PLANNER_MODE,
                        route_confidence=float(route.get("confidence") or 0.0),
                        candidate_doctypes=candidate_doctypes,
                        result_status="planner_error",
                        fallback_reason="planner_rejected",
                        error_code="planner_request_failed",
                        error_class=type(exc).__name__,
                    ),
                    "attempt": attempt,
                },
            )
            return None
        parsed = _parse_hybrid_planner_response(raw_text)
        if parsed:
            break
    if not parsed:
        _log_planner_rejection(
            request_id=request_id,
            reason="Planner did not return valid JSON.",
            route=route,
            log_operation=HYBRID_PLANNER_OPERATION,
            details={
                **build_analytics_log_details(
                    hybrid_branch="planner",
                    planner_mode=HYBRID_PLANNER_MODE,
                    route_confidence=float(route.get("confidence") or 0.0),
                    candidate_doctypes=candidate_doctypes,
                    result_status="rejected",
                    fallback_reason="planner_rejected",
                    error_code="planner_invalid_json",
                    error_class="ValidationError",
                ),
                "raw_text": raw_text[:1000],
                "retry_count": 1,
            },
        )
        return None

    if cint(parsed.get("needs_clarification")):
        _log_planner_rejection(
            request_id=request_id,
            reason=(parsed.get("clarification_question") or "Planner requested clarification.").strip(),
            route=route,
            log_operation=HYBRID_PLANNER_OPERATION,
            details={"planner_payload": parsed},
        )
        return None

    tool_choice = (parsed.get("tool") or "").strip()
    if tool_choice == SUPPORTED_TOOL:
        allowed_doctypes = {entry["name"] for entry in schema_snippets}
        doctype = _normalize_selected_doctype(parsed.get("doctype") or "", allowed_doctypes)
        if doctype not in allowed_doctypes:
            _log_planner_rejection(
                request_id=request_id,
                reason=f"Planner selected unsupported DocType '{doctype or '<empty>'}'.",
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
                details={"planner_payload": parsed},
            )
            return None

        fields = [str(field).strip() for field in (parsed.get("fields") or []) if str(field).strip()]
        if not fields:
            _log_planner_rejection(
                request_id=request_id,
                reason="Planner returned no fields for get_list.",
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
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
                **_hybrid_log_details(
                    route=route,
                    hybrid_branch="structured",
                    analysis_type="",
                    final_answer_shape=parsed.get("final_answer_shape") or "table",
                    result_status="planned",
                    fallback_reason="",
                    empty_result=0,
                ),
                "schema_doctype_count": len(schema_snippets),
            },
        )

    if tool_choice == ANALYTICS_TOOL_NAME:
        allowed_source_doctypes = {
            entry["source_doctype"]
            for entry in analytics_capabilities
            if str(entry.get("source_doctype") or "").strip()
        }
        source_doctype = _normalize_selected_doctype(parsed.get("source_doctype") or "", allowed_source_doctypes)
        if source_doctype not in allowed_source_doctypes:
            _log_planner_rejection(
                request_id=request_id,
                reason=f"Planner selected unsupported analytics source '{source_doctype or '<empty>'}'.",
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
                details={"planner_payload": parsed},
            )
            return None

        analysis_type = str(parsed.get("analysis_type") or "").strip()
        if analysis_type not in SUPPORTED_ANALYSIS_TYPES:
            _log_planner_rejection(
                request_id=request_id,
                reason=f"Planner selected unsupported analytics type '{analysis_type or '<empty>'}'.",
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
                details={"planner_payload": parsed},
            )
            return None

        analytics_rejection_reason, analytics_error_code = _analytics_plan_guardrail_rejection(
            question=question,
            parsed=parsed,
            source_doctype=source_doctype,
            analysis_type=analysis_type,
        )
        if analytics_rejection_reason:
            _log_planner_rejection(
                request_id=request_id,
                reason=analytics_rejection_reason,
                route=route,
                log_operation=HYBRID_PLANNER_OPERATION,
                tool_name=ANALYTICS_TOOL_NAME,
                intent=ANALYTICS_INTENT,
                details={
                    **build_analytics_log_details(
                        hybrid_branch="analytics",
                        analysis_type=analysis_type,
                        source_doctype=source_doctype,
                        planner_mode=HYBRID_PLANNER_MODE,
                        route_confidence=float(route.get("confidence") or 0.0),
                        candidate_doctypes=candidate_doctypes,
                        requested_limit=cint(parsed.get("limit") or 0),
                        effective_limit=_clamp_analytics_limit(
                            requested_limit=cint(parsed.get("limit") or 0),
                            final_answer_shape=parsed.get("final_answer_shape") or "table",
                            policy_limit=_analytics_policy_limit_for(source_doctype),
                        ),
                        policy_limit=_analytics_policy_limit_for(source_doctype),
                        metrics=parsed.get("metrics") or [],
                        dimensions=parsed.get("dimensions") or [],
                        relationships=parsed.get("relationships") or [],
                        result_status="rejected",
                        fallback_reason="planner_rejected",
                        error_code=analytics_error_code,
                        error_class="ValidationError",
                    ),
                    "planner_payload": parsed,
                },
            )
            return None

        _ensure_required_source_date_filter(parsed, source_doctype=source_doctype)

        return build_analytics_plan(
            question=question,
            source_doctype=source_doctype,
            analysis_type=analysis_type,
            metrics=parsed.get("metrics") or [],
            dimensions=parsed.get("dimensions") or [],
            filters=parsed.get("filters") or [],
            relationships=parsed.get("relationships") or [],
            time_bucket=parsed.get("time_bucket") or {},
            comparison=parsed.get("comparison") or {},
            numerator_metric=parsed.get("numerator_metric") or "",
            denominator_metric=parsed.get("denominator_metric") or "",
            sort=parsed.get("sort") or [],
            limit=parsed.get("limit"),
            request_id=request_id,
            final_answer_shape=parsed.get("final_answer_shape") or "table",
            planner_mode=HYBRID_PLANNER_MODE,
            confidence=parsed.get("confidence", route.get("confidence") or 0.0),
            log_operation="planner.plan_analytics_query",
            details={
                **_hybrid_log_details(
                    route=route,
                    hybrid_branch="analytics",
                    analysis_type=analysis_type,
                    final_answer_shape=parsed.get("final_answer_shape") or "table",
                    result_status="planned",
                    fallback_reason="",
                    empty_result=0,
                ),
                "policy_limit": _analytics_policy_limit_for(source_doctype),
            },
        )

    _log_planner_rejection(
        request_id=request_id,
        reason=f"Planner selected unsupported tool '{tool_choice or '<empty>'}'.",
        route=route,
        log_operation=HYBRID_PLANNER_OPERATION,
        details={"planner_payload": parsed},
    )
    return None


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

    raw_text = _run_planner_chat(
        messages=_build_planner_messages(question, route, schema_snippets),
        api_key=api_key,
    )
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

    allowed_doctypes = {entry["name"] for entry in schema_snippets}
    doctype = _normalize_selected_doctype(parsed.get("doctype") or "", allowed_doctypes)
    if doctype not in allowed_doctypes:
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
            **_hybrid_log_details(
                route=route,
                hybrid_branch="structured",
                analysis_type="",
                final_answer_shape=parsed.get("final_answer_shape") or "table",
                result_status="planned",
                fallback_reason="",
                empty_result=0,
            ),
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


def _normalize_filters(filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for current in filters or []:
        if not isinstance(current, dict):
            normalized.append(current)
            continue
        row = dict(current)
        operator = str(row.get("operator") or "").strip()
        if operator == ">":
            row["operator"] = ">="
        elif operator == "<":
            row["operator"] = "<="
        normalized.append(row)
    return normalized


def _normalize_analytics_sort(
    *,
    analysis_type: str,
    time_bucket: dict[str, Any] | None,
    sort: list[dict[str, Any]] | dict[str, Any] | None,
) -> list[dict[str, Any]]:
    if sort in (None, ""):
        return []
    if isinstance(sort, dict):
        sort = [sort]
    if not isinstance(sort, list):
        return []

    analysis_type = (analysis_type or "").strip()
    if analysis_type in {"co_occurrence", "time_bucket_aggregate", "trend"}:
        return []

    bucket_date_field = str((time_bucket or {}).get("date_field") or "").strip()
    normalized: list[dict[str, Any]] = []
    for entry in sort:
        if not isinstance(entry, dict):
            continue
        by = str(entry.get("by") or "").strip().lower()
        name = str(entry.get("name") or "").strip()
        direction = str(entry.get("direction") or "").strip().lower()
        if by not in {"metric", "dimension"} or not name or direction not in {"asc", "desc"}:
            continue
        if by == "dimension" and name in {"time_bucket", bucket_date_field}:
            continue
        normalized.append({"by": by, "name": name, "direction": direction})
    return normalized


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
                "The top-level 'doctype' key is required and must exactly match one schema_snippets.name value. "
                "Use only the provided safe fieldnames and only the allowed operators. "
                "For simple list requests, prefer the DocType that best matches the routed candidates and use "
                "'name' plus the most relevant suggested/list fields shown for that DocType. "
                "Never return an empty object or omit doctype when a listed DocType is a clear match. "
                "Never output SQL, joins, child-table access, write actions, or explanations outside JSON. "
                "If the request is unclear, unsupported, or a requested field is not shown, set "
                "needs_clarification=1 and explain why."
            ],
        },
        {"role": "model", "parts": ["Understood. I will return JSON only."]},
        {"role": "user", "parts": [json.dumps(payload, sort_keys=True, default=str)]},
    ]


def _build_hybrid_planner_messages(
    *,
    question: str,
    route: dict[str, Any],
    schema_snippets: list[dict[str, Any]],
    analytics_capabilities: list[dict[str, Any]],
    preferred_tool_hint: str,
    retry_on_invalid_json: bool,
) -> list[dict[str, Any]]:
    payload = {
        "question": (question or "").strip(),
        "today": _dt.date.today().isoformat(),
        "preferred_tool_hint": preferred_tool_hint,
        "retry_on_invalid_json": cint(retry_on_invalid_json),
        "route": {
            "selected_intent": route.get("selected_intent"),
            "confidence": route.get("confidence"),
            "reason": route.get("reason"),
            "candidate_doctypes": route.get("candidate_doctypes") or [],
        },
        "safe_tools": [SUPPORTED_TOOL, ANALYTICS_TOOL_NAME],
        "allowed_operators": ["=", "in", "between", ">=", "<=", "like_prefix"],
        "constraints": {
            "single_safe_plan_only": True,
            "read_only": True,
            "no_sql": True,
            "no_query_builder_code": True,
            "no_child_table_reads_except_approved_analytics_relationships": True,
            "no_writes": True,
        },
        "tool_guidance": {
            SUPPORTED_TOOL: (
                "Use for direct row retrieval, record lists, latest records, recent invoices, open invoices, "
                "or single-record lookup questions. NEVER use this for grouping, aggregation, ranking, 'most sold', or 'top' questions."
            ),
            ANALYTICS_TOOL_NAME: (
                "Use only for grouped, aggregated, ranked, trend, comparison, ratio, by-month, unpaid-by, "
                "declining, or item-pair questions. MUST be used for Arabic analytics questions (e.g. 'اعرض المبيعات حسب الشهر')."
            ),
        },
        "examples": [
            {
                "question": "List recent sales invoices since 2026-01-01.",
                "tool": "get_list",
                "plan": {
                    "doctype": "Sales Invoice",
                    "fields": ["name", "customer", "posting_date", "grand_total", "status"],
                    "filters": [{"field": "posting_date", "operator": ">=", "value": "2026-01-01"}],
                    "order_by": {"field": "posting_date", "direction": "desc"},
                    "limit": 10,
                    "final_answer_shape": "table",
                },
            },
            {
                "question": "Show the top customers by sales this year.",
                "tool": "analytics_plan",
                "plan": {
                    "analysis_type": "top_n",
                    "source_doctype": "Sales Invoice",
                    "metrics": ["sales_amount"],
                    "dimensions": ["customer"],
                    "filters": [
                        {"field": "docstatus", "operator": "=", "value": 1},
                        {"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-12-31"]},
                    ],
                    "sort": [{"by": "metric", "name": "sales_amount", "direction": "desc"}],
                    "limit": 10,
                    "final_answer_shape": "ranking",
                },
            },
            {
                "question": "Show sales by month this year.",
                "tool": "analytics_plan",
                "plan": {
                    "analysis_type": "time_bucket_aggregate",
                    "source_doctype": "Sales Invoice",
                    "metrics": ["sales_amount"],
                    "filters": [
                        {"field": "docstatus", "operator": "=", "value": 1},
                        {"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-12-31"]},
                    ],
                    "time_bucket": {"date_field": "posting_date", "grain": "month"},
                    "limit": 12,
                    "final_answer_shape": "time_series",
                },
            },
            {
                "question": "Show unpaid invoices by customer.",
                "tool": "analytics_plan",
                "plan": {
                    "analysis_type": "top_n",
                    "source_doctype": "Sales Invoice",
                    "metrics": ["outstanding_amount"],
                    "dimensions": ["customer"],
                    "filters": [{"field": "docstatus", "operator": "=", "value": 1}],
                    "sort": [{"by": "metric", "name": "outstanding_amount", "direction": "desc"}],
                    "limit": 10,
                    "final_answer_shape": "ranking",
                },
            },
            {
                "question": "What items are sold together most often this year?",
                "tool": "analytics_plan",
                "plan": {
                    "analysis_type": "co_occurrence",
                    "source_doctype": "Sales Invoice",
                    "relationships": ["sales_invoice_items"],
                    "dimensions": ["Sales Invoice Item.item_code"],
                    "filters": [
                        {"field": "docstatus", "operator": "=", "value": 1},
                        {"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-12-31"]},
                    ],
                    "limit": 10,
                    "final_answer_shape": "ranking",
                },
            },
            {
                "question": "Compare sales invoices with stock movements by warehouse.",
                "tool": "unsupported",
                "reason": "This needs unsupported cross-doctype or multi-hop analytics. Return needs_clarification=1.",
            },
        ],
        "schema_snippets": schema_snippets,
        "analytics_capabilities": analytics_capabilities,
        "response_shape": {
            "tool": "get_list | analytics_plan",
            "confidence": 0.85,
            "final_answer_shape": "table | ranking | time_series | comparison | number",
            "needs_clarification": 0,
            "clarification_question": "",
            "get_list_plan": {
                "doctype": "DocType name from schema_snippets",
                "fields": ["safe_fieldname_1", "safe_fieldname_2"],
                "filters": [{"field": "fieldname", "operator": "=", "value": "value"}],
                "order_by": {"field": "fieldname", "direction": "asc|desc"},
                "limit": 10,
            },
            "analytics_plan": {
                "analysis_type": "top_n | time_bucket_aggregate | period_comparison | ratio | trend | co_occurrence",
                "source_doctype": "Source DocType from analytics_capabilities",
                "metrics": ["metric_name"],
                "dimensions": ["fieldname or Related DocType.fieldname"],
                "relationships": ["approved_relationship_key"],
                "filters": [{"field": "fieldname", "operator": "=", "value": "value"}],
                "time_bucket": {"date_field": "posting_date", "grain": "month"},
                "comparison": {
                    "date_field": "posting_date",
                    "current": ["YYYY-MM-DD", "YYYY-MM-DD"],
                    "previous": ["YYYY-MM-DD", "YYYY-MM-DD"],
                },
                "numerator_metric": "metric_name",
                "denominator_metric": "metric_name",
                "sort": [{"by": "metric|dimension", "name": "field_or_metric", "direction": "asc|desc"}],
                "limit": 10,
            },
        },
    }
    return [
        {
            "role": "user",
            "parts": [
                "You plan one safe ERP read-only answer path. Return JSON only. "
                "Choose exactly one safe tool: get_list or analytics_plan. "
                "Use get_list for simple record or list retrieval. "
                "Use analytics_plan for aggregate, grouped, ranked, by-month, comparison, ratio, trend, declining, "
                "or recurring item-pair questions. "
                "When preferred_tool_hint is get_list, do not pick analytics_plan unless the question clearly requires "
                "aggregation or ranking. "
                "Never use analytics_plan when the user only wants a list of records, latest records, or open records. "
                "Use only the provided safe schema snippets, metric registry, and approved relationship graph. "
                "Never output SQL, joins, query builder code, write actions, or explanations outside JSON. "
                "Only use approved relationship keys for analytics relationships. "
                "For transactional analytics, prefer docstatus=1 unless the user explicitly asks for drafts. "
                "For unpaid or outstanding questions, prefer the outstanding_amount metric rather than guessing a status filter. "
                "Use analysis_type=co_occurrence only when the user explicitly asks for pairs, together, bought with, "
                "or sold together. Do not use co_occurrence otherwise. "
                "Never use more than one relationship key and never plan multi-hop analytics unless explicitly listed as supported. "
                "Use only source_doctype values from analytics_capabilities. Phase 4F analytics sources are limited to "
                "Sales Invoice and Purchase Invoice. "
                "For time_bucket_aggregate or trend, do not sort by the raw date field; omit sort and rely on chronological bucket ordering. "
                "When the question implies a relative date like this year, resolve it using today. "
                "If the request is unclear or unsupported, set needs_clarification=1 and explain why."
            ],
        },
        {"role": "model", "parts": ["Understood. I will return JSON only with one safe tool choice."]},
        {"role": "user", "parts": [json.dumps(payload, sort_keys=True, default=str)]},
    ]


def _build_planner_schema_snippets(doctype_names: list[str]) -> list[dict[str, Any]]:
    safe_slice = build_safe_schema_slice(doctype_names)
    snippets: list[dict[str, Any]] = []
    for entry in safe_slice.get("doctypes") or []:
        selected_fields = _select_planner_fields(
            entry,
            max_fields=_PLANNER_SCHEMA_FIELD_LIMIT,
        )
        fields = []
        for field in selected_fields:
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
                "suggested_fields": [
                    field.get("fieldname")
                    for field in selected_fields[:_PLANNER_SUGGESTED_FIELD_LIMIT]
                    if str(field.get("fieldname") or "").strip()
                ],
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


def _build_analytics_capability_snippets(*, settings: Any | None = None) -> list[dict[str, Any]]:
    policies = load_allowed_doctype_policies(settings=settings)
    allowed_source_doctypes = set(get_phase4f_analytics_source_doctypes(settings=settings))
    metric_rows = [
        metric
        for metric in list_metrics()
        if metric.get("source_doctype") in allowed_source_doctypes
    ]
    source_doctypes = sorted({str(metric.get("source_doctype") or "").strip() for metric in metric_rows if metric.get("source_doctype")})
    if not source_doctypes:
        return []

    schema_by_name = {
        entry.get("name"): entry
        for entry in (build_safe_schema_slice(source_doctypes, settings=settings).get("doctypes") or [])
        if entry.get("name")
    }
    snippets: list[dict[str, Any]] = []
    for source_doctype in source_doctypes:
        policy = policies.get(source_doctype) or {}
        source_entry = schema_by_name.get(source_doctype) or {}
        field_policy = get_analytics_field_policy(source_doctype, settings=settings)
        relationships = []
        for relationship in list_relationships(source_doctype=source_doctype):
            relationship_key = str(relationship.get("relationship_key") or "").strip()
            dimension_fields = sorted(get_allowed_relationship_fields(relationship_key, purpose="dimension"))
            filter_fields = sorted(get_allowed_relationship_fields(relationship_key, purpose="filter"))
            co_occurrence_fields = sorted(get_allowed_relationship_fields(relationship_key, purpose="co_occurrence"))
            if not (dimension_fields or filter_fields or co_occurrence_fields):
                continue
            relationships.append(
                {
                    "relationship_key": relationship_key,
                    "target_doctype": relationship.get("target_doctype"),
                    "relationship_type": relationship.get("relationship_type"),
                    "target_dimension_hints": _relationship_dimension_hints(relationship),
                    "allowed_dimension_fields": dimension_fields,
                    "allowed_filter_fields": filter_fields,
                    "allowed_co_occurrence_fields": co_occurrence_fields,
                }
            )
        snippets.append(
            {
                "source_doctype": source_doctype,
                "default_date_field": policy.get("default_date_field") or "",
                "large_table_requires_date_filter": cint(policy.get("large_table_requires_date_filter")),
                "allow_query_builder": cint(policy.get("allow_query_builder")),
                "allow_child_tables": cint(policy.get("allow_child_tables")),
                "default_limit": policy.get("default_limit"),
                "suggested_dimensions": _build_analytics_dimension_hints(
                    source_entry,
                    source_doctype=source_doctype,
                    settings=settings,
                ),
                "metrics": [
                    {
                        "metric_name": metric.get("metric_name"),
                        "label": metric.get("label"),
                        "description": metric.get("description"),
                        "analysis_types": metric.get("analysis_types") or [],
                        "relationship_key": metric.get("relationship_key") or "",
                    }
                    for metric in metric_rows
                    if metric.get("source_doctype") == source_doctype
                ],
                "relationships": relationships,
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
            "doctype": _first_present(first_step, "doctype", "doc_type", "doctype_name", "selected_doctype"),
            "fields": first_step.get("fields") if isinstance(first_step.get("fields"), list) else first_step.get("columns"),
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
        "doctype": str(_first_present(payload, "doctype", "doc_type", "doctype_name", "selected_doctype") or "").strip(),
        "fields": payload.get("fields") if isinstance(payload.get("fields"), list) else (
            payload.get("columns") if isinstance(payload.get("columns"), list) else []
        ),
        "filters": filters,
        "order_by": order_by,
        "limit": limit,
        "confidence": _coerce_confidence(payload.get("confidence")),
        "final_answer_shape": str(payload.get("final_answer_shape") or "table").strip() or "table",
        "needs_clarification": cint(payload.get("needs_clarification") or 0),
        "clarification_question": str(payload.get("clarification_question") or "").strip(),
    }


def _parse_hybrid_planner_response(raw_text: str) -> dict[str, Any] | None:
    payload = _parse_json_object(raw_text)
    if not payload:
        return None

    plan_payload = payload.get("plan")
    if isinstance(plan_payload, dict):
        payload = {**payload, **plan_payload}

    tool_choice = str(
        _first_present(payload, "tool", "tool_name", "selected_tool", "plan_type") or ""
    ).strip()
    if not tool_choice:
        if payload.get("analysis_type") or payload.get("source_doctype"):
            tool_choice = ANALYTICS_TOOL_NAME
        else:
            tool_choice = SUPPORTED_TOOL
    lowered_tool_choice = tool_choice.lower()
    if lowered_tool_choice in {"analytics", "analytics_query", "analyticsplan"}:
        tool_choice = ANALYTICS_TOOL_NAME
    elif lowered_tool_choice in {"getlist", "structured_query"}:
        tool_choice = SUPPORTED_TOOL

    nested_payload = payload.get("analytics_plan") if tool_choice == ANALYTICS_TOOL_NAME else payload.get("get_list_plan")
    if isinstance(nested_payload, dict):
        payload = {**payload, **nested_payload}

    if tool_choice == SUPPORTED_TOOL:
        parsed = _parse_planner_response(json.dumps(payload, sort_keys=True, default=str))
        if not parsed:
            return None
        return {"tool": SUPPORTED_TOOL, **parsed}

    if tool_choice != ANALYTICS_TOOL_NAME:
        return {
            "tool": tool_choice,
            "confidence": _coerce_confidence(payload.get("confidence")),
            "final_answer_shape": str(payload.get("final_answer_shape") or "table").strip() or "table",
            "needs_clarification": cint(payload.get("needs_clarification") or 0),
            "clarification_question": str(payload.get("clarification_question") or "").strip(),
        }

    filters = payload.get("filters")
    if filters in (None, ""):
        filters = []
    if not isinstance(filters, list):
        return None

    relationships = payload.get("relationships")
    if relationships in (None, ""):
        relationships = []
    if not isinstance(relationships, list):
        return None

    metrics = payload.get("metrics")
    if metrics in (None, ""):
        metrics = []
    if not isinstance(metrics, list):
        return None

    dimensions = payload.get("dimensions")
    if dimensions in (None, ""):
        dimensions = []
    if not isinstance(dimensions, list):
        return None

    sort = payload.get("sort")
    if sort in (None, ""):
        sort = []
    elif isinstance(sort, dict):
        sort = [sort]
    elif not isinstance(sort, list):
        return None

    time_bucket = payload.get("time_bucket")
    if time_bucket in (None, ""):
        time_bucket = {}
    if not isinstance(time_bucket, dict):
        return None

    comparison = payload.get("comparison")
    if comparison in (None, ""):
        comparison = {}
    if not isinstance(comparison, dict):
        return None

    limit = payload.get("limit")
    if limit not in (None, ""):
        try:
            limit = cint(limit)
        except Exception:
            return None

    return {
        "tool": ANALYTICS_TOOL_NAME,
        "analysis_type": str(payload.get("analysis_type") or "").strip(),
        "source_doctype": str(
            _first_present(payload, "source_doctype", "doctype", "doc_type", "doctype_name", "selected_doctype") or ""
        ).strip(),
        "metrics": [str(value).strip() for value in metrics if str(value).strip()],
        "dimensions": [str(value).strip() for value in dimensions if str(value).strip()],
        "filters": filters,
        "relationships": [str(value).strip() for value in relationships if str(value).strip()],
        "time_bucket": time_bucket,
        "comparison": comparison,
        "numerator_metric": str(payload.get("numerator_metric") or "").strip(),
        "denominator_metric": str(payload.get("denominator_metric") or "").strip(),
        "sort": sort,
        "limit": limit,
        "confidence": _coerce_confidence(payload.get("confidence")),
        "final_answer_shape": str(payload.get("final_answer_shape") or "table").strip() or "table",
        "needs_clarification": cint(payload.get("needs_clarification") or 0),
        "clarification_question": str(payload.get("clarification_question") or "").strip(),
    }


def _parse_json_object(raw_text: str) -> dict[str, Any] | None:
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
    return payload


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = 1.0
    return max(0.0, min(confidence, 1.0))


def _select_planner_fields(entry: dict[str, Any], *, max_fields: int) -> list[dict[str, Any]]:
    query_policy = entry.get("query_policy") or {}
    default_date_field = str(query_policy.get("default_date_field") or "").strip()
    default_title_field = str(query_policy.get("default_title_field") or "").strip()
    candidates = _build_planner_field_candidates(entry)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, field in enumerate(candidates):
        scored.append(
            (
                _score_planner_field(
                    field,
                    default_date_field=default_date_field,
                    default_title_field=default_title_field,
                ),
                index,
                field,
            )
        )
    return [
        field
        for _score, _index, field in sorted(scored, key=lambda row: (-row[0], row[1]))[:max(1, cint(max_fields))]
    ]


def _build_planner_field_candidates(entry: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    for field in _PLANNER_STANDARD_FIELDS:
        fieldname = str(field.get("fieldname") or "").strip()
        if fieldname and fieldname not in seen:
            candidates.append(dict(field))
            seen.add(fieldname)

    for field in (entry.get("fields") or []):
        fieldname = str(field.get("fieldname") or "").strip()
        if not fieldname or fieldname in seen:
            continue
        candidates.append(field)
        seen.add(fieldname)

    return candidates


def _build_analytics_dimension_hints(
    entry: dict[str, Any],
    *,
    source_doctype: str,
    settings: Any | None,
) -> list[str]:
    field_policy = get_analytics_field_policy(source_doctype, settings=settings)
    allowed_dimensions = set(field_policy.get("source_dimensions") or [])
    if not allowed_dimensions:
        return []

    hints: list[str] = []
    for field in _select_planner_fields(entry or {}, max_fields=_PLANNER_SCHEMA_FIELD_LIMIT):
        fieldname = str(field.get("fieldname") or "").strip()
        if fieldname in allowed_dimensions:
            hints.append(fieldname)
    for fieldname in sorted(allowed_dimensions):
        if fieldname not in hints:
            hints.append(fieldname)
    return hints[:_ANALYTICS_DIMENSION_FIELD_LIMIT]


def _relationship_dimension_hints(relationship: dict[str, Any]) -> list[str]:
    hints: list[str] = []
    target_doctype = str(relationship.get("target_doctype") or "").strip()
    relationship_key = str(relationship.get("relationship_key") or "").strip()
    if not target_doctype or not relationship_key:
        return hints

    for fieldname in sorted(get_allowed_relationship_fields(relationship_key, purpose="dimension")):
        hint = f"{target_doctype}.{fieldname}"
        hints.append(hint)
    return hints[:_ANALYTICS_DIMENSION_FIELD_LIMIT]


def _score_planner_field(
    field: dict[str, Any],
    *,
    default_date_field: str,
    default_title_field: str,
) -> int:
    fieldname = str(field.get("fieldname") or "").strip()
    score = 0

    if fieldname == "name":
        score += 1_000
    elif fieldname == default_title_field:
        score += 920
    elif fieldname == default_date_field:
        score += 880
    elif fieldname in {"modified", "creation", "docstatus"}:
        score += 260

    if cint(field.get("in_list_view")):
        score += 240
    if cint(field.get("in_standard_filter")):
        score += 140
    if cint(field.get("reqd")):
        score += 10

    score += _planner_field_keyword_score(field)
    return score


def _planner_field_keyword_score(field: dict[str, Any]) -> int:
    haystack = " ".join(
        part
        for part in (
            str(field.get("fieldname") or "").replace("_", " "),
            str(field.get("label") or ""),
        )
        if part
    ).lower()
    tokens = {token for token in re.findall(r"[a-z0-9]+", haystack) if token}
    return sum(weight for token, weight in _PLANNER_FIELD_KEYWORDS.items() if token in tokens)


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_selected_doctype(doctype: str, allowed_doctypes: set[str]) -> str:
    candidate = str(doctype or "").strip().strip("`\"'")
    if not candidate:
        return ""
    if candidate in allowed_doctypes:
        return candidate

    normalized = " ".join(candidate.split()).lower()
    for allowed in allowed_doctypes:
        if normalized == allowed.lower():
            return allowed
    return candidate


def _run_planner_chat(*, messages: list[dict[str, Any]], api_key: str) -> str:
    if _planner_test_fault_enabled("planner_timeout"):
        raise TimeoutError("Injected planner timeout for Phase 4F matrix.")
    if _planner_test_fault_enabled("bad_planner_json"):
        return "planner returned invalid json"

    from frapperag.rag.chat_engine import get_chat_runtime_settings
    from frapperag.rag.sidecar_client import chat

    runtime = get_chat_runtime_settings()
    response = chat(
        messages=messages,
        api_key=api_key,
        model=runtime["model"],
        tools=None,
    )
    return (response.get("text") or "").strip()


def _hybrid_log_details(
    *,
    route: dict[str, Any],
    hybrid_branch: str,
    analysis_type: str,
    final_answer_shape: str,
    result_status: str,
    fallback_reason: str,
    empty_result: int,
) -> dict[str, Any]:
    return {
        "hybrid_branch": hybrid_branch,
        "route_confidence": float(route.get("confidence") or 0.0),
        "candidate_doctypes": route.get("candidate_doctypes") or [],
        "analysis_type": analysis_type,
        "final_answer_shape": final_answer_shape,
        "result_status": result_status,
        "fallback_reason": fallback_reason,
        "empty_result": cint(empty_result),
    }


def _preferred_tool_hint(question: str) -> str:
    return SUPPORTED_TOOL if _question_prefers_get_list(question) else ANALYTICS_TOOL_NAME


def _question_prefers_get_list(question: str) -> bool:
    normalized = " ".join((question or "").strip().lower().split())
    tokens = {token for token in re.findall(r"[a-z0-9\u0600-\u06FF]+", normalized) if token}
    if any(phrase in normalized for phrase in _GET_LIST_PREFERENCE_PHRASES):
        return True
    if tokens & _GET_LIST_PREFERENCE_TOKENS and not tokens & _AGGREGATE_HINT_TOKENS:
        return True
    return False


def _analytics_policy_limit_for(source_doctype: str) -> int:
    policy = get_allowed_doctype_policy(source_doctype) or {}
    return cint(policy.get("default_limit") or DEFAULT_LIMIT) or DEFAULT_LIMIT


def _clamp_analytics_limit(*, requested_limit: int, final_answer_shape: str, policy_limit: int) -> int:
    safe_policy_limit = max(1, cint(policy_limit or DEFAULT_LIMIT))
    shape_cap = _PHASE4F_ANALYTICS_LIMIT_CAPS.get((final_answer_shape or "table").strip() or "table", 10)
    limit = cint(requested_limit or 0) or safe_policy_limit
    limit = max(1, limit)
    return min(limit, safe_policy_limit, shape_cap)


def _analytics_plan_guardrail_rejection(
    *,
    question: str,
    parsed: dict[str, Any],
    source_doctype: str,
    analysis_type: str,
) -> tuple[str, str]:
    if _question_prefers_get_list(question):
        return (
            "Planner selected analytics for a direct record-list question that should use get_list.",
            "prefer_get_list",
        )

    if analysis_type == ANALYSIS_TYPE_CO_OCCURRENCE and not _question_allows_co_occurrence(question):
        return (
            "Planner selected co_occurrence without an explicit item-pair cue.",
            "co_occurrence_requires_explicit_pair_cue",
        )

    relationship_keys = _relationship_keys_from_planner_payload(parsed)
    if len(relationship_keys) > 1:
        return (
            "Planner selected unsupported multi-hop analytics. Phase 4F allows at most one relationship.",
            "multi_hop_not_supported",
        )

    metric_guardrail = _metric_guardrail_rejection(question, parsed, source_doctype=source_doctype)
    if metric_guardrail:
        return metric_guardrail

    return "", ""


def _question_allows_co_occurrence(question: str) -> bool:
    normalized = " ".join((question or "").strip().lower().split())
    return any(phrase in normalized for phrase in _CO_OCCURRENCE_CUE_PHRASES)


def _relationship_keys_from_planner_payload(parsed: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for key in (parsed.get("relationships") or []):
        candidate = str(key or "").strip()
        if candidate and candidate not in seen:
            keys.append(candidate)
            seen.add(candidate)
    target_doctypes: set[str] = set()
    for collection_key in ("dimensions", "filters"):
        values = parsed.get(collection_key) or []
        if collection_key == "filters":
            values = [entry.get("field") for entry in values if isinstance(entry, dict)]
        for value in values:
            if "." not in str(value or ""):
                continue
            target_doctype = str(value).rsplit(".", 1)[0].strip()
            if target_doctype:
                target_doctypes.add(target_doctype)
    if len(target_doctypes) > 1:
        keys.extend(sorted(target_doctypes))
    return keys


def _metric_guardrail_rejection(
    question: str,
    parsed: dict[str, Any],
    *,
    source_doctype: str,
) -> tuple[str, str]:
    normalized = " ".join((question or "").strip().lower().split())
    selected_metrics = {
        str(metric or "").strip()
        for metric in (parsed.get("metrics") or [])
        if str(metric or "").strip()
    }
    for metric_name in ("numerator_metric", "denominator_metric"):
        value = str(parsed.get(metric_name) or "").strip()
        if value:
            selected_metrics.add(value)

    if not selected_metrics:
        return "", ""

    for expected_metric, phrases in _METRIC_GUARDRAIL_PHRASES.items():
        if not any(phrase in normalized for phrase in phrases):
            continue
        if expected_metric.startswith("sales_") and source_doctype != "Sales Invoice":
            continue
        if expected_metric.startswith("purchase_") and source_doctype != "Purchase Invoice":
            continue
        if expected_metric not in selected_metrics:
            return (
                f"Planner selected metrics {sorted(selected_metrics)!r} but the question indicates '{expected_metric}'.",
                "metric_mismatch",
            )
    return "", ""


def _ensure_required_source_date_filter(parsed: dict[str, Any], *, source_doctype: str) -> None:
    policy = get_allowed_doctype_policy(source_doctype) or {}
    if not cint(policy.get("large_table_requires_date_filter")):
        return

    default_date_field = str(policy.get("default_date_field") or "").strip()
    if not default_date_field:
        return

    for current in (parsed.get("filters") or []):
        if not isinstance(current, dict):
            continue
        if str(current.get("field") or "").strip() == default_date_field and str(current.get("operator") or "").strip() in {"between", ">=", "<="}:
            return

    time_bucket = parsed.get("time_bucket") or {}
    if str(time_bucket.get("date_field") or "").strip() == default_date_field:
        return

    comparison = parsed.get("comparison") or {}
    if str(comparison.get("date_field") or "").strip() == default_date_field:
        return

    start_of_year = _dt.date.today().replace(month=1, day=1).isoformat()
    end_of_year = _dt.date.today().replace(month=12, day=31).isoformat()
    filters = list(parsed.get("filters") or [])
    filters.append(
        {
            "field": default_date_field,
            "operator": "between",
            "value": [start_of_year, end_of_year],
        }
    )
    parsed["filters"] = filters


def _planner_test_fault_enabled(name: str) -> bool:
    faults = getattr(frappe.flags, "frapperag_test_faults", None) or {}
    return bool(cint(faults.get(name)))


def _log_planner_rejection(
    *,
    request_id: str,
    reason: str,
    route: dict[str, Any],
    log_operation: str = "planner.plan_structured_query",
    tool_name: str = SUPPORTED_TOOL,
    intent: str = INTENT_STRUCTURED_QUERY,
    details: dict[str, Any] | None = None,
) -> None:
    log_tool_call(
        log_operation,
        "Rejected",
        tool_name=tool_name,
        doctype_name=",".join((route.get("candidate_doctypes") or [])[:3]),
        request_id=request_id,
        intent=intent,
        error_message=(reason or "Planner rejected the request.").strip(),
        details={
            **_hybrid_log_details(
                route=route,
                hybrid_branch="planner",
                analysis_type="",
                final_answer_shape="",
                result_status="rejected",
                fallback_reason="planner_rejected",
                empty_result=0,
            ),
            "route": {
                "selected_intent": route.get("selected_intent"),
                "confidence": route.get("confidence"),
                "reason": route.get("reason"),
                "candidate_doctypes": route.get("candidate_doctypes") or [],
            },
            **(details or {}),
        },
    )
