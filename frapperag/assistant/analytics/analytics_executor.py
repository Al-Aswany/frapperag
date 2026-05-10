from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import cint
from frappe.query_builder import Order
from frappe.query_builder.functions import Avg, Count, DateFormat, Sum

from frapperag.assistant.tool_call_log import build_analytics_log_details, log_tool_call

from .analytics_plan_schema import (
    ANALYSIS_TYPE_BOTTOM_N,
    ANALYSIS_TYPE_CO_OCCURRENCE,
    ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
    ANALYSIS_TYPE_PERIOD_COMPARISON,
    ANALYSIS_TYPE_RATIO,
    ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
    ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
    ANALYSIS_TYPE_TOP_N,
    ANALYSIS_TYPE_TREND,
)
from .analytics_post_processors import build_period_comparison_rows, limit_rows, serialize_rows, sort_rows
from .analytics_validator import VALIDATOR_VERSION, validate_analytics_plan
from .metric_registry import get_metric_definition
from .relationship_graph import get_relationship


EXECUTOR_VERSION = "phase4d_option_a_v1_1"
_SUPPORTED_TIME_GRAINS = {"day", "month", "year"}
_IMPLEMENTED_ANALYSIS_TYPES = (
    ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
    ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
    ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
    ANALYSIS_TYPE_PERIOD_COMPARISON,
    ANALYSIS_TYPE_CO_OCCURRENCE,
    ANALYSIS_TYPE_TOP_N,
    ANALYSIS_TYPE_BOTTOM_N,
    ANALYSIS_TYPE_RATIO,
    ANALYSIS_TYPE_TREND,
)
_DEFERRED_ANALYSIS_TYPES: dict[str, str] = {}
_SUPPORTED_CO_OCCURRENCE_RELATIONSHIPS = {
    "sales_invoice_items",
    "purchase_invoice_items",
}


class UnsupportedAnalyticsPlanError(frappe.ValidationError):
    """Raised when a validated plan is intentionally unsupported by the Phase 4D executor."""


def execute_validated_analytics_plan(validated_plan: dict[str, Any] | str) -> dict[str, Any]:
    started = time.monotonic()
    raw_plan = _coerce_plan_snapshot(validated_plan)
    request_id = (raw_plan.get("request_id") or "").strip()
    analysis_type = (raw_plan.get("analysis_type") or "").strip()
    doctype_name = (raw_plan.get("source_doctype") or "").strip()

    try:
        plan = validate_analytics_plan(
            validated_plan,
            require_validated_flag=True,
            log_result=False,
        )
    except Exception as exc:
        _log_execution_event(
            "Rejected",
            started=started,
            plan=raw_plan,
            request_id=request_id,
            analysis_type=analysis_type,
            doctype_name=doctype_name,
            error_message=str(exc),
            details={"stage": "validation"},
        )
        raise

    try:
        result = _execute_plan(plan)
    except UnsupportedAnalyticsPlanError as exc:
        result = _build_unsupported_result(plan, reason=str(exc))
        _log_execution_event(
            "Rejected",
            started=started,
            plan=plan,
            request_id=plan.get("request_id"),
            analysis_type=plan.get("analysis_type"),
            doctype_name=plan.get("source_doctype"),
            row_count=0,
            error_message=result["error"],
            details={"status": result["status"]},
        )
        return result
    except frappe.PermissionError as exc:
        result = _build_permission_denied_result(plan, reason=str(exc))
        _log_execution_event(
            "Rejected",
            started=started,
            plan=plan,
            request_id=plan.get("request_id"),
            analysis_type=plan.get("analysis_type"),
            doctype_name=plan.get("source_doctype"),
            row_count=0,
            error_message=result["error"],
            details={
                "status": result["status"],
                "error_code": "permission_denied",
                "error_class": "PermissionError",
            },
        )
        return result
    except Exception as exc:
        _log_execution_event(
            "Failed",
            started=started,
            plan=plan,
            request_id=plan.get("request_id"),
            analysis_type=plan.get("analysis_type"),
            doctype_name=plan.get("source_doctype"),
            error_message=str(exc),
            details={"stage": "execution"},
        )
        raise

    _log_execution_event(
        "Success",
        started=started,
        plan=plan,
        request_id=plan.get("request_id"),
        analysis_type=plan.get("analysis_type"),
        doctype_name=plan.get("source_doctype"),
        row_count=result.get("row_count"),
        details={
            "status": result.get("status"),
            "column_count": len(result.get("columns") or []),
            "relationship_count": len(plan.get("relationships") or []),
        },
    )
    return result


