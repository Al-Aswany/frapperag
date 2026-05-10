from __future__ import annotations

from copy import deepcopy
from typing import Any


PLAN_VERSION = "phase4c_foundation_v1"
PLANNER_MODE = "analytics_dsl_v1"
INTENT = "analytics_query"
TOOL_NAME = "analytics_plan"

ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE = "single_doctype_aggregate"
ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE = "parent_child_aggregate"
ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE = "time_bucket_aggregate"
ANALYSIS_TYPE_PERIOD_COMPARISON = "period_comparison"
ANALYSIS_TYPE_CO_OCCURRENCE = "co_occurrence"
ANALYSIS_TYPE_TOP_N = "top_n"
ANALYSIS_TYPE_BOTTOM_N = "bottom_n"
ANALYSIS_TYPE_RATIO = "ratio"
ANALYSIS_TYPE_TREND = "trend"

SUPPORTED_ANALYSIS_TYPES = (
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

SUPPORTED_FILTER_OPERATORS = ("=", "in", "between", ">=", "<=", "like_prefix")
SUPPORTED_TIME_GRAINS = ("day", "week", "month", "quarter", "year")
SUPPORTED_SORT_TARGETS = ("metric", "dimension")
SUPPORTED_SORT_DIRECTIONS = ("asc", "desc")
SUPPORTED_FINAL_ANSWER_SHAPES = ("table", "number", "comparison", "ranking", "time_series")

COMMON_PLAN_KEYS = {
    "analysis_type",
    "clarification_question",
    "comparison",
    "confidence",
    "dimensions",
    "filters",
    "final_answer_shape",
    "intent",
    "limit",
    "metrics",
    "needs_clarification",
    "numerator_metric",
    "denominator_metric",
    "plan_version",
    "planner_mode",
    "question",
    "relationships",
    "request_id",
    "sort",
    "source_doctype",
    "time_bucket",
    "validated",
    "validated_at",
    "validator_version",
}

REJECTED_PLAN_KEYS = {
    "action",
    "command",
    "delete",
    "insert",
    "mutation",
    "operation",
    "query",
    "raw_sql",
    "sql",
    "sql_query",
    "statement",
    "update",
    "write_operation",
}

_COMMON_OPTIONAL_KEYS = {
    "clarification_question",
    "confidence",
    "dimensions",
    "filters",
    "final_answer_shape",
    "intent",
    "limit",
    "needs_clarification",
    "planner_mode",
    "question",
    "relationships",
    "request_id",
    "sort",
    "validated",
    "validated_at",
    "validator_version",
}

ANALYTICS_PLAN_SHAPES: dict[str, dict[str, Any]] = {
    ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE: {
        "description": "Aggregate one or more safe metrics on a single allowed DocType.",
        "required_keys": {"analysis_type", "metrics", "plan_version", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
            "source_doctype": "Sales Invoice",
            "metrics": ["sales_amount"],
            "dimensions": ["customer"],
            "filters": [{"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-01-31"]}],
            "limit": 10,
            "sort": [{"by": "metric", "name": "sales_amount", "direction": "desc"}],
        },
    },
    ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE: {
        "description": "Aggregate safe child-table metrics through an approved parent-child relationship.",
        "required_keys": {"analysis_type", "metrics", "plan_version", "relationships", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
            "source_doctype": "Sales Invoice",
            "relationships": ["sales_invoice_items"],
            "metrics": ["sales_qty"],
            "dimensions": ["Sales Invoice Item.item_code"],
            "filters": [{"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-01-31"]}],
            "limit": 10,
        },
    },
    ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE: {
        "description": "Aggregate safe metrics grouped by a validated date bucket.",
        "required_keys": {"analysis_type", "metrics", "plan_version", "source_doctype", "time_bucket"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
            "source_doctype": "Sales Invoice",
            "metrics": ["sales_amount"],
            "time_bucket": {"date_field": "posting_date", "grain": "month"},
        },
    },
    ANALYSIS_TYPE_PERIOD_COMPARISON: {
        "description": "Compare safe metrics across two explicit time periods.",
        "required_keys": {"analysis_type", "comparison", "metrics", "plan_version", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_PERIOD_COMPARISON,
            "source_doctype": "Sales Invoice",
            "metrics": ["sales_amount"],
            "comparison": {
                "date_field": "posting_date",
                "current": ["2026-05-01", "2026-05-31"],
                "previous": ["2026-04-01", "2026-04-30"],
            },
        },
    },
    ANALYSIS_TYPE_CO_OCCURRENCE: {
        "description": "Find recurring item pairs through one validated parent-child relationship.",
        "required_keys": {"analysis_type", "dimensions", "plan_version", "relationships", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_CO_OCCURRENCE,
            "source_doctype": "Sales Invoice",
            "relationships": ["sales_invoice_items"],
            "dimensions": ["Sales Invoice Item.item_code"],
            "filters": [{"field": "posting_date", "operator": "between", "value": ["2026-01-01", "2026-12-31"]}],
            "limit": 10,
        },
    },
    ANALYSIS_TYPE_TOP_N: {
        "description": "Rank the highest groups by a safe metric.",
        "required_keys": {"analysis_type", "dimensions", "metrics", "plan_version", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_TOP_N,
            "source_doctype": "Sales Invoice",
            "metrics": ["invoice_count"],
            "dimensions": ["customer"],
            "limit": 10,
            "sort": [{"by": "metric", "name": "invoice_count", "direction": "desc"}],
        },
    },
    ANALYSIS_TYPE_BOTTOM_N: {
        "description": "Rank the lowest groups by a safe metric.",
        "required_keys": {"analysis_type", "dimensions", "metrics", "plan_version", "source_doctype"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_BOTTOM_N,
            "source_doctype": "Bin",
            "metrics": ["stock_qty"],
            "dimensions": ["warehouse"],
            "limit": 10,
            "sort": [{"by": "metric", "name": "stock_qty", "direction": "asc"}],
        },
    },
    ANALYSIS_TYPE_RATIO: {
        "description": "Compute a ratio from two safe metrics on the same source doctype.",
        "required_keys": {
            "analysis_type",
            "denominator_metric",
            "numerator_metric",
            "plan_version",
            "source_doctype",
        },
        "optional_keys": _COMMON_OPTIONAL_KEYS | {"metrics"},
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_RATIO,
            "source_doctype": "Sales Invoice",
            "numerator_metric": "outstanding_amount",
            "denominator_metric": "sales_amount",
        },
    },
    ANALYSIS_TYPE_TREND: {
        "description": "Plot one safe metric across a validated time bucket.",
        "required_keys": {"analysis_type", "metrics", "plan_version", "source_doctype", "time_bucket"},
        "optional_keys": _COMMON_OPTIONAL_KEYS,
        "example": {
            "plan_version": PLAN_VERSION,
            "analysis_type": ANALYSIS_TYPE_TREND,
            "source_doctype": "Stock Ledger Entry",
            "metrics": ["movement_qty"],
            "time_bucket": {"date_field": "posting_date", "grain": "month"},
        },
    },
}


def get_plan_shape(analysis_type: str) -> dict[str, Any] | None:
    shape = ANALYTICS_PLAN_SHAPES.get((analysis_type or "").strip())
    if not shape:
        return None
    return deepcopy(shape)


def list_supported_analysis_types() -> list[str]:
    return list(SUPPORTED_ANALYSIS_TYPES)


def debug_describe_supported_plan_shapes() -> dict[str, Any]:
    return {
        "plan_version": PLAN_VERSION,
        "planner_mode": PLANNER_MODE,
        "intent": INTENT,
        "supported_analysis_types": list_supported_analysis_types(),
        "supported_filter_operators": list(SUPPORTED_FILTER_OPERATORS),
        "supported_time_grains": list(SUPPORTED_TIME_GRAINS),
        "supported_sort_targets": list(SUPPORTED_SORT_TARGETS),
        "supported_final_answer_shapes": list(SUPPORTED_FINAL_ANSWER_SHAPES),
        "shapes": {name: get_plan_shape(name) for name in SUPPORTED_ANALYSIS_TYPES},
    }
