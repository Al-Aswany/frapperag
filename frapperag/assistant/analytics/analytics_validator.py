from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import re
import time
from typing import Any

import frappe
from frappe.utils import cint, get_datetime, getdate

from frapperag.assistant.schema_policy import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    classify_field_safety,
    get_allowed_doctype_policy,
    get_analytics_field_policy,
    get_phase4f_analytics_source_doctypes,
    is_phase4f_analytics_source,
)
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
    COMMON_PLAN_KEYS,
    INTENT,
    PLAN_VERSION,
    REJECTED_PLAN_KEYS,
    SUPPORTED_ANALYSIS_TYPES,
    SUPPORTED_FILTER_OPERATORS,
    SUPPORTED_FINAL_ANSWER_SHAPES,
    SUPPORTED_SORT_DIRECTIONS,
    SUPPORTED_SORT_TARGETS,
    SUPPORTED_TIME_GRAINS,
    TOOL_NAME,
    get_plan_shape,
)
from .metric_registry import get_metric_definition, list_metrics
from .relationship_graph import (
    find_relationship,
    get_allowed_relationship_fields,
    get_relationship,
    list_relationships,
)


VALIDATOR_VERSION = "phase4c_foundation_v1"
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
_FIELD_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_SQL_TEXT_RE = re.compile(r"\b(select|insert|update|delete|drop|alter|union)\b", re.IGNORECASE)
_WRITE_WORDS = {"create", "delete", "insert", "set_value", "submit", "update", "write"}
_TARGET_FIELD_KEYS = {"date_field", "field", "target_field"}
_PHASE4F_ANALYTICS_LIMIT_CAPS = {
    "number": 1,
    "ranking": 10,
    "table": 10,
    "comparison": 10,
    "time_series": 12,
}


def validate_plan(
    plan: dict[str, Any] | str,
    *,
    require_validated_flag: bool = False,
    log_result: bool = True,
) -> dict[str, Any]:
    started = time.monotonic()
    raw_plan = _coerce_plan(plan)
    request_id = (raw_plan.get("request_id") or "").strip()
    doctype_name = (raw_plan.get("source_doctype") or "").strip()
    analysis_type = (raw_plan.get("analysis_type") or "").strip()

    try:
        validated = _validate_plan(raw_plan, require_validated_flag=require_validated_flag)
    except Exception as exc:
        _log_validator_event(
            "Rejected",
            request_id=request_id,
            doctype_name=doctype_name,
            analysis_type=analysis_type,
            started=started,
            error_message=str(exc),
            plan=raw_plan,
            details={"require_validated_flag": cint(require_validated_flag)},
            enabled=log_result,
        )
        raise

    _log_validator_event(
        "Success",
        request_id=validated.get("request_id"),
        doctype_name=validated.get("source_doctype"),
        analysis_type=validated.get("analysis_type"),
        started=started,
        plan=validated,
        details={
            "metric_count": len(validated.get("metrics") or []),
            "relationship_count": len(validated.get("relationships") or []),
            "dimension_count": len(validated.get("dimensions") or []),
        },
        enabled=log_result,
    )
    return validated


def validate_analytics_plan(
    plan: dict[str, Any] | str,
    *,
    require_validated_flag: bool = False,
    log_result: bool = True,
) -> dict[str, Any]:
    return validate_plan(
        plan,
        require_validated_flag=require_validated_flag,
        log_result=log_result,
    )


def debug_validate_plan(plan_json: str, require_validated_flag: int = 0) -> dict[str, Any]:
    return validate_plan(plan_json, require_validated_flag=bool(cint(require_validated_flag)))


def debug_validate_analytics_plan(plan_json: str, require_validated_flag: int = 0) -> dict[str, Any]:
    return validate_analytics_plan(plan_json, require_validated_flag=bool(cint(require_validated_flag)))


def debug_describe_analytics_capabilities(source_doctype: str | None = None) -> dict[str, Any]:
    source_doctype = (source_doctype or "").strip()
    phase4f_sources = get_phase4f_analytics_source_doctypes()
    if source_doctype and source_doctype not in phase4f_sources:
        phase4f_sources = []
    elif source_doctype:
        phase4f_sources = [source_doctype]
    return {
        "plan_version": PLAN_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "phase4f_analytics_source_doctypes": phase4f_sources,
        "supported_analysis_types": list(SUPPORTED_ANALYSIS_TYPES),
        "metrics": [
            metric
            for metric in list_metrics(source_doctype=source_doctype or None)
            if metric.get("source_doctype") in phase4f_sources
        ],
        "relationships": [
            {
                **relationship,
                "allowed_dimension_fields": sorted(
                    get_allowed_relationship_fields(relationship.get("relationship_key") or "", purpose="dimension")
                ),
                "allowed_filter_fields": sorted(
                    get_allowed_relationship_fields(relationship.get("relationship_key") or "", purpose="filter")
                ),
                "allowed_co_occurrence_fields": sorted(
                    get_allowed_relationship_fields(relationship.get("relationship_key") or "", purpose="co_occurrence")
                ),
            }
            for relationship in list_relationships(source_doctype=source_doctype or None)
            if relationship.get("source_doctype") in phase4f_sources
        ],
        "field_policies": {
            doctype_name: {
                key: sorted(value) if isinstance(value, frozenset) else value
                for key, value in get_analytics_field_policy(doctype_name).items()
            }
            for doctype_name in phase4f_sources
        },
    }


