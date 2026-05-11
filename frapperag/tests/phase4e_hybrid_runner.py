from __future__ import annotations

import copy
import json
import os
from datetime import datetime
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.chat_orchestrator import debug_probe_hybrid_path
from frapperag.rag.chat_runner import run_chat_job


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MATRIX_NAME = "phase4e_hybrid_matrix.json"
RUNNER_VERSION = "phase4f_hybrid_v1"
LOG_DOCTYPE = "AI Tool Call Log"


def run_matrix(case_id: str | None = None, write_results: int = 1, matrix_name: str | None = None) -> dict[str, Any]:
    matrix = _load_matrix(matrix_name=matrix_name)
    cases = matrix
    if case_id:
        cases = [entry for entry in matrix if entry.get("case_id") == case_id]
        if not cases:
            frappe.throw(f"Unknown hybrid matrix case_id '{case_id}'.", frappe.ValidationError)

    assistant_mode_before = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    run_suffix = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    results = [_run_case(entry, run_suffix=run_suffix) for entry in cases]
    assistant_mode_after = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"

    passed = sum(1 for result in results if result["grade"] == "PASS")
    summary = {
        "runner_version": RUNNER_VERSION,
        "matrix_name": matrix_name or DEFAULT_MATRIX_NAME,
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
        payload["results_path"] = _write_results(payload, matrix_name=matrix_name or DEFAULT_MATRIX_NAME)
    return payload


def run_case(case_id: str, matrix_name: str | None = None) -> dict[str, Any]:
    return run_matrix(case_id=case_id, write_results=0, matrix_name=matrix_name)


def compare_case(case_id: str, matrix_name: str | None = None) -> dict[str, Any]:
    matrix = _load_matrix(matrix_name=matrix_name)
    case = next((entry for entry in matrix if entry.get("case_id") == case_id), None)
    if not case:
        frappe.throw(f"Unknown hybrid matrix case_id '{case_id}'.", frappe.ValidationError)
    if (case.get("mode") or "").strip() != "live_chat":
        frappe.throw("compare_case currently supports live_chat cases only.", frappe.ValidationError)

    run_suffix = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    hybrid_case = copy.deepcopy(case)
    hybrid_case["assistant_mode"] = "hybrid"
    hybrid_result = _run_case(hybrid_case, run_suffix=run_suffix)

    v1_case = copy.deepcopy(case)
    v1_case["assistant_mode"] = "v1"
    v1_case.pop("test_faults", None)
    v1_result = _run_case(v1_case, run_suffix=run_suffix)
    return {
        "case_id": case_id,
        "matrix_name": matrix_name or DEFAULT_MATRIX_NAME,
        "question": case.get("question") or "",
        "hybrid": hybrid_result.get("actual") or {},
        "v1": v1_result.get("actual") or {},
        "comparison": {
            "hybrid_operations": (hybrid_result.get("actual") or {}).get("operations") or [],
            "v1_operations": (v1_result.get("actual") or {}).get("operations") or [],
            "hybrid_analysis_type": (hybrid_result.get("actual") or {}).get("analysis_type") or "",
            "v1_analysis_type": (v1_result.get("actual") or {}).get("analysis_type") or "",
        },
    }


def _load_matrix(*, matrix_name: str | None = None) -> list[dict[str, Any]]:
    matrix_path = os.path.join(_SPEC_DIR, matrix_name or DEFAULT_MATRIX_NAME)
    with open(matrix_path, "r", encoding="utf-8") as handle:
        matrix = json.load(handle)
    if not isinstance(matrix, list) or not matrix:
        frappe.throw("Hybrid matrix must contain a non-empty list of cases.", frappe.ValidationError)
    return matrix


def _run_case(entry: dict[str, Any], *, run_suffix: str) -> dict[str, Any]:
    restore_state = _apply_policy_overrides(entry.get("policy_overrides") or {})
    previous_faults = _set_test_faults(entry.get("test_faults") or {})
    previous_mode = _set_assistant_mode(entry.get("assistant_mode") or "v1")
    try:
        mode = (entry.get("mode") or "").strip()
        if mode == "debug_probe":
            actual = _run_debug_case(entry, run_suffix=run_suffix)
        elif mode == "live_chat":
            actual = _run_live_chat_case(entry)
        else:
            actual = {
                "error": f"Unsupported case mode '{mode or '<empty>'}'.",
                "tool_logs": [],
            }
        grade, failures = _grade_case(entry, actual)
        return {
            "case_id": entry.get("case_id"),
            "mode": mode,
            "assistant_mode": entry.get("assistant_mode") or "v1",
            "question": entry.get("question") or "",
            "grade": grade,
            "failures": failures,
            "expected": entry.get("expected") or {},
            "actual": actual,
        }
    finally:
        _set_assistant_mode(previous_mode)
        _restore_test_faults(previous_faults)
        _restore_policy_overrides(restore_state)


def _run_debug_case(entry: dict[str, Any], *, run_suffix: str) -> dict[str, Any]:
    route = copy.deepcopy(entry.get("route") or {})
    plan = copy.deepcopy(entry.get("plan") or {})
    request_id = ""
    if plan:
        request_id = f"phase4e-{entry.get('case_id')}-{run_suffix}"
        plan["request_id"] = request_id

    raw_result = debug_probe_hybrid_path(
        question=entry.get("question") or "",
        route_json=_dump_json(route) if route else None,
        plan_json=_dump_json(plan) if plan else None,
        execute=int(entry.get("execute", 1)),
        override_assistant_mode=entry.get("assistant_mode") or "hybrid",
    )
    tool_logs = _get_tool_logs(request_id) if request_id else []
    return {
        **raw_result,
        "request_id": request_id,
        "tool_logs": tool_logs,
        "operations": [row.get("operation") for row in tool_logs],
        "analysis_type": _extract_analysis_type(raw_result, fallback_plan=plan),
    }


def _run_live_chat_case(entry: dict[str, Any]) -> dict[str, Any]:
    question = (entry.get("question") or "").strip()
    run_as = (entry.get("run_as") or "Administrator").strip() or "Administrator"
    original_user = frappe.session.user
    session_name = ""
    message_name = ""
    request_id = ""
    try:
        frappe.set_user(run_as)
        session = frappe.get_doc({
            "doctype": "Chat Session",
            "status": "Open",
            "title": "",
        })
        session.insert(ignore_permissions=True)
        frappe.db.commit()
        session_name = session.name

        message = frappe.get_doc({
            "doctype": "Chat Message",
            "session": session_name,
            "role": "user",
            "content": question,
            "status": "Pending",
        })
        message.insert(ignore_permissions=True)
        frappe.db.commit()
        message_name = message.name
        request_id = f"hybrid-{message_name}"

        execution_error = ""
        try:
            run_chat_job(
                message_id=message_name,
                session_id=session_name,
                user=run_as,
                question=question,
            )
        except Exception as exc:
            execution_error = f"{type(exc).__name__}: {exc}"

        user_message = frappe.db.get_value(
            "Chat Message",
            message_name,
            ["status", "failure_reason", "error_detail", "modified"],
            as_dict=True,
        ) or {}
        reply = frappe.db.get_value(
            "Chat Message",
            {"session": session_name, "role": "assistant"},
            ["name", "status", "content", "citations", "creation", "tokens_used"],
            as_dict=True,
            order_by="creation desc",
        ) or {}
        citations = _parse_citations(reply.get("citations"))
        tool_logs = _get_tool_logs(request_id)
        return {
            "request_id": request_id,
            "session_id": session_name,
            "message_id": message_name,
            "message_status": (reply.get("status") or user_message.get("status") or "").strip(),
            "user_status": user_message.get("status") or "",
            "failure_reason": user_message.get("failure_reason") or "",
            "assistant_content": reply.get("content") or "",
            "assistant_message_name": reply.get("name") or "",
            "tokens_used": reply.get("tokens_used") or 0,
            "citations": citations,
            "citation_types": list({c.get("type") for c in citations if c.get("type")}),
            "citation_result_kind": _extract_citation_result_kind(citations),
            "analysis_type": _extract_live_analysis_type(citations, tool_logs),
            "tool_logs": tool_logs,
            "operations": [row.get("operation") for row in tool_logs],
            "execution_error": execution_error,
        }
    finally:
        frappe.set_user("Administrator")
        if session_name:
            frappe.db.set_value("Chat Session", session_name, "status", "Archived")
            frappe.db.commit()
        frappe.set_user(original_user)


def _grade_case(entry: dict[str, Any], actual: dict[str, Any]) -> tuple[str, list[str]]:
    expected = entry.get("expected") or {}
    failures: list[str] = []
    assistant_text = (actual.get("assistant_content") or actual.get("final_text") or "").strip()

    if "handled" in expected and int(actual.get("handled", 0)) != int(expected.get("handled", 0)):
        failures.append(f"handled mismatch: expected {expected.get('handled')}, got {actual.get('handled')}")

    expected_reason = expected.get("fallback_reason")
    if expected_reason and actual.get("fallback_reason") != expected_reason:
        failures.append(
            f"fallback_reason mismatch: expected {expected_reason!r}, got {actual.get('fallback_reason')!r}"
        )

    expected_message_status = expected.get("message_status")
    if expected_message_status and actual.get("message_status") != expected_message_status:
        failures.append(
            f"message_status mismatch: expected {expected_message_status!r}, got {actual.get('message_status')!r}"
        )

    operations = [row.get("operation") for row in (actual.get("tool_logs") or [])]
    for operation in expected.get("operations_contains") or []:
        if operation not in operations:
            failures.append(f"expected operation {operation!r} not found in {operations!r}")
    for operation in expected.get("operations_not_contains") or []:
        if operation in operations:
            failures.append(f"unexpected operation {operation!r} found in {operations!r}")

    if int(expected.get("no_tool_logs", 0)) and operations:
        failures.append(f"expected no tool logs, found operations {operations!r}")

    citation_types = actual.get("citation_types") or []
    for citation_type in expected.get("citation_types_contains") or []:
        if citation_type not in citation_types:
            failures.append(f"expected citation type {citation_type!r} not found in {citation_types!r}")

    expected_result_kind = expected.get("citation_result_kind")
    if expected_result_kind and actual.get("citation_result_kind") != expected_result_kind:
        failures.append(
            f"citation_result_kind mismatch: expected {expected_result_kind!r}, got {actual.get('citation_result_kind')!r}"
        )

    expected_analysis_type = expected.get("analysis_type")
    if expected_analysis_type and actual.get("analysis_type") != expected_analysis_type:
        failures.append(
            f"analysis_type mismatch: expected {expected_analysis_type!r}, got {actual.get('analysis_type')!r}"
        )

    if int(expected.get("response_not_empty", 0)) and not (actual.get("assistant_content") or "").strip():
        if not assistant_text:
            failures.append("assistant response was empty")

    assistant_contains = expected.get("assistant_contains")
    if assistant_contains and assistant_contains not in assistant_text:
        failures.append(f"expected assistant text to contain {assistant_contains!r}")

    for fragment in expected.get("assistant_contains_any") or []:
        if fragment in assistant_text:
            break
    else:
        if expected.get("assistant_contains_any"):
            failures.append(
                f"expected assistant text to contain one of {expected.get('assistant_contains_any')!r}, got {assistant_text!r}"
            )

    assistant_not_contains = expected.get("assistant_not_contains")
    if assistant_not_contains and assistant_not_contains in assistant_text:
        failures.append(f"assistant text unexpectedly contained {assistant_not_contains!r}")

    expected_execution_status = expected.get("execution_status")
    if expected_execution_status and str((actual.get("execution_result") or {}).get("status") or "") != expected_execution_status:
        failures.append(
            f"execution_status mismatch: expected {expected_execution_status!r}, got {(actual.get('execution_result') or {}).get('status')!r}"
        )

    expected_validated_limit = expected.get("validated_plan_limit")
    if expected_validated_limit is not None and cint((actual.get("validated_plan") or {}).get("limit") or 0) != cint(expected_validated_limit):
        failures.append(
            f"validated_plan.limit mismatch: expected {expected_validated_limit!r}, got {(actual.get('validated_plan') or {}).get('limit')!r}"
        )

    expected_tool_log_count = expected.get("tool_log_count")
    if expected_tool_log_count is not None and len(actual.get("tool_logs") or []) != cint(expected_tool_log_count):
        failures.append(
            f"tool_log_count mismatch: expected {expected_tool_log_count!r}, got {len(actual.get('tool_logs') or [])!r}"
        )

    for expectation in expected.get("tool_log_detail_matches") or []:
        if not _has_matching_tool_log(actual.get("tool_logs") or [], expectation):
            failures.append(f"expected tool log details match not found: {expectation!r}")

    error_contains = expected.get("error_contains")
    if error_contains and error_contains not in str(actual.get("error") or actual.get("execution_error") or ""):
        failures.append(
            f"expected error fragment {error_contains!r} not found in {actual.get('error') or actual.get('execution_error')!r}"
        )

    return ("PASS", failures) if not failures else ("FAIL", failures)


def _extract_analysis_type(raw_result: dict[str, Any], *, fallback_plan: dict[str, Any]) -> str:
    execution_result = raw_result.get("execution_result") or {}
    if execution_result.get("analysis_type"):
        return str(execution_result.get("analysis_type") or "").strip()
    validated_plan = raw_result.get("validated_plan") or {}
    if validated_plan.get("analysis_type"):
        return str(validated_plan.get("analysis_type") or "").strip()
    if fallback_plan.get("analysis_type"):
        return str(fallback_plan.get("analysis_type") or "").strip()
    return ""


def _extract_live_analysis_type(citations: list[dict[str, Any]], tool_logs: list[dict[str, Any]]) -> str:
    for citation in citations:
        if citation.get("analysis_type"):
            return str(citation.get("analysis_type") or "").strip()
    for row in tool_logs:
        plan = row.get("plan") or {}
        if plan.get("analysis_type"):
            return str(plan.get("analysis_type") or "").strip()
    return ""


def _extract_citation_result_kind(citations: list[dict[str, Any]]) -> str:
    for citation in citations:
        if citation.get("result_kind"):
            return str(citation.get("result_kind") or "").strip()
    return ""


def _has_matching_tool_log(tool_logs: list[dict[str, Any]], expectation: dict[str, Any]) -> bool:
    operation = str(expectation.get("operation") or "").strip()
    status = str(expectation.get("status") or "").strip()
    details_expectation = expectation.get("details") or {}
    for row in tool_logs:
        if operation and row.get("operation") != operation:
            continue
        if status and row.get("status") != status:
            continue
        details = row.get("details") or {}
        if all(details.get(key) == value for key, value in details_expectation.items()):
            return True
    return False


def _parse_citations(raw_value: Any) -> list[dict[str, Any]]:
    if not raw_value:
        return []
    if isinstance(raw_value, list):
        return [dict(row) for row in raw_value if isinstance(row, dict)]
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except Exception:
            return []
        if isinstance(parsed, list):
            return [dict(row) for row in parsed if isinstance(row, dict)]
    return []


def _get_tool_logs(request_id: str) -> list[dict[str, Any]]:
    if not request_id or not frappe.db.exists("DocType", LOG_DOCTYPE):
        return []

    rows = frappe.get_all(
        LOG_DOCTYPE,
        filters={"request_id": request_id},
        fields=[
            "name",
            "creation",
            "operation",
            "status",
            "tool_name",
            "doctype_name",
            "intent",
            "assistant_mode",
            "row_count",
            "duration_ms",
            "error_message",
            "plan_json",
            "details_json",
        ],
        order_by="creation asc",
        ignore_permissions=True,
    )
    parsed_rows: list[dict[str, Any]] = []
    for row in rows:
        current = dict(row)
        current["plan"] = _loads_json(current.pop("plan_json", ""))
        current["details"] = _loads_json(current.pop("details_json", ""))
        parsed_rows.append(current)
    return parsed_rows


def _loads_json(raw_value: str) -> dict[str, Any]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _write_results(payload: dict[str, Any], *, matrix_name: str) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    stem = os.path.splitext(os.path.basename(matrix_name))[0]
    path = os.path.join(_SPEC_DIR, f"{stem}_results_{timestamp}.json")
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
    _clear_assistant_cache()
    return restore_state


def _restore_policy_overrides(restore_state: list[dict[str, Any]]) -> None:
    if not restore_state:
        return
    for entry in restore_state:
        _update_policy_row(entry["doctype_name"], entry.get("fields") or {})
    frappe.db.commit()
    _clear_assistant_cache()


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


def _set_assistant_mode(mode: str) -> str:
    previous_mode = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    normalized = (mode or "v1").strip() or "v1"
    frappe.db.set_single_value("AI Assistant Settings", "assistant_mode", normalized)
    frappe.db.commit()
    _clear_assistant_cache()
    return previous_mode


def _set_test_faults(faults: dict[str, Any]) -> dict[str, Any]:
    previous = dict(getattr(frappe.flags, "frapperag_test_faults", None) or {})
    frappe.flags.frapperag_test_faults = dict(faults or {})
    return previous


def _restore_test_faults(previous_faults: dict[str, Any]) -> None:
    if previous_faults:
        frappe.flags.frapperag_test_faults = dict(previous_faults)
        return
    if hasattr(frappe.flags, "frapperag_test_faults"):
        delattr(frappe.flags, "frapperag_test_faults")


def _clear_assistant_cache() -> None:
    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
    frappe.clear_cache(doctype="AI Assistant Settings")
