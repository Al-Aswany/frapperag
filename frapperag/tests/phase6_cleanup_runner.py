from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import frappe

from frapperag.api.indexer import (
    _get_manual_indexing_target_snapshot,
    sidecar_health as get_sidecar_health,
    trigger_indexing,
)
from frapperag.rag import sync_hooks
from frapperag.rag.legacy_vector_policy import LEGACY_VECTOR_DOCTYPES
from frapperag.rag.retriever import filter_by_legacy_retrieval_policy
from frapperag.tests.phase4e_hybrid_runner import run_case as run_hybrid_case


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(_SPEC_DIR)
_README_PATH = os.path.join(os.path.dirname(_APP_ROOT), "README.md")
_SETTINGS_JSON_PATH = os.path.join(
    _APP_ROOT,
    "frapperag",
    "doctype",
    "ai_assistant_settings",
    "ai_assistant_settings.json",
)
_SETTINGS_JS_PATH = os.path.join(
    _APP_ROOT,
    "frapperag",
    "doctype",
    "ai_assistant_settings",
    "ai_assistant_settings.js",
)
_RAG_ADMIN_JSON_PATH = os.path.join(
    _APP_ROOT,
    "frapperag",
    "page",
    "rag_admin",
    "rag_admin.json",
)
_RAG_ADMIN_JS_PATH = os.path.join(
    _APP_ROOT,
    "frapperag",
    "page",
    "rag_admin",
    "rag_admin.js",
)