def _validate_plan(raw_plan: dict[str, Any], *, require_validated_flag: bool) -> dict[str, Any]:
    _reject_write_or_sql_payload(raw_plan)
    _reject_unknown_keys(raw_plan, COMMON_PLAN_KEYS, label="Analytics plan")

    plan_version = (raw_plan.get("plan_version") or "").strip()
    if plan_version != PLAN_VERSION:
        frappe.throw(
            f"Analytics plan version '{plan_version or '<empty>'}' is not supported.",
            frappe.ValidationError,
        )

    if require_validated_flag and not cint(raw_plan.get("validated")):
        frappe.throw("Analytics execution requires a previously validated plan.", frappe.ValidationError)

    if cint(raw_plan.get("needs_clarification")):
        frappe.throw("Plans that need clarification cannot be executed.", frappe.ValidationError)

    analysis_type = (raw_plan.get("analysis_type") or "").strip()
    shape = get_plan_shape(analysis_type)
    if not shape:
        frappe.throw(
            f"Analytics analysis type '{analysis_type or '<empty>'}' is not allowed.",
            frappe.ValidationError,
        )

    _validate_shape_requirements(raw_plan, shape)

    final_answer_shape = (raw_plan.get("final_answer_shape") or "").strip() or _default_answer_shape(analysis_type)
    source_context = _build_doctype_context((raw_plan.get("source_doctype") or "").strip(), allow_child_source=False)
    explicit_relationships = _validate_relationships(raw_plan.get("relationships"), source_context["doctype"])

    metrics, metric_relationships = _validate_metrics(
        raw_plan=raw_plan,
        analysis_type=analysis_type,
        source_context=source_context,
    )
    dimensions, dimension_relationships = _validate_dimensions(
        raw_plan.get("dimensions"),
        source_context,
        analysis_type=analysis_type,
    )
    filters, filter_relationships, has_source_date_filter = _validate_filters(
        raw_plan.get("filters"),
        source_context,
    )
    time_bucket, time_bucket_uses_source_date = _validate_time_bucket(
        raw_plan.get("time_bucket"),
        source_context,
        analysis_type=analysis_type,
    )
    comparison, comparison_uses_source_date = _validate_comparison(
        raw_plan.get("comparison"),
        source_context,
        analysis_type=analysis_type,
    )
    limit, _requested_limit, _policy_limit = _validate_limit(
        raw_plan.get("limit"),
        source_context,
        final_answer_shape=final_answer_shape,
    )
    relationships = _normalize_relationship_union(
        explicit_relationships,
        metric_relationships,
        dimension_relationships,
        filter_relationships,
    )
    sort = _validate_sort(raw_plan.get("sort"), metrics, dimensions)
    numerator_metric = None
    denominator_metric = None
    if analysis_type == ANALYSIS_TYPE_RATIO:
        numerator_metric = _validate_named_metric(raw_plan.get("numerator_metric"), source_context["doctype"], analysis_type)
        denominator_metric = _validate_named_metric(
            raw_plan.get("denominator_metric"),
            source_context["doctype"],
            analysis_type,
        )
        if numerator_metric["metric_name"] == denominator_metric["metric_name"]:
            frappe.throw("Ratio plans require two distinct metrics.", frappe.ValidationError)
        relationships = _normalize_relationship_union(
            relationships,
            [key for key in (numerator_metric.get("relationship_key"), denominator_metric.get("relationship_key")) if key],
        )

    _enforce_analysis_specific_rules(
        analysis_type=analysis_type,
        dimensions=dimensions,
        relationships=relationships,
        metrics=metrics,
        numerator_metric=numerator_metric,
        denominator_metric=denominator_metric,
    )
    _enforce_large_table_date_guard(
        source_context,
        has_source_date_filter=has_source_date_filter,
        time_bucket_uses_source_date=time_bucket_uses_source_date,
        comparison_uses_source_date=comparison_uses_source_date,
    )

    if final_answer_shape not in SUPPORTED_FINAL_ANSWER_SHAPES:
        frappe.throw(
            f"Final answer shape '{final_answer_shape}' is not supported.",
            frappe.ValidationError,
        )

    return {
        "plan_version": PLAN_VERSION,
        "planner_mode": (raw_plan.get("planner_mode") or "").strip() or "manual_scaffold",
        "request_id": (raw_plan.get("request_id") or "").strip(),
        "intent": (raw_plan.get("intent") or INTENT).strip() or INTENT,
        "analysis_type": analysis_type,
        "confidence": _coerce_confidence(raw_plan.get("confidence")),
        "question": (raw_plan.get("question") or "").strip(),
        "source_doctype": source_context["doctype"],
        "relationships": relationships,
        "metrics": [metric["metric_name"] for metric in metrics],
        "dimensions": dimensions,
        "filters": filters,
        "time_bucket": time_bucket,
        "comparison": comparison,
        "numerator_metric": numerator_metric["metric_name"] if numerator_metric else "",
        "denominator_metric": denominator_metric["metric_name"] if denominator_metric else "",
        "sort": sort,
        "limit": limit,
        "final_answer_shape": final_answer_shape,
        "needs_clarification": 0,
        "clarification_question": "",
        "validated": 1,
        "validator_version": VALIDATOR_VERSION,
        "validated_at": str(frappe.utils.now_datetime()),
    }


def _validate_shape_requirements(raw_plan: dict[str, Any], shape: dict[str, Any]) -> None:
    missing = []
    for key in sorted(shape.get("required_keys") or []):
        value = raw_plan.get(key)
        if value in (None, "", [], {}):
            missing.append(key)
    if missing:
        frappe.throw(
            f"Analytics plan is missing required keys for '{raw_plan.get('analysis_type')}'. Missing: {', '.join(missing)}.",
            frappe.ValidationError,
        )

    allowed = set(shape.get("required_keys") or []) | set(shape.get("optional_keys") or [])
    for key, value in raw_plan.items():
        if key not in allowed and value not in (None, "", [], {}):
            frappe.throw(
                f"Key '{key}' is not allowed for analytics analysis type '{raw_plan.get('analysis_type')}'.",
                frappe.ValidationError,
            )


