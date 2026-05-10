from __future__ import annotations

from decimal import Decimal
from typing import Any


def serialize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: _serialize_scalar(value) for key, value in row.items()} for row in rows]


def build_period_comparison_rows(
    *,
    current_rows: list[dict[str, Any]],
    previous_rows: list[dict[str, Any]],
    dimensions: list[str],
    metrics: list[str],
) -> list[dict[str, Any]]:
    previous_by_key = {_row_key(row, dimensions): row for row in previous_rows}
    current_keys = [_row_key(row, dimensions) for row in current_rows]
    all_keys = current_keys + [key for key in previous_by_key if key not in current_keys]

    merged: list[dict[str, Any]] = []
    current_by_key = {_row_key(row, dimensions): row for row in current_rows}
    for key in all_keys:
        current = current_by_key.get(key, {})
        previous = previous_by_key.get(key, {})
        row = {dimension: current.get(dimension, previous.get(dimension)) for dimension in dimensions}
        for metric in metrics:
            current_value = _to_number(current.get(metric, 0))
            previous_value = _to_number(previous.get(metric, 0))
            delta = current_value - previous_value
            pct_change = None
            if previous_value not in (None, 0):
                pct_change = delta / previous_value
            row[f"{metric}_current"] = current_value
            row[f"{metric}_previous"] = previous_value
            row[f"{metric}_delta"] = delta
            row[f"{metric}_pct_change"] = pct_change
        merged.append(row)
    return merged


def sort_rows(
    rows: list[dict[str, Any]],
    *,
    sort_spec: list[dict[str, Any]] | None = None,
    default_sort: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ordered = list(rows)
    spec = sort_spec or default_sort or []
    for entry in reversed(spec):
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        reverse = str(entry.get("direction") or "asc").strip().lower() == "desc"
        ordered.sort(key=lambda row: _sort_value(row.get(name)), reverse=reverse)
    return ordered


def limit_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    return list(rows[:limit])


def _row_key(row: dict[str, Any], dimensions: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(dimension) for dimension in dimensions)


def _serialize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, "")
    if isinstance(value, Decimal):
        return (0, float(value))
    return (0, value)


def _to_number(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    return float(value)