RUNNER_VERSION = "phase6_cleanup_v1"
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

    results: list[dict[str, Any]] = []
    try:
        _set_transactional_vector_sync_flag(0)
        results.append(_run_hooks_scope_case())
        for doctype in SYNC_PROBE_DOCTYPES:
            results.append(_run_sync_disabled_case(doctype))
        results.append(_run_sync_enabled_case("Customer"))
        results.append(_run_policy_only_doctype_case())
        results.append(_run_manual_indexing_case())
        results.append(_run_manual_indexing_target_filter_case())
        results.append(_run_retrieval_filter_case())
        results.append(_run_sidecar_health_case())
        for case_id in RETAINED_CASES:
            results.append(_run_retained_regression_case(case_id))
        results.append(_run_ui_label_source_case())
        results.append(_run_readme_case())
    finally:
        _set_transactional_vector_sync_flag(transactional_sync_before)

    assistant_mode_after = frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    transactional_sync_after = _get_transactional_vector_sync_flag()
    passed = sum(1 for result in results if result["grade"] == "PASS")

    payload = {
        "summary": {
            "runner_version": RUNNER_VERSION,
            "matrix_name": "phase6_cleanup_runner",
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


def _run_hooks_scope_case() -> dict[str, Any]:
    from frapperag import hooks as app_hooks

    configured = set((app_hooks.doc_events or {}).keys())
    expected = set(LEGACY_VECTOR_DOCTYPES)
    failures: list[str] = []
    if "*" in configured:
        failures.append("doc_events still contains wildcard '*' wiring")
    if configured != expected:
        failures.append(f"doc_events keys mismatch: expected {sorted(expected)!r}, got {sorted(configured)!r}")
    return {
        "case_id": "hooks_scoped_to_legacy_doctypes",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {"configured_doctypes": sorted(configured)},
    }


def _run_sync_disabled_case(doctype: str) -> dict[str, Any]:
    before_log_count = frappe.db.count("Sync Event Log")
    captured_enqueue_calls: list[dict[str, Any]] = []
    doc, source = _load_probe_document(doctype)

    original_enqueue = frappe.enqueue

    def _capture_enqueue(method, **kwargs):
        captured_enqueue_calls.append({"method": method, "kwargs": dict(kwargs)})
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


def _run_sync_enabled_case(doctype: str) -> dict[str, Any]:
    failures: list[str] = []
    doc, source = _load_probe_document(doctype)
    before_names = set(_get_sync_log_names(doctype, getattr(doc, "name", "")))
    captured_enqueue_calls: list[dict[str, Any]] = []
    original_enqueue = frappe.enqueue

    class _CapturedQueueJob:
        id = "phase6-legacy-sync-captured"

    def _capture_enqueue(method, **kwargs):
        captured_enqueue_calls.append({"method": method, "kwargs": dict(kwargs)})
        return _CapturedQueueJob()

    _set_transactional_vector_sync_flag(1)
    error = ""
    try:
        frappe.enqueue = _capture_enqueue
        sync_hooks.on_document_save(doc)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        frappe.enqueue = original_enqueue
        _set_transactional_vector_sync_flag(0)

    after_names = set(_get_sync_log_names(doctype, getattr(doc, "name", "")))
    created_logs = sorted(after_names - before_names)
    try:
        for log_name in created_logs:
            frappe.delete_doc("Sync Event Log", log_name, ignore_permissions=True, force=True)
        if created_logs:
            frappe.db.commit()
    except Exception as exc:
        failures.append(f"cleanup failed for created sync logs {created_logs!r}: {type(exc).__name__}: {exc}")

    if error:
        failures.append(error)
    if len(captured_enqueue_calls) != 1:
        failures.append(f"expected exactly one enqueue call, got {len(captured_enqueue_calls)}")
    elif captured_enqueue_calls[0].get("method") != "frapperag.rag.sync_runner.run_sync_job":
        failures.append(f"unexpected enqueue target: {captured_enqueue_calls[0].get('method')!r}")
    if len(created_logs) != 1:
        failures.append(f"expected exactly one queued Sync Event Log entry, got {created_logs!r}")

    return {
        "case_id": "save_supported_doctype_flag_on_enqueues",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {
            "doctype": doctype,
            "record_name": getattr(doc, "name", ""),
            "document_source": source,
            "created_logs": created_logs,
            "enqueue_calls": captured_enqueue_calls,
        },
    }


def _run_policy_only_doctype_case() -> dict[str, Any]:
    before_log_count = frappe.db.count("Sync Event Log")
    captured_enqueue_calls: list[dict[str, Any]] = []
    doc = _HookProbeDoc("ToDo")

    original_enqueue = frappe.enqueue

    def _capture_enqueue(method, **kwargs):
        captured_enqueue_calls.append({"method": method, "kwargs": dict(kwargs)})
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
        "case_id": "save_policy_only_doctype_never_enqueues",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {
            "doctype": doc.doctype,
            "record_name": doc.name,
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
    health_payload: dict[str, Any] = {}
    vector_available = None

    original_enqueue = frappe.enqueue

    class _CapturedQueueJob:
        id = "phase6-manual-indexing-captured"

    def _capture_enqueue(method, **kwargs):
        enqueue_calls.append({"method": method, "kwargs": dict(kwargs)})
        return _CapturedQueueJob()

    frappe.enqueue = _capture_enqueue
    try:
        health_payload = get_sidecar_health()
        vector_available = bool((health_payload.get("data") or {}).get("vector_available"))
        result = trigger_indexing(doctype)
        created_job_id = result.get("job_id") or ""
        if result.get("status") != "Queued":
            failures.append(f"unexpected trigger_indexing status: {result!r}")
        if not created_job_id or not frappe.db.exists("AI Indexing Job", created_job_id):
            failures.append(f"AI Indexing Job was not created for doctype {doctype!r}")
        else:
            created_queue_job_id = frappe.db.get_value("AI Indexing Job", created_job_id, "queue_job_id") or ""
    except Exception as exc:
        if vector_available is False:
            message = str(exc)
            if "Legacy vector backend is unavailable" not in message:
                failures.append(f"unexpected manual indexing error: {type(exc).__name__}: {exc}")
            if enqueue_calls:
                failures.append(f"manual indexing enqueued unexpectedly while vector backend was unavailable: {enqueue_calls!r}")
        else:
            failures.append(f"{type(exc).__name__}: {exc}")
    finally:
        frappe.enqueue = original_enqueue
        if created_job_id and frappe.db.exists("AI Indexing Job", created_job_id):
            frappe.delete_doc("AI Indexing Job", created_job_id, ignore_permissions=True, force=True)
            frappe.db.commit()

    if vector_available is False:
        if created_job_id:
            failures.append(f"manual indexing created a job unexpectedly while vector backend was unavailable: {created_job_id!r}")
    else:
        if not enqueue_calls:
            failures.append("manual indexing did not enqueue any job")
        else:
            method = enqueue_calls[0].get("method")
            if method != "frapperag.rag.indexer.run_indexing_job":
                failures.append(f"unexpected enqueue target for manual indexing: {method!r}")

    return {
        "case_id": "manual_indexing_single_still_works",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {
            "doctype": doctype,
            "vector_available": vector_available,
            "health_payload": health_payload,
            "job_id": created_job_id,
            "queue_job_id": created_queue_job_id,
            "enqueue_calls": enqueue_calls,
        },
    }


def _run_manual_indexing_target_filter_case() -> dict[str, Any]:
    fake_settings = SimpleNamespace(
        allowed_doctypes=[
            SimpleNamespace(doctype_name="Sales Invoice"),
            SimpleNamespace(doctype_name="ToDo"),
            SimpleNamespace(doctype_name="Customer"),
        ]
    )
    snapshot = _get_manual_indexing_target_snapshot(fake_settings)
    failures: list[str] = []
    if snapshot.get("targets") != ["Customer", "Sales Invoice"]:
        failures.append(f"unexpected legacy/manual targets: {snapshot.get('targets')!r}")
    if snapshot.get("policy_only") != ["ToDo"]:
        failures.append(f"unexpected policy_only DoTypes: {snapshot.get('policy_only')!r}")
    return {
        "case_id": "manual_indexing_full_filters_targets",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": snapshot,
    }


def _run_retrieval_filter_case() -> dict[str, Any]:
    fake_settings = SimpleNamespace(
        is_enabled=1,
        allowed_doctypes=[SimpleNamespace(doctype_name="Sales Invoice")],
    )
    candidates = [
        {"doctype": "Sales Invoice", "name": "SINV-0001", "text": "ok"},
        {"doctype": "Customer", "name": "CUST-0001", "text": "legacy but disallowed"},
        {"doctype": "ToDo", "name": "TODO-0001", "text": "policy only"},
    ]
    filtered = filter_by_legacy_retrieval_policy(candidates, settings=fake_settings)
    failures: list[str] = []
    kept = [(row.get("doctype"), row.get("name")) for row in filtered]
    if kept != [("Sales Invoice", "SINV-0001")]:
        failures.append(f"unexpected filtered candidates: {kept!r}")
    return {
        "case_id": "v1_retrieval_filters_disallowed_candidates",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {"kept": kept},
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


def _run_ui_label_source_case() -> dict[str, Any]:
    checks = {
        _SETTINGS_JSON_PATH: (
            "Enable Legacy Transactional Vector Sync",
            "Allowed ERP DocTypes",
            "Allowed AI Reports",
            "Queryable Fields / Aggregate Policy",
            "Legacy Vector Sync Health",
        ),
        _SETTINGS_JS_PATH: (
            "Legacy Index All",
            "legacy vector sync",
        ),
        _RAG_ADMIN_JSON_PATH: ("Legacy Vector Index Manager",),
        _RAG_ADMIN_JS_PATH: (
            "Start Legacy Indexing",
            "Live ERP querying remains the primary structured-data path.",
            "FrappeAI Assistant",
        ),
    }
    failures: list[str] = []
    actual: dict[str, list[str]] = {}
    for path, required_strings in checks.items():
        content = _read_text(path)
        actual[path] = list(required_strings)
        for needle in required_strings:
            if needle not in content:
                failures.append(f"missing {needle!r} in {path}")
    return {
        "case_id": "ui_labels_updated",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": actual,
    }


def _run_readme_case() -> dict[str, Any]:
    content = _read_text(_README_PATH)
    needles = (
        "FrappeAI Assistant",
        "Legacy Internal Names",
        "Minimal Install Guide",
        "Optional Legacy-Vector Install Guide",
        "Final Smoke Matrix",
    )
    failures = [f"missing {needle!r} in README" for needle in needles if needle not in content]
    return {
        "case_id": "readme_architecture_updated",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": {"checked_strings": list(needles)},
    }


def _load_probe_document(doctype: str) -> tuple[Any, str]:
    name = frappe.db.get_value(doctype, {}, "name", order_by="modified desc")
    if name:
        return frappe.get_doc(doctype, name), "existing_document"
    return _HookProbeDoc(doctype), "synthetic_probe"


def _pick_manual_indexing_doctype() -> str:
    settings = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    configured = set(_get_manual_indexing_target_snapshot(settings).get("targets") or [])
    for doctype in LEGACY_VECTOR_DOCTYPES:
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
    frappe.throw("Phase 6 runner could not find a legacy/manual indexing DocType with records.", frappe.ValidationError)


def _get_transactional_vector_sync_flag() -> int:
    return int(frappe.db.get_single_value("AI Assistant Settings", "enable_transactional_vector_sync") or 0)


def _set_transactional_vector_sync_flag(value: int) -> None:
    frappe.db.set_single_value("AI Assistant Settings", "enable_transactional_vector_sync", int(value))
    frappe.db.commit()
    frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
    frappe.clear_cache(doctype="AI Assistant Settings")


def _get_sync_log_names(doctype: str, record_name: str) -> list[str]:
    return frappe.db.get_all(
        "Sync Event Log",
        filters={"doctype_name": doctype, "record_name": record_name},
        pluck="name",
        ignore_permissions=True,
    )


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _write_results(payload: dict[str, Any]) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SPEC_DIR, f"phase6_cleanup_results_{timestamp}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        handle.write("\n")
    return path


class _HookProbeDoc:
    def __init__(self, doctype: str):
        self.doctype = doctype
        self.name = f"PHASE6-PROBE-{doctype.replace(' ', '-').upper()}"

    def is_new(self) -> bool:
        return False