def _build_doctype_context(doctype: str, *, allow_child_source: bool) -> dict[str, Any]:
    doctype = (doctype or "").strip()
    if not doctype:
        frappe.throw("Analytics plan must include source_doctype.", frappe.ValidationError)
    if not is_phase4f_analytics_source(doctype):
        allowed_sources = ", ".join(get_phase4f_analytics_source_doctypes()) or "<none>"
        frappe.throw(
            f"DocType '{doctype}' is not enabled for Phase 4F analytics. Allowed sources: {allowed_sources}.",
            frappe.ValidationError,
        )

    policy = get_allowed_doctype_policy(doctype)
    if not policy or not cint(policy.get("enabled")):
        frappe.throw(f"DocType '{doctype}' is not enabled for analytics.", frappe.ValidationError)
    if not cint(policy.get("allow_get_list")) and not cint(policy.get("allow_query_builder")):
        frappe.throw(
            f"DocType '{doctype}' is not enabled for analytics reads in query policy.",
            frappe.ValidationError,
        )

    meta = frappe.get_meta(doctype)
    if cint(getattr(meta, "issingle", 0)):
        frappe.throw(f"Single DocType '{doctype}' is not supported for analytics.", frappe.ValidationError)
    if cint(getattr(meta, "istable", 0)) and not allow_child_source:
        frappe.throw(f"Child table DocType '{doctype}' is not supported as an analytics source.", frappe.ValidationError)

    safe_fields = dict(_SAFE_STANDARD_FIELDS)
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

    default_date_field = (policy.get("default_date_field") or "").strip()
    if default_date_field and default_date_field not in safe_fields:
        frappe.throw(
            f"DocType '{doctype}' has an unsafe or missing default_date_field '{default_date_field}'.",
            frappe.ValidationError,
        )

    return {
        "doctype": doctype,
        "meta": meta,
        "policy": policy,
        "field_policy": get_analytics_field_policy(doctype),
        "safe_fields": safe_fields,
        "default_date_field": default_date_field,
    }


def _validate_relationships(raw_relationships: Any, source_doctype: str) -> list[str]:
    if raw_relationships in (None, ""):
        return []
    if not isinstance(raw_relationships, list):
        frappe.throw("Analytics relationships must be a list.", frappe.ValidationError)

    validated: list[str] = []
    seen: set[str] = set()
    for raw_relationship in raw_relationships:
        key = str(raw_relationship or "").strip()
        if not key or key in seen:
            continue
        relationship = get_relationship(key)
        if not relationship:
            frappe.throw(f"Relationship '{key}' is not in the approved relationship graph.", frappe.ValidationError)
        if relationship["source_doctype"] != source_doctype:
            frappe.throw(
                f"Relationship '{key}' does not belong to source DocType '{source_doctype}'.",
                frappe.ValidationError,
            )
        _enforce_relationship_policy(relationship, source_doctype)
        validated.append(key)
        seen.add(key)
    if len(validated) > 1:
        frappe.throw(
            "Phase 4F analytics supports at most one approved relationship per plan.",
            frappe.ValidationError,
        )
    return validated


def _validate_metrics(
    *,
    raw_plan: dict[str, Any],
    analysis_type: str,
    source_context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    if analysis_type == ANALYSIS_TYPE_CO_OCCURRENCE:
        raw_metrics = raw_plan.get("metrics") or []
        if raw_metrics not in (None, "", []):
            frappe.throw(
                "co_occurrence does not accept planner-supplied metrics. pair_count is computed by the executor.",
                frappe.ValidationError,
            )
        return [], []

    if analysis_type == ANALYSIS_TYPE_RATIO:
        raw_metrics = raw_plan.get("metrics") or []
        if raw_metrics not in (None, "", []):
            if not isinstance(raw_metrics, list):
                frappe.throw("Metrics must be a list when provided.", frappe.ValidationError)
            for raw_metric in raw_metrics:
                _validate_named_metric(raw_metric, source_context["doctype"], analysis_type)
        return [], []

    raw_metrics = raw_plan.get("metrics")
    if not isinstance(raw_metrics, list) or not raw_metrics:
        frappe.throw("Analytics plan must include a non-empty metrics list.", frappe.ValidationError)

    validated: list[dict[str, Any]] = []
    relationship_keys: list[str] = []
    seen: set[str] = set()
    for raw_metric in raw_metrics:
        metric = _validate_named_metric(raw_metric, source_context["doctype"], analysis_type)
        if metric["metric_name"] in seen:
            continue
        validated.append(metric)
        seen.add(metric["metric_name"])
        if metric.get("relationship_key"):
            relationship_keys.append(metric["relationship_key"])
            _validate_target_metric_field(metric)
        else:
            _validate_source_field_exists(metric["value_field"], source_context)

    return validated, relationship_keys


def _validate_named_metric(metric_name: Any, source_doctype: str, analysis_type: str) -> dict[str, Any]:
    metric_name = str(metric_name or "").strip()
    definition = get_metric_definition(metric_name)
    if not definition:
        frappe.throw(f"Metric '{metric_name or '<empty>'}' is not allowed.", frappe.ValidationError)
    if definition["source_doctype"] != source_doctype:
        frappe.throw(
            f"Metric '{metric_name}' is not valid for source DocType '{source_doctype}'.",
            frappe.ValidationError,
        )
    if analysis_type not in set(definition.get("analysis_types") or []):
        frappe.throw(
            f"Metric '{metric_name}' is not allowed for analytics type '{analysis_type}'.",
            frappe.ValidationError,
        )
    return definition


def _validate_dimensions(
    raw_dimensions: Any,
    source_context: dict[str, Any],
    *,
    analysis_type: str,
) -> tuple[list[str], list[str]]:
    if raw_dimensions in (None, ""):
        return [], []
    if not isinstance(raw_dimensions, list):
        frappe.throw("Analytics dimensions must be a list.", frappe.ValidationError)

    dimensions: list[str] = []
    relationship_keys: list[str] = []
    seen: set[str] = set()
    for raw_dimension in raw_dimensions:
        dimension = str(raw_dimension or "").strip()
        if not dimension or dimension in seen:
            continue
        field_ref = _validate_field_reference(
            dimension,
            source_context,
            allow_relationships=True,
            usage="dimension",
        )
        if analysis_type in {ANALYSIS_TYPE_TOP_N, ANALYSIS_TYPE_BOTTOM_N, ANALYSIS_TYPE_CO_OCCURRENCE} and field_ref.get("fieldtype") in _NUMERIC_FIELDTYPES:
            frappe.throw(
                f"Dimension '{dimension}' is numeric and not suitable as a ranking/grouping dimension.",
                frappe.ValidationError,
            )
        dimensions.append(dimension)
        seen.add(dimension)
        if field_ref.get("relationship_key"):
            relationship_keys.append(field_ref["relationship_key"])
    return dimensions, relationship_keys


def _validate_filters(
    raw_filters: Any,
    source_context: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], bool]:
    if raw_filters in (None, ""):
        return [], [], False
    if not isinstance(raw_filters, list):
        frappe.throw("Analytics filters must be a list.", frappe.ValidationError)

    filters: list[dict[str, Any]] = []
    relationship_keys: list[str] = []
    has_source_date_filter = False
    for raw_filter in raw_filters:
        if not isinstance(raw_filter, dict):
            frappe.throw("Analytics filters must contain objects.", frappe.ValidationError)

        field_ref = _validate_field_reference(
            raw_filter.get("field"),
            source_context,
            allow_relationships=True,
            usage="filter",
        )
        operator = str(raw_filter.get("operator") or "").strip()
        if operator not in SUPPORTED_FILTER_OPERATORS:
            frappe.throw(
                f"Filter operator '{operator or '<empty>'}' is not allowed.",
                frappe.ValidationError,
            )
        value = _validate_filter_value(raw_filter.get("value"), operator, field_ref["fieldtype"], field_ref["field"])
        filters.append({"field": field_ref["normalized"], "operator": operator, "value": value})
        if field_ref.get("relationship_key"):
            relationship_keys.append(field_ref["relationship_key"])
        if (
            field_ref["scope"] == "source"
            and field_ref["field"] == source_context["default_date_field"]
            and operator in {"between", ">=", "<="}
        ):
            has_source_date_filter = True
    return filters, relationship_keys, has_source_date_filter


