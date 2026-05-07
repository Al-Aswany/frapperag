from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.planner import debug_create_get_list_plan
from frapperag.assistant.plan_validator import validate_plan
from frapperag.assistant.tool_call_log import log_tool_call


def execute_validated_plan(validated_plan: dict[str, Any] | str) -> dict[str, Any]:
    started = time.monotonic()
    plan = validate_plan(
        validated_plan,
        require_validated_flag=True,
        log_result=False,
    )
    results: list[dict[str, Any]] = []
    total_rows = 0

    try:
        for step in plan.get("steps") or []:
            rows = frappe.get_list(
                step["doctype"],
                fields=step["fields"],
                filters=[_to_frappe_filter(current) for current in (step.get("filters") or [])],
                order_by=step["order_by"],
                limit_page_length=cint(step["limit"]),
            )
            total_rows += len(rows)
            results.append(
                {
                    "step_id": step["step_id"],
                    "tool": step["tool"],
                    "doctype": step["doctype"],
                    "fields": step["fields"],
                    "row_count": len(rows),
                    "rows": [_serialize_row(row) for row in rows],
                }
            )
    except Exception as exc:
        log_tool_call(
            "executor.get_list.execute_validated_plan",
            "Failed",
            tool_name="get_list",
            doctype_name=",".join(sorted({step["doctype"] for step in (plan.get("steps") or [])})),
            request_id=plan.get("request_id"),
            intent=plan.get("intent"),
            duration_ms=_duration_ms(started),
            error_message=str(exc),
            plan=plan,
            details={"step_count": len(plan.get("steps") or [])},
        )
        raise

    result = {
        "request_id": plan.get("request_id"),
        "intent": plan.get("intent"),
        "validated": 1,
        "steps": results,
        "total_rows": total_rows,
    }
    log_tool_call(
        "executor.get_list.execute_validated_plan",
        "Success",
        tool_name="get_list",
        doctype_name=",".join(sorted({step["doctype"] for step in (plan.get("steps") or [])})),
        request_id=plan.get("request_id"),
        intent=plan.get("intent"),
        row_count=total_rows,
        duration_ms=_duration_ms(started),
        plan=plan,
        details={
            "step_count": len(results),
            "result_summary": [
                {
                    "step_id": step_result["step_id"],
                    "doctype": step_result["doctype"],
                    "row_count": step_result["row_count"],
                }
                for step_result in results
            ],
        },
    )
    return result


def debug_execute_plan(plan_json: str) -> dict[str, Any]:
    return execute_validated_plan(plan_json)


def debug_validate_and_execute_plan(plan_json: str) -> dict[str, Any]:
    validated = validate_plan(plan_json, require_validated_flag=False, log_result=True)
    return execute_validated_plan(validated)


def debug_build_validate_and_execute_get_list_plan(
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
    validated = validate_plan(plan, require_validated_flag=False, log_result=True)
    return execute_validated_plan(validated)


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _to_frappe_filter(current: dict[str, Any]) -> list[Any]:
    operator = current["operator"]
    value = current["value"]
    if operator == "like_prefix":
        operator = "like"
        value = f"{value}%"
    return [current["field"], operator, value]


def _serialize_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)

    as_dict = getattr(row, "as_dict", None)
    if callable(as_dict):
        return as_dict()

    return dict(row)
