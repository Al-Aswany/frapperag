from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any

import frappe

from frapperag.assistant.chat_orchestrator import debug_probe_hybrid_path


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
MATRIX_PATH = os.path.join(_SPEC_DIR, "phase4b_hybrid_matrix.json")


def run_matrix(case_id: str | None = None, write_results: int = 1) -> dict[str, Any]:
    matrix = _load_matrix()
    cases = matrix
    if case_id:
        cases = [entry for entry in matrix if entry.get("case_id") == case_id]
        if not cases:
            frappe.throw(f"Unknown Phase 4B case_id '{case_id}'.", frappe.ValidationError)

    assistant_mode_before = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    results = [_run_case(entry) for entry in cases]
    assistant_mode_after = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"

    passed = sum(1 for result in results if result["grade"] == "PASS")
    summary = {
        "matrix_version": "phase4b_v1",
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
        frappe.throw("Phase 4B matrix must contain a non-empty list of cases.", frappe.ValidationError)
    return matrix


def _run_case(entry: dict[str, Any]) -> dict[str, Any]:
    question = entry.get("question") or ""
    route = entry.get("route")
    plan = entry.get("plan")
    expected = entry.get("expected") or {}

    raw_result = debug_probe_hybrid_path(
        question=question,
        route_json=_dump_json(route) if isinstance(route, dict) else None,
        plan_json=_dump_json(plan) if isinstance(plan, dict) else None,
        execute=int(entry.get("execute", 1)),
        override_assistant_mode="hybrid",
    )
    grade, failures = _grade_case(entry, raw_result)

    return {
        "case_id": entry.get("case_id"),
        "category": entry.get("category"),
        "question": question,
        "grade": grade,
        "failures": failures,
        "expected": expected,
        "actual": raw_result,
    }


def _grade_case(entry: dict[str, Any], actual: dict[str, Any]) -> tuple[str, list[str]]:
    expected = entry.get("expected") or {}
    failures: list[str] = []

    if int(actual.get("handled", 0)) != int(expected.get("handled", 0)):
        failures.append(
            f"handled mismatch: expected {expected.get('handled', 0)}, got {actual.get('handled', 0)}"
        )

    expected_reason = expected.get("fallback_reason")
    if expected_reason and actual.get("fallback_reason") != expected_reason:
        failures.append(
            f"fallback_reason mismatch: expected {expected_reason!r}, got {actual.get('fallback_reason')!r}"
        )

    route_intent = expected.get("route_intent")
    if route_intent:
        actual_route_intent = ((actual.get("route") or {}).get("selected_intent") or "").strip()
        if actual_route_intent != route_intent:
            failures.append(
                f"route_intent mismatch: expected {route_intent!r}, got {actual_route_intent!r}"
            )

    expected_doctype = expected.get("doctype")
    if expected_doctype:
        actual_doctype = _extract_doctype(actual)
        if actual_doctype != expected_doctype:
            failures.append(
                f"doctype mismatch: expected {expected_doctype!r}, got {actual_doctype!r}"
            )

    error_contains = expected.get("error_contains")
    if error_contains and error_contains not in str(actual.get("error") or ""):
        failures.append(
            f"expected error fragment {error_contains!r} not found in {actual.get('error')!r}"
        )

    if int(expected.get("handled", 0)) and "execution_result" not in actual:
        failures.append("expected execution_result for handled case, but none was returned")

    return ("PASS", failures) if not failures else ("FAIL", failures)


def _extract_doctype(actual: dict[str, Any]) -> str:
    validated_plan = actual.get("validated_plan") or {}
    steps = validated_plan.get("steps") or []
    if steps and isinstance(steps[0], dict):
        return str(steps[0].get("doctype") or "").strip()
    return ""


def _write_results(payload: dict[str, Any]) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SPEC_DIR, f"phase4b_hybrid_results_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        handle.write("\n")
    return path


def _dump_json(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