def _validate_time_bucket(
    raw_time_bucket: Any,
    source_context: dict[str, Any],
    *,
    analysis_type: str,
) -> tuple[dict[str, Any], bool]:
    if analysis_type not in {
        ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
        ANALYSIS_TYPE_TREND,
    }:
        if raw_time_bucket not in (None, "", {}):
            frappe.throw(
                f"time_bucket is not allowed for analysis type '{analysis_type}'.",
                frappe.ValidationError,
            )
        return {}, False

    if not isinstance(raw_time_bucket, dict):
        frappe.throw("time_bucket must be an object.", frappe.ValidationError)

    field_ref = _validate_field_reference(
        raw_time_bucket.get("date_field"),
        source_context,
        allow_relationships=False,
        usage="date",
    )
    grain = str(raw_time_bucket.get("grain") or "").strip().lower()
    if grain not in SUPPORTED_TIME_GRAINS:
        frappe.throw(f"time_bucket grain '{grain or '<empty>'}' is not supported.", frappe.ValidationError)
    if field_ref["fieldtype"] not in _DATE_FIELDTYPES:
        frappe.throw("time_bucket date_field must be a Date or Datetime field.", frappe.ValidationError)

    return {
        "date_field": field_ref["normalized"],
        "grain": grain,
    }, field_ref["scope"] == "source" and field_ref["field"] == source_context["default_date_field"]


def _validate_comparison(
    raw_comparison: Any,
    source_context: dict[str, Any],
    *,
    analysis_type: str,
) -> tuple[dict[str, Any], bool]:
    if analysis_type != ANALYSIS_TYPE_PERIOD_COMPARISON:
        if raw_comparison not in (None, "", {}):
            frappe.throw(
                f"comparison is not allowed for analysis type '{analysis_type}'.",
                frappe.ValidationError,
            )
        return {}, False

    if not isinstance(raw_comparison, dict):
        frappe.throw("comparison must be an object.", frappe.ValidationError)

    field_ref = _validate_field_reference(
        raw_comparison.get("date_field"),
        source_context,
        allow_relationships=False,
        usage="date",
    )
    if field_ref["fieldtype"] not in _DATE_FIELDTYPES:
        frappe.throw("comparison date_field must be a Date or Datetime field.", frappe.ValidationError)

    current = _validate_date_range(raw_comparison.get("current"), label="comparison.current")
    previous = _validate_date_range(raw_comparison.get("previous"), label="comparison.previous")
    return {
        "date_field": field_ref["normalized"],
        "current": current,
        "previous": previous,
    }, field_ref["scope"] == "source" and field_ref["field"] == source_context["default_date_field"]


def _validate_sort(raw_sort: Any, metrics: list[dict[str, Any]], dimensions: list[str]) -> list[dict[str, Any]]:
    if raw_sort in (None, ""):
        return []
    if isinstance(raw_sort, dict):
        raw_sort = [raw_sort]
    if not isinstance(raw_sort, list):
        frappe.throw("sort must be a list or object.", frappe.ValidationError)

    metric_names = {metric["metric_name"] for metric in metrics}
    dimension_names = set(dimensions)
    validated: list[dict[str, Any]] = []
    for entry in raw_sort:
        if not isinstance(entry, dict):
            frappe.throw("sort entries must be objects.", frappe.ValidationError)
        by = str(entry.get("by") or "").strip()
        name = str(entry.get("name") or "").strip()
        direction = str(entry.get("direction") or "asc").strip().lower() or "asc"
        if by not in SUPPORTED_SORT_TARGETS:
            frappe.throw(f"sort target '{by or '<empty>'}' is not supported.", frappe.ValidationError)
        if direction not in SUPPORTED_SORT_DIRECTIONS:
            frappe.throw(f"sort direction '{direction or '<empty>'}' is not supported.", frappe.ValidationError)
        if by == "metric" and name not in metric_names:
            frappe.throw(f"sort metric '{name or '<empty>'}' is not part of the validated metrics list.", frappe.ValidationError)
        if by == "dimension" and name not in dimension_names:
            frappe.throw(f"sort dimension '{name or '<empty>'}' is not part of the validated dimensions list.", frappe.ValidationError)
        validated.append({"by": by, "name": name, "direction": direction})
    return validated


def _validate_limit(
    raw_limit: Any,
    source_context: dict[str, Any],
    *,
    final_answer_shape: str,
) -> tuple[int, int, int]:
    policy_limit = cint((source_context.get("policy") or {}).get("default_limit") or DEFAULT_LIMIT)
    if policy_limit < 1:
        policy_limit = DEFAULT_LIMIT
    policy_limit = min(policy_limit, MAX_LIMIT)

    if raw_limit in (None, ""):
        requested_limit = policy_limit
    else:
        try:
            requested_limit = cint(raw_limit)
        except Exception:
            frappe.throw("Analytics limit must be numeric.", frappe.ValidationError)
        if requested_limit < 1:
            frappe.throw("Analytics limit must be at least 1.", frappe.ValidationError)

    effective_limit = min(
        requested_limit,
        policy_limit,
        _limit_cap_for_shape(final_answer_shape),
        MAX_LIMIT,
    )
    effective_limit = max(1, effective_limit)
    return effective_limit, requested_limit, policy_limit