def debug_execute_analytics_plan(plan_json: str) -> dict[str, Any]:
    return execute_validated_analytics_plan(plan_json)


def debug_validate_and_execute_analytics_plan(plan_json: str) -> dict[str, Any]:
    validated = validate_analytics_plan(plan_json, require_validated_flag=False, log_result=True)
    return execute_validated_analytics_plan(validated)


def debug_describe_analytics_executor_capabilities() -> dict[str, Any]:
    return {
        "executor_version": EXECUTOR_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "implemented_analysis_types": list(_IMPLEMENTED_ANALYSIS_TYPES),
        "deferred_analysis_types": _DEFERRED_ANALYSIS_TYPES,
        "supported_time_bucket_grains": sorted(_SUPPORTED_TIME_GRAINS),
        "notes": [
            "Execution accepts only validated analytics plan JSON.",
            "Execution uses Query Builder or controlled internal SQL only; no planner, user, or LLM SQL strings may execute.",
            "co_occurrence is limited to normalized item pairs on approved parent-child item tables.",
            "Chat runtime and hybrid orchestration are intentionally untouched in Phase 4D.",
        ],
    }


def _execute_plan(plan: dict[str, Any]) -> dict[str, Any]:
    analysis_type = plan["analysis_type"]
    if analysis_type == ANALYSIS_TYPE_CO_OCCURRENCE:
        return _execute_co_occurrence(plan)

    context = _build_execution_context(plan)
    _enforce_permissions(context)
    _enforce_executor_shape_support(plan, context)

    if analysis_type == ANALYSIS_TYPE_PERIOD_COMPARISON:
        return _execute_period_comparison(plan, context)
    if analysis_type == ANALYSIS_TYPE_RATIO:
        return _execute_ratio(plan, context)
    if analysis_type in {
        ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
        ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
        ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
        ANALYSIS_TYPE_TOP_N,
        ANALYSIS_TYPE_BOTTOM_N,
        ANALYSIS_TYPE_TREND,
    }:
        return _execute_aggregate_plan(plan, context)

    raise UnsupportedAnalyticsPlanError(f"Unsupported analysis type '{analysis_type}'.")


