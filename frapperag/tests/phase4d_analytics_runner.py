from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import frappe

from frapperag.assistant.analytics.analytics_executor import debug_validate_and_execute_analytics_plan


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
MATRIX_PATH = os.path.join(_SPEC_DIR, "phase4d_analytics_matrix.json")


def run_matrix(case_id: str | None = None, write_results: int = 1) -> dict[str, Any]:
    matrix = _load_matrix()
    cases = matrix
    if case_id:
        cases = [entry for entry in matrix if entry.get("case_id") == case_id]
        if not cases:
            frappe.throw(f"Unknown Phase 4D analytics case_id '{case_id}'.", frappe.ValidationError)

    assistant_mode_before = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    results = [_run_case(entry) for entry in cases]
    assistant_mode_after = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"

    passed = sum(1 for result in results if result["grade"] == "PASS")
    summary = {
        "matrix_version": "phase4d_option_a_v1",
        "started_at": started_at,
        "assistant_mode_before": assistant_mode_before,
        "assistant_mode_after": assistant_mode_after,
        "case_count": len(results),
        "passed": passed,
        "failed": len(results) - passed,
    }

    payload = {
        "summary": summary,
        "results": results,
    }
    if int(write_results):
        payload["results_path"] = _write_results(payload)
    return payload


def run_case(case_id: str) -> dict[str, Any]:
    return run_matrix(case_id=case_id, write_results=0)


def _load_matrix() -> list[dict[str, Any]]:
    with open(MATRIX_PATH, "r", encoding="utf-8") as handle:
        matrix = json.load(handle)
    if not isinstance(matrix, list) or not matrix:
        frappe.throw("Phase 4D analytics matrix must contain a non-empty list of cases.", frappe.ValidationError)
    return matrix


def _run_case(entry: dict[str, Any]) -> dict[str, Any]:
    plan = entry.get("plan") or {}
    expected = entry.get("expected") or {}
    actual: dict[str, Any]
    restore_state = _apply_policy_overrides(entry.get("policy_overrides") or {})

    try:
        try:
            actual = debug_validate_and_execute_analytics_plan(_dump_json(plan))
            actual_status = actual.get("status") or "success"
        except Exception as exc:
            actual = {
                "status": "rejected",
                "analysis_type": plan.get("analysis_type"),
                "error": str(exc),
                "columns": [],
                "rows": [],
                "row_count": 0,
            }
            actual_status = "rejected"
    finally:
        _restore_policy_overrides(restore_state)

    grade, failures = _grade_case(expected, actual, actual_status)
    return {
        "case_id": entry.get("case_id"),
        "category": entry.get("category"),
        "grade": grade,
        "failures": failures,
        "expected": expected,
        "actual": actual,
    }


def _grade_case(
    expected: dict[str, Any],
    actual: dict[str, Any],
    actual_status: str,
) -> tuple[str, list[str]]:
    failures: list[str] = []

    expected_status = expected.get("status")
    if expected_status and actual_status != expected_status:
        failures.append(f"status mismatch: expected {expected_status!r}, got {actual_status!r}")

    expected_analysis_type = expected.get("analysis_type")
    if expected_analysis_type and actual.get("analysis_type") != expected_analysis_type:
        failures.append(
            f"analysis_type mismatch: expected {expected_analysis_type!r}, got {actual.get('analysis_type')!r}"
        )

    columns_contains = expected.get("columns_contains") or []
    actual_columns = set(actual.get("columns") or [])
    for column in columns_contains:
        if column not in actual_columns:
            failures.append(f"expected column {column!r} not found in {sorted(actual_columns)!r}")

    error_contains = expected.get("error_contains")
    if error_contains and error_contains not in str(actual.get("error") or ""):
        failures.append(f"expected error fragment {error_contains!r} not found in {actual.get('error')!r}")

    min_row_count = expected.get("min_row_count")
    if min_row_count is not None and int(actual.get("row_count") or 0) < int(min_row_count):
        failures.append(
            f"row_count below expectation: expected at least {int(min_row_count)!r}, got {int(actual.get('row_count') or 0)!r}"
        )

    return ("PASS", failures) if not failures else ("FAIL", failures)


def _write_results(payload: dict[str, Any]) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SPEC_DIR, f"phase4d_analytics_results_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        handle.write("\n")
    return path


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


def _apply_policy_overrides(policy_overrides: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if not policy_overrides:
        return []

    restore_state: list[dict[str, Any]] = []
    for doctype_name, overrides in policy_overrides.items():
        doctype_name = str(doctype_name or "").strip()
        if not doctype_name:
            continue
        row = frappe.db.get_value(
            "RAG Allowed DocType",
            {
                "parent": "AI Assistant Settings",
                "parenttype": "AI Assistant Settings",
                "doctype_name": doctype_name,
            },
            ["name", *sorted(overrides.keys())],
            as_dict=True,
        )
        if not row:
            frappe.throw(f"Policy override target '{doctype_name}' is not configured in AI Assistant Settings.")

        restore_state.append(
            {
                "doctype_name": doctype_name,
                "fields": {fieldname: row.get(fieldname) for fieldname in overrides},
            }
        )
        _update_policy_row(doctype_name, overrides)

    frappe.db.commit()
    _clear_policy_cache()
    return restore_state


def _restore_policy_overrides(restore_state: list[dict[str, Any]]) -> None:
    if not restore_state:
        return

    for entry in restore_state:
        _update_policy_row(entry["doctype_name"], entry.get("fields") or {})
    frappe.db.commit()
    _clear_policy_cache()


def _update_policy_row(doctype_name: str, fields: dict[str, Any]) -> None:
    if not fields:
        return

    assignments: list[str] = []
    params: dict[str, Any] = {
        "parent": "AI Assistant Settings",
        "parenttype": "AI Assistant Settings",
        "doctype_name": doctype_name,
    }
    for fieldname, value in fields.items():
        assignments.append(f"`{fieldname}` = %({fieldname})s")
        params[fieldname] = value

    frappe.db.sql(
        f"""
        UPDATE `tabRAG Allowed DocType`
        SET {", ".join(assignments)}
        WHERE parent = %(parent)s
            AND parenttype = %(parenttype)s
            AND doctype_name = %(doctype_name)s
        """,
        params,
    )


def _clear_policy_cache() -> None:
    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
    frappe.clear_cache(doctype="AI Assistant Settings")