def _validate_field_reference(
    value: Any,
    source_context: dict[str, Any],
    *,
    allow_relationships: bool,
    usage: str,
) -> dict[str, Any]:
    raw_ref = str(value or "").strip()
    if not raw_ref:
        frappe.throw("Analytics field references cannot be empty.", frappe.ValidationError)

    if "." not in raw_ref:
        fieldname = raw_ref
        _ensure_safe_field_name(fieldname, label=raw_ref)
        field_meta = source_context["safe_fields"].get(fieldname)
        if not field_meta:
            frappe.throw(
                f"Field '{fieldname}' is not allowed for DocType '{source_context['doctype']}'.",
                frappe.ValidationError,
            )
        _enforce_source_field_policy(source_context, fieldname, usage=usage)
        return {
            "scope": "source",
            "doctype": source_context["doctype"],
            "field": fieldname,
            "fieldtype": field_meta["fieldtype"],
            "relationship_key": "",
            "normalized": fieldname,
        }

    if not allow_relationships:
        frappe.throw(
            f"Field reference '{raw_ref}' cannot traverse relationships in this context.",
            frappe.ValidationError,
        )

    target_doctype, fieldname = raw_ref.rsplit(".", 1)
    target_doctype = target_doctype.strip()
    fieldname = fieldname.strip()
    relationship = find_relationship(source_context["doctype"], target_doctype)
    if not relationship:
        frappe.throw(
            f"Relationship '{source_context['doctype']} -> {target_doctype}' is not approved.",
            frappe.ValidationError,
        )
    _enforce_relationship_policy(relationship, source_context["doctype"])
    _ensure_safe_field_name(fieldname, label=raw_ref)

    target_context = _build_related_target_context(relationship)
    field_meta = target_context["safe_fields"].get(fieldname)
    if not field_meta:
        frappe.throw(
            f"Field '{fieldname}' is not allowed for related DocType '{target_doctype}'.",
            frappe.ValidationError,
        )
    _enforce_relationship_field_policy(relationship["relationship_key"], fieldname, usage=usage, target_doctype=target_doctype)

    return {
        "scope": "relationship",
        "doctype": target_doctype,
        "field": fieldname,
        "fieldtype": field_meta["fieldtype"],
        "relationship_key": relationship["relationship_key"],
        "normalized": f"{target_doctype}.{fieldname}",
    }


def _build_related_target_context(relationship: dict[str, Any]) -> dict[str, Any]:
    target_doctype = relationship["target_doctype"]
    if target_doctype == "Party":
        frappe.throw(
            "Dynamic Party traversal is registered for future analytics only and is not field-queryable yet.",
            frappe.ValidationError,
        )

    target_policy = get_allowed_doctype_policy(target_doctype)
    if target_policy is not None and not cint(target_policy.get("enabled")):
        frappe.throw(
            f"Related DocType '{target_doctype}' is explicitly disabled in query policy.",
            frappe.ValidationError,
        )

    return _build_meta_context(target_doctype, allow_child_table=relationship["relationship_type"] == "child_table")


def _enforce_source_field_policy(source_context: dict[str, Any], fieldname: str, *, usage: str) -> None:
    field_policy = source_context.get("field_policy") or {}
    if usage == "dimension":
        allowed = set(field_policy.get("source_dimensions") or [])
        if fieldname not in allowed:
            frappe.throw(
                f"Field '{fieldname}' is not approved as an analytics dimension for DocType '{source_context['doctype']}'.",
                frappe.ValidationError,
            )
        return

    if usage == "filter":
        allowed = set(field_policy.get("source_filters") or [])
        if fieldname not in allowed:
            frappe.throw(
                f"Field '{fieldname}' is not approved as an analytics filter for DocType '{source_context['doctype']}'.",
                frappe.ValidationError,
            )
        return

    if usage == "date":
        default_date_field = str(field_policy.get("default_date_field") or "").strip()
        if fieldname != default_date_field:
            frappe.throw(
                f"Field '{fieldname}' is not approved as the analytics date field for DocType '{source_context['doctype']}'.",
                frappe.ValidationError,
            )
        return

    frappe.throw(f"Unknown analytics field usage '{usage}'.", frappe.ValidationError)


def _enforce_relationship_field_policy(
    relationship_key: str,
    fieldname: str,
    *,
    usage: str,
    target_doctype: str,
) -> None:
    purpose = "dimension" if usage == "dimension" else "filter"
    allowed = set(get_allowed_relationship_fields(relationship_key, purpose=purpose))
    if fieldname not in allowed:
        frappe.throw(
            f"Field '{fieldname}' is not approved for relationship '{relationship_key}' on related DocType '{target_doctype}'.",
            frappe.ValidationError,
        )


def _build_meta_context(doctype: str, *, allow_child_table: bool) -> dict[str, Any]:
    meta = frappe.get_meta(doctype)
    if cint(getattr(meta, "issingle", 0)):
        frappe.throw(f"Single DocType '{doctype}' cannot be used as a related analytics target.", frappe.ValidationError)
    if cint(getattr(meta, "istable", 0)) and not allow_child_table:
        frappe.throw(f"Child table '{doctype}' is not allowed as a related analytics target.", frappe.ValidationError)

    safe_fields = dict(_SAFE_STANDARD_FIELDS)
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
    return {"doctype": doctype, "meta": meta, "safe_fields": safe_fields}


def _enforce_relationship_policy(relationship: dict[str, Any], source_doctype: str) -> None:
    policy = get_allowed_doctype_policy(source_doctype) or {}
    if not cint(policy.get("allow_query_builder")):
        frappe.throw(
            f"DocType '{source_doctype}' does not allow relationship analytics in query policy.",
            frappe.ValidationError,
        )
    if relationship["relationship_type"] == "child_table" and not cint(policy.get("allow_child_tables")):
        frappe.throw(
            f"DocType '{source_doctype}' does not allow child-table traversal for analytics.",
            frappe.ValidationError,
        )


