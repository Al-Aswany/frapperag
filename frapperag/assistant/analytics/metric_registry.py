from __future__ import annotations

from copy import deepcopy
from typing import Any

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


_DEFAULT_ANALYSIS_TYPES = [
    ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
    ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
    ANALYSIS_TYPE_PERIOD_COMPARISON,
    ANALYSIS_TYPE_TOP_N,
    ANALYSIS_TYPE_BOTTOM_N,
    ANALYSIS_TYPE_RATIO,
    ANALYSIS_TYPE_TREND,
]

METRIC_REGISTRY: dict[str, dict[str, Any]] = {
    "sales_amount": {
        "metric_name": "sales_amount",
        "label": "Sales Amount",
        "description": "Sum of Sales Invoice grand totals.",
        "source_doctype": "Sales Invoice",
        "aggregation": "sum",
        "value_field": "grand_total",
        "date_field_hint": "posting_date",
        "analysis_types": _DEFAULT_ANALYSIS_TYPES,
    },
    "sales_qty": {
        "metric_name": "sales_qty",
        "label": "Sales Quantity",
        "description": "Sum of Sales Invoice Item quantities through Sales Invoice.",
        "source_doctype": "Sales Invoice",
        "aggregation": "sum",
        "value_field": "qty",
        "target_doctype": "Sales Invoice Item",
        "relationship_key": "sales_invoice_items",
        "date_field_hint": "posting_date",
        "analysis_types": [
            ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
            ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
            ANALYSIS_TYPE_PERIOD_COMPARISON,
            ANALYSIS_TYPE_TOP_N,
            ANALYSIS_TYPE_BOTTOM_N,
            ANALYSIS_TYPE_TREND,
        ],
    },
    "invoice_count": {
        "metric_name": "invoice_count",
        "label": "Invoice Count",
        "description": "Count of Sales Invoice records.",
        "source_doctype": "Sales Invoice",
        "aggregation": "count",
        "value_field": "name",
        "date_field_hint": "posting_date",
        "analysis_types": _DEFAULT_ANALYSIS_TYPES + [ANALYSIS_TYPE_CO_OCCURRENCE],
    },
    "avg_invoice_value": {
        "metric_name": "avg_invoice_value",
        "label": "Average Invoice Value",
        "description": "Average Sales Invoice grand total.",
        "source_doctype": "Sales Invoice",
        "aggregation": "avg",
        "value_field": "grand_total",
        "date_field_hint": "posting_date",
        "analysis_types": _DEFAULT_ANALYSIS_TYPES,
    },
    "outstanding_amount": {
        "metric_name": "outstanding_amount",
        "label": "Outstanding Amount",
        "description": "Sum of Sales Invoice outstanding amounts.",
        "source_doctype": "Sales Invoice",
        "aggregation": "sum",
        "value_field": "outstanding_amount",
        "date_field_hint": "posting_date",
        "analysis_types": _DEFAULT_ANALYSIS_TYPES,
    },
    "purchase_amount": {
        "metric_name": "purchase_amount",
        "label": "Purchase Amount",
        "description": "Sum of Purchase Invoice grand totals.",
        "source_doctype": "Purchase Invoice",
        "aggregation": "sum",
        "value_field": "grand_total",
        "date_field_hint": "posting_date",
        "analysis_types": _DEFAULT_ANALYSIS_TYPES,
    },
    "purchase_qty": {
        "metric_name": "purchase_qty",
        "label": "Purchase Quantity",
        "description": "Sum of Purchase Invoice Item quantities through Purchase Invoice.",
        "source_doctype": "Purchase Invoice",
        "aggregation": "sum",
        "value_field": "qty",
        "target_doctype": "Purchase Invoice Item",
        "relationship_key": "purchase_invoice_items",
        "date_field_hint": "posting_date",
        "analysis_types": [
            ANALYSIS_TYPE_PARENT_CHILD_AGGREGATE,
            ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
            ANALYSIS_TYPE_PERIOD_COMPARISON,
            ANALYSIS_TYPE_TOP_N,
            ANALYSIS_TYPE_BOTTOM_N,
            ANALYSIS_TYPE_TREND,
        ],
    },
    "stock_qty": {
        "metric_name": "stock_qty",
        "label": "Stock Quantity",
        "description": "Sum of Bin actual quantities.",
        "source_doctype": "Bin",
        "aggregation": "sum",
        "value_field": "actual_qty",
        "analysis_types": [
            ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
            ANALYSIS_TYPE_TOP_N,
            ANALYSIS_TYPE_BOTTOM_N,
        ],
    },
    "movement_qty": {
        "metric_name": "movement_qty",
        "label": "Movement Quantity",
        "description": "Sum of Stock Ledger Entry movement quantities.",
        "source_doctype": "Stock Ledger Entry",
        "aggregation": "sum",
        "value_field": "actual_qty",
        "date_field_hint": "posting_date",
        "analysis_types": [
            ANALYSIS_TYPE_SINGLE_DOCTYPE_AGGREGATE,
            ANALYSIS_TYPE_TIME_BUCKET_AGGREGATE,
            ANALYSIS_TYPE_PERIOD_COMPARISON,
            ANALYSIS_TYPE_TOP_N,
            ANALYSIS_TYPE_BOTTOM_N,
            ANALYSIS_TYPE_TREND,
        ],
    },
}


def get_metric_definition(metric_name: str) -> dict[str, Any] | None:
    definition = METRIC_REGISTRY.get((metric_name or "").strip())
    if not definition:
        return None
    return deepcopy(definition)


def list_metrics(source_doctype: str | None = None) -> list[dict[str, Any]]:
    source_doctype = (source_doctype or "").strip()
    metrics: list[dict[str, Any]] = []
    for definition in METRIC_REGISTRY.values():
        if source_doctype and definition["source_doctype"] != source_doctype:
            continue
        metrics.append(deepcopy(definition))
    return metrics


def debug_describe_metric_registry(source_doctype: str | None = None) -> dict[str, Any]:
    return {
        "metric_count": len(METRIC_REGISTRY),
        "metrics": list_metrics(source_doctype=source_doctype),
    }