def _execute_aggregate_plan(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    rows = _run_aggregate_query(plan, context)
    rows = serialize_rows(rows)
    return _build_success_result(
        plan,
        rows=rows,
        columns=_result_columns_for_aggregate(plan),
        details={"source_doctype": plan["source_doctype"]},
    )


def _execute_co_occurrence(plan: dict[str, Any]) -> dict[str, Any]:
    relationship = _co_occurrence_relationship(plan)
    if _executor_test_fault_enabled("permission_denied"):
        raise frappe.PermissionError("Injected analytics permission denial for Phase 4F matrix.")
    frappe.has_permission(plan["source_doctype"], ptype="read", throw=True)

    if plan.get("sort"):
        raise UnsupportedAnalyticsPlanError("co_occurrence does not support explicit sort overrides in Phase 4D.1.")

    qty_field = _co_occurrence_qty_field(relationship["target_doctype"])
    sql, params = _compile_co_occurrence_sql(
        plan,
        relationship=relationship,
        qty_field=qty_field,
    )
    rows = frappe.db.sql(sql, params, as_dict=True)
    rows = serialize_rows(rows)

    columns = ["item_a", "item_b", "pair_count"]
    if qty_field:
        columns.extend(["total_qty_a", "total_qty_b"])

    return _build_success_result(
        plan,
        rows=rows,
        columns=columns,
        details={
            "source_doctype": plan["source_doctype"],
            "relationship_key": relationship["relationship_key"],
            "pair_entity_field": "item_code",
            "pair_parent_field": "parent",
            "qty_field": qty_field or "",
        },
    )


def _execute_ratio(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    ratio_plan = dict(plan)
    ratio_plan["metrics"] = [plan["numerator_metric"], plan["denominator_metric"]]
    rows = _run_aggregate_query(ratio_plan, context)

    result_rows: list[dict[str, Any]] = []
    for row in rows:
        numerator_value = row.get(plan["numerator_metric"], 0) or 0
        denominator_value = row.get(plan["denominator_metric"], 0) or 0
        ratio_value = None
        if denominator_value not in (None, 0):
            ratio_value = float(numerator_value) / float(denominator_value)
        serialized = dict(row)
        serialized["ratio"] = ratio_value
        result_rows.append(serialized)

    result_rows = sort_rows(
        result_rows,
        sort_spec=_ratio_sort_spec(plan),
        default_sort=_default_sort_spec(plan),
    )
    result_rows = limit_rows(result_rows, plan["limit"])
    result_rows = serialize_rows(result_rows)
    return _build_success_result(
        plan,
        rows=result_rows,
        columns=list(plan.get("dimensions") or [])
        + [plan["numerator_metric"], plan["denominator_metric"], "ratio"],
        details={"source_doctype": plan["source_doctype"]},
    )


def _execute_period_comparison(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    comparison = plan["comparison"]
    current_filters = _date_range_filter(comparison["date_field"], comparison["current"])
    previous_filters = _date_range_filter(comparison["date_field"], comparison["previous"])

    current_rows = _run_aggregate_query(plan, context, extra_filters=[current_filters], apply_limit=False)
    previous_rows = _run_aggregate_query(plan, context, extra_filters=[previous_filters], apply_limit=False)

    merged_rows = build_period_comparison_rows(
        current_rows=current_rows,
        previous_rows=previous_rows,
        dimensions=plan.get("dimensions") or [],
        metrics=plan.get("metrics") or [],
    )
    merged_rows = sort_rows(
        merged_rows,
        sort_spec=_period_comparison_sort_spec(plan),
        default_sort=_period_comparison_default_sort_spec(plan),
    )
    merged_rows = limit_rows(merged_rows, plan["limit"])
    merged_rows = serialize_rows(merged_rows)
    return _build_success_result(
        plan,
        rows=merged_rows,
        columns=_result_columns_for_period_comparison(plan),
        details={
            "source_doctype": plan["source_doctype"],
            "comparison": comparison,
        },
    )


def _run_aggregate_query(
    plan: dict[str, Any],
    context: dict[str, Any],
    *,
    extra_filters: list[dict[str, Any]] | None = None,
    apply_limit: bool = True,
) -> list[dict[str, Any]]:
    query = context["query"]
    alias_map: dict[str, str] = {}
    order_terms_by_name: dict[str, Any] = {}
    grouped = False

    for index, dimension in enumerate(plan.get("dimensions") or [], start=1):
        alias = f"dimension_{index}"
        dimension_term = _resolve_field_term(dimension, context)
        alias_map[alias] = dimension
        order_terms_by_name[dimension] = dimension_term
        query = query.select(dimension_term.as_(alias)).groupby(dimension_term)
        grouped = True

    time_bucket = plan.get("time_bucket") or {}
    if time_bucket:
        grain = (time_bucket.get("grain") or "").strip().lower()
        if grain not in _SUPPORTED_TIME_GRAINS:
            raise UnsupportedAnalyticsPlanError(
                f"Unsupported time bucket grain '{grain}'. Phase 4D supports only day, month, and year."
            )
        label_term = _build_time_bucket_term(_resolve_field_term(time_bucket["date_field"], context), grain)
        alias_map["time_bucket"] = "time_bucket"
        order_terms_by_name["time_bucket"] = label_term
        query = query.select(label_term.as_("time_bucket")).groupby(label_term)
        grouped = True

    for metric_name in plan.get("metrics") or []:
        metric_term = _build_metric_term(metric_name, context)
        order_terms_by_name[metric_name] = metric_term
        query = query.select(metric_term.as_(metric_name))

    for current_filter in (plan.get("filters") or []) + (extra_filters or []):
        query = query.where(_build_filter_criterion(current_filter, context))

    for entry in _query_sort_spec(plan):
        order_term = order_terms_by_name[entry["name"]]
        direction = Order.desc if entry["direction"] == "desc" else Order.asc
        query = query.orderby(order_term, order=direction)

    if grouped and apply_limit:
        query = query.limit(plan["limit"])

    rows = query.run(as_dict=True)
    renamed_rows = [_rename_row_aliases(row, alias_map) for row in rows]
    return _normalize_metric_values(renamed_rows, plan.get("metrics") or [])


def _build_execution_context(plan: dict[str, Any]) -> dict[str, Any]:
    relationship_keys = list(plan.get("relationships") or [])
    if len(relationship_keys) > 1:
        raise UnsupportedAnalyticsPlanError(
            "Validated analytics plans with more than one relationship are not supported in Phase 4D."
        )

    relationship = get_relationship(relationship_keys[0]) if relationship_keys else None
    source_table = frappe.qb.DocType(plan["source_doctype"]).as_("analytics_source")
    target_table = None
    query = frappe.qb.from_(source_table)

    if relationship:
        if relationship["relationship_type"] == "dynamic_link":
            raise UnsupportedAnalyticsPlanError(
                f"Relationship '{relationship['relationship_key']}' is dynamic and not executable in Phase 4D."
            )

        target_table = frappe.qb.DocType(relationship["target_doctype"]).as_("analytics_related")
        if relationship["relationship_type"] == "child_table":
            query = query.left_join(target_table).on(
                (target_table.parent == source_table.name)
                & (target_table.parenttype == plan["source_doctype"])
                & (target_table.parentfield == relationship["source_field"])
            )
        elif relationship["relationship_type"] == "link":
            query = query.left_join(target_table).on(
                source_table[relationship["source_field"]] == target_table[relationship["target_field"]]
            )
        else:
            raise UnsupportedAnalyticsPlanError(
                f"Relationship type '{relationship['relationship_type']}' is not executable in Phase 4D."
            )

    return {
        "source_table": source_table,
        "target_table": target_table,
        "relationship": relationship,
        "query": query,
    }


def _enforce_permissions(context: dict[str, Any]) -> None:
    if _executor_test_fault_enabled("permission_denied"):
        raise frappe.PermissionError("Injected analytics permission denial for Phase 4F matrix.")
    relationship = context.get("relationship")
    source_table = context["source_table"]
    source_doctype = source_table._table_name.replace("tab", "", 1)
    frappe.has_permission(source_doctype, ptype="read", throw=True)

    if relationship and relationship["relationship_type"] == "link":
        frappe.has_permission(relationship["target_doctype"], ptype="read", throw=True)


def _enforce_executor_shape_support(plan: dict[str, Any], context: dict[str, Any]) -> None:
    relationship = context.get("relationship")
    relationship_key = (relationship or {}).get("relationship_key") or ""
    child_relationship = bool(relationship and relationship["relationship_type"] == "child_table")

    metric_defs = [get_metric_definition(metric_name) or {} for metric_name in (plan.get("metrics") or [])]
    ratio_metric_defs = [
        get_metric_definition(plan.get("numerator_metric") or "") or {},
        get_metric_definition(plan.get("denominator_metric") or "") or {},
    ]

    all_metric_defs = [definition for definition in metric_defs + ratio_metric_defs if definition]
    source_metric_defs = [definition for definition in all_metric_defs if not definition.get("relationship_key")]
    related_metric_defs = [definition for definition in all_metric_defs if definition.get("relationship_key")]

    related_dimensions = [dimension for dimension in (plan.get("dimensions") or []) if "." in dimension]
    related_filters = [current for current in (plan.get("filters") or []) if "." in str(current.get("field") or "")]

    if child_relationship:
        related_metric_keys = {definition.get("relationship_key") for definition in related_metric_defs}
        if any(key != relationship_key for key in related_metric_keys):
            raise UnsupportedAnalyticsPlanError(
                "Validated child-table metrics must all use the same approved relationship."
            )
        if source_metric_defs and related_metric_defs:
            raise UnsupportedAnalyticsPlanError(
                "Mixed source metrics and child-table metrics are not supported in one Phase 4D execution."
            )
        if source_metric_defs and (related_dimensions or related_filters):
            raise UnsupportedAnalyticsPlanError(
                "Source metrics cannot be combined with child-table dimensions or filters in Phase 4D because that can duplicate parent aggregates."
            )

    if plan["analysis_type"] == ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE:
        if not child_relationship:
            raise UnsupportedAnalyticsPlanError(
                "parent_child_aggregate currently supports only approved child-table relationships."
            )


def _resolve_field_term(field_ref: str, context: dict[str, Any]) -> Any:
    relationship = context.get("relationship")
    source_table = context["source_table"]
    target_table = context.get("target_table")

    if "." not in field_ref:
        return source_table[field_ref]

    if not relationship or not target_table:
        raise UnsupportedAnalyticsPlanError(f"Field '{field_ref}' requires a relationship join that is not available.")

    target_doctype, fieldname = field_ref.rsplit(".", 1)
    if target_doctype != relationship["target_doctype"]:
        raise UnsupportedAnalyticsPlanError(
            f"Field '{field_ref}' is outside the single approved relationship supported in Phase 4D."
        )
    return target_table[fieldname]


def _build_metric_term(metric_name: str, context: dict[str, Any]) -> Any:
    definition = get_metric_definition(metric_name)
    if not definition:
        raise UnsupportedAnalyticsPlanError(f"Metric '{metric_name}' is not registered.")

    field_term = _resolve_metric_field(definition, context)
    aggregation = (definition.get("aggregation") or "").strip().lower()
    if aggregation == "sum":
        return Sum(field_term)
    if aggregation == "avg":
        return Avg(field_term)
    if aggregation == "count":
        return Count(field_term)
    raise UnsupportedAnalyticsPlanError(f"Aggregation '{aggregation}' is not supported for metric '{metric_name}'.")


def _resolve_metric_field(definition: dict[str, Any], context: dict[str, Any]) -> Any:
    relationship_key = (definition.get("relationship_key") or "").strip()
    if relationship_key:
        relationship = context.get("relationship") or {}
        if relationship.get("relationship_key") != relationship_key:
            raise UnsupportedAnalyticsPlanError(
                f"Metric '{definition['metric_name']}' requires relationship '{relationship_key}'."
            )
        return context["target_table"][definition["value_field"]]
    return context["source_table"][definition["value_field"]]


def _build_filter_criterion(current_filter: dict[str, Any], context: dict[str, Any]) -> Any:
    term = _resolve_field_term(current_filter["field"], context)
    operator = current_filter["operator"]
    value = current_filter["value"]
    if operator == "=":
        return term == value
    if operator == "in":
        return term.isin(value)
    if operator == "between":
        return (term >= value[0]) & (term <= value[1])
    if operator == ">=":
        return term >= value
    if operator == "<=":
        return term <= value
    if operator == "like_prefix":
        return term.like(f"{value}%")
    raise UnsupportedAnalyticsPlanError(f"Filter operator '{operator}' is not executable.")


def _build_time_bucket_term(field_term: Any, grain: str) -> Any:
    if grain == "day":
        return DateFormat(field_term, _date_format_for_grain("day"))
    if grain == "month":
        return DateFormat(field_term, _date_format_for_grain("month"))
    if grain == "year":
        return DateFormat(field_term, _date_format_for_grain("year"))
    raise UnsupportedAnalyticsPlanError(
        f"Unsupported time bucket grain '{grain}'. Phase 4D supports only day, month, and year."
    )


def _date_format_for_grain(grain: str) -> str:
    if frappe.db.db_type == "postgres":
        return {"day": "YYYY-MM-DD", "month": "YYYY-MM", "year": "YYYY"}[grain]
    return {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}[grain]


def _query_sort_spec(plan: dict[str, Any]) -> list[dict[str, Any]]:
    sorts = list(plan.get("sort") or [])
    if not sorts:
        return _default_sort_spec(plan)

    query_sort: list[dict[str, Any]] = []
    for entry in sorts:
        query_sort.append({"direction": entry["direction"], "name": entry["name"]})
    return query_sort


def _default_sort_spec(plan: dict[str, Any]) -> list[dict[str, Any]]:
    analysis_type = plan["analysis_type"]
    metrics = plan.get("metrics") or []
    dimensions = plan.get("dimensions") or []
    if analysis_type == ANALYSIS_TYPE_BOTTOM_N and metrics:
        return [{"direction": "asc", "name": metrics[0]}]
    if analysis_type in {ANALYSIS_TYPE_TOP_N, ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE} and metrics:
        return [{"direction": "desc", "name": metrics[0]}]
    if analysis_type in {ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE, ANALYSIS_TYPE_TREND}:
        return [{"direction": "asc", "name": "time_bucket"}]
    if dimensions:
        return [{"direction": "asc", "name": dimensions[0]}]
    return []


def _period_comparison_sort_spec(plan: dict[str, Any]) -> list[dict[str, Any]]:
    sorts = list(plan.get("sort") or [])
    if not sorts:
        return []
    normalized: list[dict[str, Any]] = []
    for entry in sorts:
        name = entry["name"]
        if entry["by"] == "metric":
            normalized.append({"name": f"{name}_current", "direction": entry["direction"]})
        else:
            normalized.append({"name": name, "direction": entry["direction"]})
    return normalized


def _period_comparison_default_sort_spec(plan: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = plan.get("metrics") or []
    dimensions = plan.get("dimensions") or []
    if metrics:
        return [{"name": f"{metrics[0]}_current", "direction": "desc"}]
    if dimensions:
        return [{"name": dimensions[0], "direction": "asc"}]
    return []


def _ratio_sort_spec(plan: dict[str, Any]) -> list[dict[str, Any]]:
    if plan.get("sort"):
        normalized: list[dict[str, Any]] = []
        for entry in plan["sort"]:
            if entry["by"] == "metric":
                normalized.append({"name": entry["name"], "direction": entry["direction"]})
            else:
                normalized.append({"name": entry["name"], "direction": entry["direction"]})
        return normalized
    return [{"name": "ratio", "direction": "desc"}]


def _rename_row_aliases(row: dict[str, Any], alias_map: dict[str, str]) -> dict[str, Any]:
    renamed: dict[str, Any] = {}
    for key, value in row.items():
        renamed[alias_map.get(key, key)] = value
    return renamed


def _normalize_metric_values(rows: list[dict[str, Any]], metrics: list[str]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        for metric_name in metrics:
            if current.get(metric_name) is None:
                current[metric_name] = 0
        normalized.append(current)
    return normalized


def _date_range_filter(field: str, value: list[str]) -> dict[str, Any]:
    return {"field": field, "operator": "between", "value": value}


def _result_columns_for_aggregate(plan: dict[str, Any]) -> list[str]:
    columns = list(plan.get("dimensions") or [])
    if plan.get("time_bucket"):
        columns.append("time_bucket")
    columns.extend(plan.get("metrics") or [])
    return columns


def _result_columns_for_period_comparison(plan: dict[str, Any]) -> list[str]:
    columns = list(plan.get("dimensions") or [])
    for metric_name in plan.get("metrics") or []:
        columns.extend(
            [
                f"{metric_name}_current",
                f"{metric_name}_previous",
                f"{metric_name}_delta",
                f"{metric_name}_pct_change",
            ]
        )
    return columns


def _build_success_result(
    plan: dict[str, Any],
    *,
    rows: list[dict[str, Any]],
    columns: list[str],
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "request_id": plan.get("request_id"),
        "intent": plan.get("intent"),
        "validated": 1,
        "validator_version": plan.get("validator_version"),
        "executor_version": EXECUTOR_VERSION,
        "analysis_type": plan.get("analysis_type"),
        "source_doctype": plan.get("source_doctype"),
        "status": "success",
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "details": details or {},
    }


def _build_unsupported_result(plan: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "request_id": plan.get("request_id"),
        "intent": plan.get("intent"),
        "validated": 1,
        "validator_version": plan.get("validator_version"),
        "executor_version": EXECUTOR_VERSION,
        "analysis_type": plan.get("analysis_type"),
        "source_doctype": plan.get("source_doctype"),
        "status": "unsupported",
        "error": reason,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "details": {},
    }


def _build_permission_denied_result(plan: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "request_id": plan.get("request_id"),
        "intent": plan.get("intent"),
        "validated": 1,
        "validator_version": plan.get("validator_version"),
        "executor_version": EXECUTOR_VERSION,
        "analysis_type": plan.get("analysis_type"),
        "source_doctype": plan.get("source_doctype"),
        "status": "permission_denied",
        "error": reason,
        "columns": [],
        "rows": [],
        "row_count": 0,
        "details": {},
    }


def _log_execution_event(
    status: str,
    *,
    started: float,
    plan: dict[str, Any],
    request_id: str | None,
    analysis_type: str | None,
    doctype_name: str | None,
    row_count: int | None = None,
    error_message: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    error_code, error_class = _classify_execution_error(error_message or "", details or {})
    log_tool_call(
        "analytics.executor.execute_validated_analytics_plan",
        status,
        tool_name=analysis_type or "analytics",
        doctype_name=doctype_name,
        request_id=request_id,
        intent=plan.get("intent"),
        row_count=row_count,
        duration_ms=_duration_ms(started),
        error_message=error_message,
        plan=plan,
        details={
            **build_analytics_log_details(
                hybrid_branch="analytics",
                analysis_type=analysis_type or "",
                source_doctype=doctype_name or "",
                planner_mode=(plan.get("planner_mode") or ""),
                requested_limit=cint(plan.get("limit") or 0),
                effective_limit=cint(plan.get("limit") or 0),
                policy_limit=0,
                date_filter_required=0,
                date_filter_present=0,
                metrics=plan.get("metrics") or [],
                dimensions=plan.get("dimensions") or [],
                relationships=plan.get("relationships") or [],
                result_status=(details or {}).get("status") or status.lower(),
                empty_result=not bool(row_count),
                error_code=error_code,
                error_class=error_class,
            ),
            "route_confidence": 0.0,
            "candidate_doctypes": [],
            "final_answer_shape": (plan.get("final_answer_shape") or ""),
            **(details or {}),
        },
    )


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _classify_execution_error(error_message: str, details: dict[str, Any]) -> tuple[str, str]:
    if str(details.get("error_code") or "").strip():
        return (
            str(details.get("error_code") or "").strip(),
            str(details.get("error_class") or "").strip(),
        )
    lowered = (error_message or "").lower()
    if "permission" in lowered:
        return "permission_denied", "PermissionError"
    if "unsupported" in lowered:
        return "unsupported_plan", "ValidationError"
    return ("execution_failed", "ExecutionError") if lowered else ("", "")


def _executor_test_fault_enabled(name: str) -> bool:
    faults = getattr(frappe.flags, "frapperag_test_faults", None) or {}
    return bool(cint(faults.get(name)))


def _co_occurrence_relationship(plan: dict[str, Any]) -> dict[str, Any]:
    relationship_keys = list(plan.get("relationships") or [])
    if len(relationship_keys) != 1:
        raise UnsupportedAnalyticsPlanError("co_occurrence requires exactly one approved relationship.")

    relationship = get_relationship(relationship_keys[0]) or {}
    relationship_key = (relationship.get("relationship_key") or "").strip()
    if relationship_key not in _SUPPORTED_CO_OCCURRENCE_RELATIONSHIPS:
        raise UnsupportedAnalyticsPlanError(
            f"co_occurrence relationship '{relationship_key or '<empty>'}' is not supported in Phase 4D.1."
        )
    if relationship.get("relationship_type") != "child_table":
        raise UnsupportedAnalyticsPlanError("co_occurrence only supports approved parent-child relationships.")

    expected_dimension = f"{relationship['target_doctype']}.item_code"
    if plan.get("dimensions") != [expected_dimension]:
        raise UnsupportedAnalyticsPlanError(f"co_occurrence currently requires dimension '{expected_dimension}'.")
    return relationship


def _co_occurrence_qty_field(target_doctype: str) -> str:
    meta = frappe.get_meta(target_doctype)
    return "qty" if meta.get_field("qty") else ""


def _compile_co_occurrence_sql(
    plan: dict[str, Any],
    *,
    relationship: dict[str, Any],
    qty_field: str,
) -> tuple[str, dict[str, Any]]:
    source_table = _quote_identifier(f"tab{plan['source_doctype']}")
    child_table = _quote_identifier(f"tab{relationship['target_doctype']}")
    params: dict[str, Any] = {
        "relationship_parenttype": plan["source_doctype"],
        "relationship_parentfield": relationship["source_field"],
        "limit": plan["limit"],
    }

    filter_sql = _compile_co_occurrence_filters(
        plan.get("filters") or [],
        source_alias="co_source",
        child_alias="co_child",
        target_doctype=relationship["target_doctype"],
        params=params,
    )

    qty_select = ""
    if qty_field:
        qty_select = f", SUM(COALESCE(co_child.{_quote_identifier(qty_field)}, 0)) AS item_qty"

    base_sql = f"""
        SELECT
            co_child.{_quote_identifier('parent')} AS parent,
            co_child.{_quote_identifier('item_code')} AS item_code
            {qty_select}
        FROM {source_table} co_source
        INNER JOIN {child_table} co_child
            ON co_child.{_quote_identifier('parent')} = co_source.{_quote_identifier('name')}
            AND co_child.{_quote_identifier('parenttype')} = %(relationship_parenttype)s
            AND co_child.{_quote_identifier('parentfield')} = %(relationship_parentfield)s
        WHERE co_child.{_quote_identifier('item_code')} IS NOT NULL
            AND co_child.{_quote_identifier('item_code')} <> ''
            {filter_sql}
        GROUP BY
            co_child.{_quote_identifier('parent')},
            co_child.{_quote_identifier('item_code')}
    """.strip()

    if qty_field:
        sql = f"""
            SELECT
                pairs.item_a,
                pairs.item_b,
                COUNT(*) AS pair_count,
                SUM(pairs.qty_a) AS total_qty_a,
                SUM(pairs.qty_b) AS total_qty_b
            FROM (
                SELECT
                    co_a.parent AS parent,
                    co_a.item_code AS item_a,
                    co_b.item_code AS item_b,
                    co_a.item_qty AS qty_a,
                    co_b.item_qty AS qty_b
                FROM ({base_sql}) co_a
                INNER JOIN ({base_sql}) co_b
                    ON co_a.parent = co_b.parent
                    AND co_a.item_code < co_b.item_code
            ) pairs
            GROUP BY pairs.item_a, pairs.item_b
            ORDER BY pair_count DESC, pairs.item_a ASC, pairs.item_b ASC
            LIMIT %(limit)s
        """
    else:
        sql = f"""
            SELECT
                pairs.item_a,
                pairs.item_b,
                COUNT(*) AS pair_count
            FROM (
                SELECT
                    co_a.parent AS parent,
                    co_a.item_code AS item_a,
                    co_b.item_code AS item_b
                FROM ({base_sql}) co_a
                INNER JOIN ({base_sql}) co_b
                    ON co_a.parent = co_b.parent
                    AND co_a.item_code < co_b.item_code
            ) pairs
            GROUP BY pairs.item_a, pairs.item_b
            ORDER BY pair_count DESC, pairs.item_a ASC, pairs.item_b ASC
            LIMIT %(limit)s
        """
    return sql, params


def _compile_co_occurrence_filters(
    filters: list[dict[str, Any]],
    *,
    source_alias: str,
    child_alias: str,
    target_doctype: str,
    params: dict[str, Any],
) -> str:
    clauses: list[str] = []
    for index, current_filter in enumerate(filters):
        field_ref = str(current_filter.get("field") or "").strip()
        term = _compile_co_occurrence_filter_term(
            field_ref,
            source_alias=source_alias,
            child_alias=child_alias,
            target_doctype=target_doctype,
        )
        operator = current_filter["operator"]
        value = current_filter["value"]
        key_prefix = f"co_filter_{index}"
        clauses.append(_compile_filter_sql(term, operator, value, key_prefix=key_prefix, params=params))
    if not clauses:
        return ""
    return " AND " + " AND ".join(clauses)


def _compile_co_occurrence_filter_term(
    field_ref: str,
    *,
    source_alias: str,
    child_alias: str,
    target_doctype: str,
) -> str:
    if "." not in field_ref:
        return f"{source_alias}.{_quote_identifier(field_ref)}"

    doctype_name, fieldname = field_ref.rsplit(".", 1)
    if doctype_name != target_doctype:
        raise UnsupportedAnalyticsPlanError(
            f"co_occurrence filter field '{field_ref}' is outside the approved relationship '{target_doctype}'."
        )
    return f"{child_alias}.{_quote_identifier(fieldname)}"


def _compile_filter_sql(
    term: str,
    operator: str,
    value: Any,
    *,
    key_prefix: str,
    params: dict[str, Any],
) -> str:
    if operator == "=":
        params[key_prefix] = value
        return f"{term} = %({key_prefix})s"
    if operator == ">=":
        params[key_prefix] = value
        return f"{term} >= %({key_prefix})s"
    if operator == "<=":
        params[key_prefix] = value
        return f"{term} <= %({key_prefix})s"
    if operator == "between":
        params[f"{key_prefix}_start"] = value[0]
        params[f"{key_prefix}_end"] = value[1]
        return f"{term} BETWEEN %({key_prefix}_start)s AND %({key_prefix}_end)s"
    if operator == "like_prefix":
        params[key_prefix] = f"{value}%"
        return f"{term} LIKE %({key_prefix})s"
    if operator == "in":
        placeholders: list[str] = []
        for item_index, item in enumerate(value):
            item_key = f"{key_prefix}_{item_index}"
            params[item_key] = item
            placeholders.append(f"%({item_key})s")
        return f"{term} IN ({', '.join(placeholders)})"
    raise UnsupportedAnalyticsPlanError(f"Filter operator '{operator}' is not executable.")


def _quote_identifier(identifier: str) -> str:
    quote = '"' if frappe.db.db_type == "postgres" else "`"
    escaped = str(identifier).replace(quote, quote * 2)
    return f"{quote}{escaped}{quote}"


def _coerce_plan_snapshot(plan: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(plan, dict):
        return dict(plan)
    try:
        return frappe.parse_json(plan) or {}
    except Exception:
        return {}