def _validate_target_metric_field(metric: dict[str, Any]) -> None:
    relationship_key = metric.get("relationship_key")
    relationship = get_relationship(relationship_key or "")
    if not relationship:
        frappe.throw(
            f"Metric '{metric['metric_name']}' depends on unsupported relationship '{relationship_key}'.",
            frappe.ValidationError,
        )
    target_context = _build_related_target_context(relationship)
    field_meta = target_context["safe_fields"].get(metric["value_field"])
    if not field_meta:
        frappe.throw(
            f"Metric '{metric['metric_name']}' uses unsupported field '{metric['value_field']}' on '{relationship['target_doctype']}'.",
            frappe.ValidationError,
        )


def _validate_source_field_exists(fieldname: str, source_context: dict[str, Any]) -> None:
    field_meta = source_context["safe_fields"].get(fieldname)
    if not field_meta:
        frappe.throw(
            f"Field '{fieldname}' is not allowed for DocType '{source_context['doctype']}'.",
            frappe.ValidationError,
        )


def _validate_filter_value(raw_value: Any, operator: str, fieldtype: str, fieldname: str) -> Any:
    if operator == "in":
        if not isinstance(raw_value, list) or not raw_value:
            frappe.throw(f"Filter '{fieldname}' with operator 'in' needs a non-empty list.", frappe.ValidationError)
        if len(raw_value) > 50:
            frappe.throw(f"Filter '{fieldname}' exceeds the 50-value cap for 'in'.", frappe.ValidationError)
        return [_coerce_scalar(value, fieldtype, fieldname=fieldname) for value in raw_value]

    if operator == "between":
        if not isinstance(raw_value, list) or len(raw_value) != 2:
            frappe.throw(f"Filter '{fieldname}' with operator 'between' needs a two-item list.", frappe.ValidationError)
        if fieldtype not in _DATE_FIELDTYPES:
            frappe.throw(
                f"Filter '{fieldname}' only supports 'between' on Date or Datetime fields.",
                frappe.ValidationError,
            )
        return [
            _coerce_scalar(raw_value[0], fieldtype, fieldname=fieldname),
            _coerce_scalar(raw_value[1], fieldtype, fieldname=fieldname),
        ]

    if operator in {">=", "<="}:
        if fieldtype not in (_DATE_FIELDTYPES | _NUMERIC_FIELDTYPES):
            frappe.throw(
                f"Filter '{fieldname}' only supports '{operator}' on date or numeric fields.",
                frappe.ValidationError,
            )
        return _coerce_scalar(raw_value, fieldtype, fieldname=fieldname)

    if operator == "like_prefix":
        if fieldtype not in _STRING_FIELDTYPES:
            frappe.throw(
                f"Filter '{fieldname}' only supports 'like_prefix' on string-like fields.",
                frappe.ValidationError,
            )
        value = str(raw_value or "").strip()
        if not value:
            frappe.throw(f"Filter '{fieldname}' with operator 'like_prefix' needs a value.", frappe.ValidationError)
        if "%" in value or "_" in value:
            frappe.throw(
                f"Filter '{fieldname}' cannot include SQL wildcard characters.",
                frappe.ValidationError,
            )
        return value

    return _coerce_scalar(raw_value, fieldtype, fieldname=fieldname)


def _coerce_scalar(value: Any, fieldtype: str, *, fieldname: str) -> Any:
    if value in (None, ""):
        frappe.throw(f"Filter '{fieldname}' requires a non-empty value.", frappe.ValidationError)

    if fieldtype in _DATE_FIELDTYPES:
        try:
            if fieldtype == "Datetime":
                return str(get_datetime(value))
            return str(getdate(value))
        except Exception:
            frappe.throw(f"Filter '{fieldname}' requires a valid {fieldtype} value.", frappe.ValidationError)

    if fieldtype in _NUMERIC_FIELDTYPES:
        try:
            return float(Decimal(str(value)))
        except (InvalidOperation, TypeError, ValueError):
            frappe.throw(f"Filter '{fieldname}' requires a numeric value.", frappe.ValidationError)

    if fieldtype in _BOOLEAN_FIELDTYPES:
        if value not in (0, 1, True, False, "0", "1", "true", "false", "True", "False"):
            frappe.throw(f"Filter '{fieldname}' only accepts 0/1 or true/false.", frappe.ValidationError)
        return 1 if value in (1, True, "1", "true", "True") else 0

    return str(value).strip()


def _validate_date_range(value: Any, *, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) != 2:
        frappe.throw(f"{label} must be a two-item date range.", frappe.ValidationError)
    return [str(getdate(value[0])), str(getdate(value[1]))]


def _enforce_analysis_specific_rules(
    *,
    analysis_type: str,
    dimensions: list[str],
    relationships: list[str],
    metrics: list[dict[str, Any]],
    numerator_metric: dict[str, Any] | None,
    denominator_metric: dict[str, Any] | None,
) -> None:
    if analysis_type == ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE:
        if not relationships:
            frappe.throw("parent_child_aggregate requires at least one approved relationship.", frappe.ValidationError)
        for relationship_key in relationships:
            relationship = get_relationship(relationship_key)
            if relationship and relationship["relationship_type"] != "child_table":
                frappe.throw(
                    "parent_child_aggregate only supports approved parent-child relationships.",
                    frappe.ValidationError,
                )

    if analysis_type == ANALYSIS_TYPE_CO_OCCURRENCE:
        if len(dimensions) != 1:
            frappe.throw(
                "co_occurrence requires exactly one entity dimension on the approved child table.",
                frappe.ValidationError,
            )
        if len(relationships) != 1:
            frappe.throw("co_occurrence requires exactly one approved relationship.", frappe.ValidationError)
        relationship = get_relationship(relationships[0])
        if not relationship or relationship["relationship_type"] != "child_table":
            frappe.throw(
                "co_occurrence only supports approved parent-child relationships.",
                frappe.ValidationError,
            )
        expected_dimension = f"{relationship['target_doctype']}.item_code"
        if dimensions[0] != expected_dimension:
            frappe.throw(
                f"co_occurrence currently requires dimension '{expected_dimension}'.",
                frappe.ValidationError,
            )

    if analysis_type in {ANALYSIS_TYPE_TOP_N, ANALYSIS_TYPE_BOTTOM_N} and not dimensions:
        frappe.throw(f"{analysis_type} requires at least one dimension.", frappe.ValidationError)

    if analysis_type in {ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE, ANALYSIS_TYPE_TREND} and not metrics:
        frappe.throw(f"{analysis_type} requires at least one metric.", frappe.ValidationError)

    if analysis_type == ANALYSIS_TYPE_TREND and len(metrics) != 1:
        frappe.throw("trend requires exactly one metric.", frappe.ValidationError)

    if analysis_type == ANALYSIS_TYPE_RATIO and (not numerator_metric or not denominator_metric):
        frappe.throw("ratio requires numerator_metric and denominator_metric.", frappe.ValidationError)


