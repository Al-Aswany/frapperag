from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import frappe

from frapperag.api.indexer import trigger_indexing
from frapperag.api.indexer import sidecar_health as get_sidecar_health
from frapperag.rag import sync_hooks
from frapperag.rag.transactional_vector_sync import TRANSACTIONAL_VECTOR_DOCTYPES
from frapperag.tests.phase4e_hybrid_runner import run_case as run_hybrid_case


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER_VERSION = "phase5_transactional_vector_sync_v1"
PHASE4F_MATRIX = "phase4f_analytics_hardening_matrix.json"
RETAINED_CASES = (
    "top_customers_by_sales",
    "sales_by_month",
    "most_sold_item_pairs_this_year",
    "assistant_mode_v1_bypasses_analytics",
    "hybrid_normal_structured_get_list_works",
)
SYNC_PROBE_DOCTYPES = (
    "Customer",
    "Item",
    "Sales Invoice",
    "Purchase Invoice",
)


def run_matrix(write_results: int = 1) -> dict[str, Any]:
    assistant_mode_before = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    transactional_sync_before = _get_transactional_vector_sync_flag()
    started_at = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

    _set_transactional_vector_sync_flag(0)

    results = []
    for doctype in SYNC_PROBE_DOCTYPES:
        results.append(_run_sync_disabled_case(doctype))
    results.append(_run_manual_indexing_case())
    results.append(_run_sidecar_health_case())
    for case_id in RETAINED_CASES:
        results.append(_run_retained_regression_case(case_id))

    assistant_mode_after = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    transactional_sync_after = _get_transactional_vector_sync_flag()
    passed = sum(1 for result in results if result["grade"] == "PASS")

    payload = {
        "summary": {
            "runner_version": RUNNER_VERSION,
            "matrix_name": "phase5_transactional_vector_sync_runner",
            "started_at": started_at,
            "assistant_mode_before": assistant_mode_before,
            "assistant_mode_after": assistant_mode_after,
            "transactional_vector_sync_before": transactional_sync_before,
            "transactional_vector_sync_after": transactional_sync_after,
            "case_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "results": results,
    }
    if int(write_results):
        payload["results_path"] = _write_results(payload)
    return payload


def _run_sync_disabled_case(doctype: str) -> dict[str, Any]:
    before_log_count = frappe.db.count("Sync Event Log")
    captured_enqueue_calls: list[dict[str, Any]] = []
    doc, source = _load_probe_document(doctype)

    original_enqueue = frappe.enqueue

    def _capture_enqueue(method, **kwargs):
        captured_enqueue_calls.append({
            "method": method,
            "kwargs": dict(kwargs),
        })
        return None

    frappe.enqueue = _capture_enqueue
    error = ""
    try:
        sync_hooks.on_document_save(doc)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        frappe.enqueue = original_enqueue

    after_log_count = frappe.db.count("Sync Event Log")
    failures: list[str] = []
    if error:
        failures.append(error)
    if captured_enqueue_calls:
        failures.append(f"unexpected enqueue calls: {captured_enqueue_calls!r}")
    if after_log_count != before_log_count:
        failures.append(
            f"Sync Event Log count changed unexpectedly: before={before_log_count}, after={after_log_count}"
        )

    return {
        "case_id": f"{doctype.lower().replace(' ', '_')}_save_does_not_enqueue_vector_sync",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {
            "doctype": doctype,
            "record_name": getattr(doc, "name", ""),
            "document_source": source,
            "log_count_before": before_log_count,
            "log_count_after": after_log_count,
            "enqueue_call_count": len(captured_enqueue_calls),
        },
    }


def _run_manual_indexing_case() -> dict[str, Any]:
    failures: list[str] = []
    doctype = _pick_manual_indexing_doctype()
    created_job_id = ""
    created_queue_job_id = ""
    enqueue_calls: list[dict[str, Any]] = []

    original_enqueue = frappe.enqueue

    class _CapturedQueueJob:
        id = "phase5-manual-indexing-captured"

    def _capture_enqueue(method, **kwargs):
        enqueue_calls.append({
            "method": method,
            "kwargs": dict(kwargs),
        })
        return _CapturedQueueJob()

    frappe.enqueue = _capture_enqueue
    try:
        result = trigger_indexing(doctype)
        created_job_id = result.get("job_id") or ""
        if result.get("status") != "Queued":
            failures.append(f"unexpected trigger_indexing status: {result!r}")
        if not created_job_id or not frappe.db.exists("AI Indexing Job", created_job_id):
            failures.append(f"AI Indexing Job was not created for doctype {doctype!r}")
        else:
            created_queue_job_id = frappe.db.get_value("AI Indexing Job", created_job_id, "queue_job_id") or ""
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        frappe.enqueue = original_enqueue
        if created_job_id and frappe.db.exists("AI Indexing Job", created_job_id):
            frappe.delete_doc("AI Indexing Job", created_job_id, ignore_permissions=True, force=True)
            frappe.db.commit()

    if not enqueue_calls:
        failures.append("manual indexing did not enqueue any job")
    else:
        method = enqueue_calls[0].get("method")
        if method != "frapperag.rag.indexer.run_indexing_job":
            failures.append(f"unexpected enqueue target for manual indexing: {method!r}")

    return {
        "case_id": "manual_indexing_still_works",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {
            "doctype": doctype,
            "job_id": created_job_id,
            "queue_job_id": created_queue_job_id,
            "enqueue_calls": enqueue_calls,
        },
    }


def _run_sidecar_health_case() -> dict[str, Any]:
    failures: list[str] = []
    response: dict[str, Any] = {}
    try:
        response = get_sidecar_health()
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")

    if response and response.get("ok") is False:
        failures.append(f"sidecar health check reported unhealthy: {response!r}")

    return {
        "case_id": "sidecar_health_unchanged",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": response,
    }


def _run_retained_regression_case(case_id: str) -> dict[str, Any]:
    failures: list[str] = []
    actual: dict[str, Any] = {}
    try:
        actual = run_hybrid_case(case_id, matrix_name=PHASE4F_MATRIX)
        first_result = ((actual.get("results") or [None])[0] or {})
        if first_result.get("grade") != "PASS":
            failures.extend(first_result.get("failures") or [f"retained case {case_id!r} did not pass"])
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")

    return {
        "case_id": case_id,
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": actual,
    }


def _load_probe_document(doctype: str) -> tuple[Any, str]:
    name = frappe.db.get_value(doctype, {}, "name", order_by="modified desc")
    if name:
        return frappe.get_doc(doctype, name), "existing_document"
    return _HookProbeDoc(doctype), "synthetic_probe"


def _pick_manual_indexing_doctype() -> str:
    settings = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    configured = {
        row.doctype_name
        for row in (getattr(settings, "allowed_doctypes", None) or [])
        if getattr(row, "doctype_name", None)
    }
    for doctype in TRANSACTIONAL_VECTOR_DOCTYPES:
        if doctype not in configured:
            continue
        if not frappe.db.get_value(doctype, {}, "name"):
            continue
        active = frappe.db.exists(
            "AI Indexing Job",
            {"doctype_to_index": doctype, "status": ["in", ["Queued", "Running"]]},
        )
        if not active:
            return doctype
    frappe.throw("Phase 5 runner could not find an allowed DocType with records for manual indexing verification.")


def _get_transactional_vector_sync_flag() -> int:
    return int(frappe.db.get_single_value("AI Assistant Settings", "enable_transactional_vector_sync") or 0)


def _set_transactional_vector_sync_flag(value: int) -> None:
    frappe.db.set_single_value("AI Assistant Settings", "enable_transactional_vector_sync", int(value))
    frappe.db.commit()
    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
    frappe.clear_cache(doctype="AI Assistant Settings")


def _write_results(payload: dict[str, Any]) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SPEC_DIR, f"phase5_transactional_vector_sync_results_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        import json

        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        handle.write("\n")
    return path


class _HookProbeDoc:
    def __init__(self, doctype: str):
        self.doctype = doctype
        self.name = f"PHASE5-PROBE-{doctype.replace(' ', '-').upper()}"

    def is_new(self) -> bool:
        return False