def _enforce_large_table_date_guard(
    source_context: dict[str, Any],
    *,
    has_source_date_filter: bool,
    time_bucket_uses_source_date: bool,
    comparison_uses_source_date: bool,
) -> None:
    policy = source_context["policy"] or {}
    if not cint(policy.get("large_table_requires_date_filter")):
        return

    default_date_field = source_context["default_date_field"]
    if not default_date_field:
        frappe.throw(
            f"DocType '{source_context['doctype']}' requires a date filter but has no safe default_date_field.",
            frappe.ValidationError,
        )

    if has_source_date_filter or time_bucket_uses_source_date or comparison_uses_source_date:
        return

    frappe.throw(
        f"DocType '{source_context['doctype']}' requires a filter on '{default_date_field}' for analytics plans.",
        frappe.ValidationError,
    )


def _reject_write_or_sql_payload(value: Any, *, path: str = "plan") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key or "").strip().lower()
            if lowered in REJECTED_PLAN_KEYS:
                if lowered in {"action", "command", "delete", "insert", "mutation", "operation", "update", "write_operation"}:
                    frappe.throw(
                        f"Analytics plan key '{key}' is not allowed because analytics plans are read-only.",
                        frappe.ValidationError,
                    )
                frappe.throw(
                    f"Analytics plan key '{key}' is not allowed because SQL strings are forbidden.",
                    frappe.ValidationError,
                )
            if lowered in _TARGET_FIELD_KEYS:
                _ensure_safe_field_reference_text(item, label=f"{path}.{key}")
            elif lowered == "direction":
                if str(item or "").strip().lower() not in SUPPORTED_SORT_DIRECTIONS:
                    frappe.throw(f"Sort direction '{item}' is not supported.", frappe.ValidationError)
            elif lowered == "by":
                if str(item or "").strip() not in SUPPORTED_SORT_TARGETS:
                    frappe.throw(f"Sort target '{item}' is not supported.", frappe.ValidationError)
            else:
                _reject_write_or_sql_payload(item, path=f"{path}.{key}")
        return

    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_write_or_sql_payload(item, path=f"{path}[{index}]")
        return

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        if _SQL_TEXT_RE.search(text) and ("\n" in text or " from " in text.lower() or " join " in text.lower()):
            frappe.throw(
                f"Analytics plan contains forbidden SQL text at {path}.",
                frappe.ValidationError,
            )
        if text.lower() in _WRITE_WORDS:
            frappe.throw(
                f"Analytics plan contains write operation text '{text}' at {path}.",
                frappe.ValidationError,
            )


def _ensure_safe_field_name(value: Any, *, label: str) -> None:
    text = str(value or "").strip()
    if not text:
        frappe.throw(f"{label} cannot be empty.", frappe.ValidationError)
    if not _FIELD_REF_RE.fullmatch(text):
        frappe.throw(f"{label} must use a safe fieldname only.", frappe.ValidationError)


def _ensure_safe_field_reference_text(value: Any, *, label: str) -> None:
    text = str(value or "").strip()
    if not text:
        frappe.throw(f"{label} cannot be empty.", frappe.ValidationError)
    if "." not in text:
        _ensure_safe_field_name(text, label=label)
        return

    target_doctype, fieldname = text.rsplit(".", 1)
    if not target_doctype.strip() or not fieldname.strip():
        frappe.throw(f"{label} must use 'DocType.fieldname' for related field references.", frappe.ValidationError)
    _ensure_safe_field_name(fieldname.strip(), label=label)


def _normalize_relationship_union(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for group in groups:
        for key in group or []:
            value = str(key or "").strip()
            if not value or value in seen:
                continue
            relationship = get_relationship(value)
            if not relationship:
                frappe.throw(f"Relationship '{value}' is not allowed.", frappe.ValidationError)
            seen.add(value)
            normalized.append(value)
    if len(normalized) > 1:
        frappe.throw(
            "Phase 4F analytics does not support multi-hop or multi-relationship plans.",
            frappe.ValidationError,
        )
    return normalized


def _limit_cap_for_shape(final_answer_shape: str) -> int:
    return _PHASE4F_ANALYTICS_LIMIT_CAPS.get((final_answer_shape or "table").strip() or "table", 10)


def _default_answer_shape(analysis_type: str) -> str:
    if analysis_type in {ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE, ANALYSIS_TYPE_TREND}:
        return "time_series"
    if analysis_type == ANALYSIS_TYPE_PERIOD_COMPARISON:
        return "comparison"
    if analysis_type in {ANALYSIS_TYPE_TOP_N, ANALYSIS_TYPE_BOTTOM_N, ANALYSIS_TYPE_CO_OCCURRENCE}:
        return "ranking"
    if analysis_type == ANALYSIS_TYPE_RATIO:
        return "number"
    return "table"


def _reject_unknown_keys(payload: dict[str, Any], allowed_keys: set[str], *, label: str) -> None:
    unknown = sorted(set(payload.keys()) - set(allowed_keys))
    if unknown:
        frappe.throw(
            f"{label} contains unsupported keys: {', '.join(unknown)}.",
            frappe.ValidationError,
        )


def _coerce_confidence(raw_value: Any) -> float:
    if raw_value in (None, ""):
        return 1.0
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        frappe.throw("Analytics confidence must be a number between 0 and 1.", frappe.ValidationError)
    if value < 0 or value > 1:
        frappe.throw("Analytics confidence must be between 0 and 1.", frappe.ValidationError)
    return round(value, 4)


def _coerce_plan(plan: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(plan, dict):
        return dict(plan)
    if isinstance(plan, str):
        text = plan.strip()
        if not text:
            frappe.throw("Analytics plan JSON cannot be empty.", frappe.ValidationError)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            frappe.throw(f"Analytics plan JSON is invalid: {exc}", frappe.ValidationError)
        if not isinstance(parsed, dict):
            frappe.throw("Analytics plan JSON must decode to an object.", frappe.ValidationError)
        return parsed
    frappe.throw("Analytics plan must be a dict or JSON string.", frappe.ValidationError)


def _duration_ms(started: float) -> int:
    return max(0, int(round((time.monotonic() - started) * 1000)))


def _log_validator_event(
    status: str,
    *,
    request_id: str,
    doctype_name: str,
    analysis_type: str,
    started: float,
    error_message: str | None = None,
    plan: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    enabled: bool,
) -> None:
    if not enabled:
        return
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    if status == "Rejected":
        logger.info(
            "[ANALYTICS_PLAN_REJECTED] request_id=%s doctype=%s analysis_type=%s reason=%s",
            request_id,
            doctype_name,
            analysis_type,
            error_message or "",
        )
    log_tool_call(
        "analytics.validator.validate_plan",
        status,
        tool_name=TOOL_NAME,
        doctype_name=doctype_name,
        request_id=request_id,
        intent=((plan or {}).get("intent") or INTENT),
        duration_ms=_duration_ms(started),
        error_message=error_message,
        plan=plan,
        details={
            "validator_version": VALIDATOR_VERSION,
            **build_analytics_log_details(
                hybrid_branch="analytics",
                analysis_type=analysis_type or "",
                source_doctype=doctype_name or "",
                planner_mode=((plan or {}).get("planner_mode") or ""),
                requested_limit=_requested_limit_for_log(plan),
                effective_limit=_effective_limit_for_log(plan),
                policy_limit=_policy_limit_for_log(doctype_name),
                date_filter_required=_date_filter_required_for_log(doctype_name),
                date_filter_present=_date_filter_present_for_log(plan, doctype_name),
                metrics=_metric_names_for_log(plan),
                dimensions=((plan or {}).get("dimensions") or []),
                relationships=((plan or {}).get("relationships") or []),
                result_status=status.lower(),
                error_code=_classify_validator_error(error_message or "", plan)[0],
                error_class=_classify_validator_error(error_message or "", plan)[1],
            ),
            "route_confidence": 0.0,
            "candidate_doctypes": [],
            "final_answer_shape": ((plan or {}).get("final_answer_shape") or ""),
            **(details or {}),
        },
    )


def _requested_limit_for_log(plan: dict[str, Any] | None) -> int:
    return cint(((plan or {}).get("limit") or 0))


def _effective_limit_for_log(plan: dict[str, Any] | None) -> int:
    if not plan:
        return 0
    doctype_name = str(plan.get("source_doctype") or "").strip()
    return _clamp_limit_for_log(
        requested_limit=cint(plan.get("limit") or 0),
        doctype_name=doctype_name,
        final_answer_shape=str(plan.get("final_answer_shape") or "table").strip() or "table",
    )


def _policy_limit_for_log(doctype_name: str | None) -> int:
    policy = get_allowed_doctype_policy(doctype_name or "") or {}
    limit = cint(policy.get("default_limit") or DEFAULT_LIMIT)
    return min(MAX_LIMIT, max(1, limit))


def _date_filter_required_for_log(doctype_name: str | None) -> int:
    policy = get_allowed_doctype_policy(doctype_name or "") or {}
    return cint(policy.get("large_table_requires_date_filter") or 0)


def _date_filter_present_for_log(plan: dict[str, Any] | None, doctype_name: str | None) -> int:
    if not plan:
        return 0
    default_date_field = str((get_allowed_doctype_policy(doctype_name or "") or {}).get("default_date_field") or "").strip()
    if not default_date_field:
        return 0
    filters = plan.get("filters") or []
    for row in filters:
        if not isinstance(row, dict):
            continue
        if str(row.get("field") or "").strip() == default_date_field and str(row.get("operator") or "").strip() in {"between", ">=", "<="}:
            return 1
    time_bucket = plan.get("time_bucket") or {}
    if str(time_bucket.get("date_field") or "").strip() == default_date_field:
        return 1
    comparison = plan.get("comparison") or {}
    return cint(str(comparison.get("date_field") or "").strip() == default_date_field)


def _metric_names_for_log(plan: dict[str, Any] | None) -> list[str]:
    names = [
        str(metric or "").strip()
        for metric in ((plan or {}).get("metrics") or [])
        if str(metric or "").strip()
    ]
    for key in ("numerator_metric", "denominator_metric"):
        value = str((plan or {}).get(key) or "").strip()
        if value and value not in names:
            names.append(value)
    return names


def _clamp_limit_for_log(*, requested_limit: int, doctype_name: str, final_answer_shape: str) -> int:
    if requested_limit < 1:
        requested_limit = _policy_limit_for_log(doctype_name)
    return min(
        max(1, requested_limit),
        _policy_limit_for_log(doctype_name),
        _limit_cap_for_shape(final_answer_shape),
        MAX_LIMIT,
    )


def _classify_validator_error(error_message: str, plan: dict[str, Any] | None) -> tuple[str, str]:
    del plan
    text = (error_message or "").lower()
    if not text:
        return "", ""
    if "metric '" in text:
        return "metric_rejected", "metric"
    if "phase 4f analytics" in text or "source_doctype" in text:
        return "source_doctype_rejected", "doctype"
    if "query policy" in text or "child-table traversal" in text:
        return "policy_rejected", "policy"
    if "relationship" in text and "multi-hop" in text:
        return "multi_hop_not_supported", "relationship"
    if "relationship" in text:
        return "relationship_rejected", "relationship"
    if "analytics dimension" in text or "analytics filter" in text or "analytics date field" in text:
        return "field_rejected", "field"
    if "requires a filter on" in text:
        return "date_filter_required", "filter"
    if "limit" in text:
        return "limit_rejected", "limit"
    return "validation_rejected", "validation"
