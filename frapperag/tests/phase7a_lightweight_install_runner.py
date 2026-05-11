from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import frappe


_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_ROOT = os.path.dirname(_SPEC_DIR)
_REPO_ROOT = os.path.dirname(_APP_ROOT)
_PYPROJECT_PATH = os.path.join(_REPO_ROOT, "pyproject.toml")
_README_PATH = os.path.join(_REPO_ROOT, "README.md")
_REQ_CORE_PATH = os.path.join(_APP_ROOT, "requirements.txt")
_REQ_VECTOR_PATH = os.path.join(_APP_ROOT, "requirements-legacy-vector.txt")
_REQ_LOCAL_PATH = os.path.join(_APP_ROOT, "requirements-local-embeddings.txt")
_REQ_DOCS_PATH = os.path.join(_APP_ROOT, "requirements-documents.txt")
_CHAT_RUNNER_PATH = os.path.join(_APP_ROOT, "rag", "chat_runner.py")

RUNNER_VERSION = "phase7a_lightweight_install_v1"


def run_matrix(write_results: int = 1) -> dict[str, Any]:
    results = [
        _run_dependency_split_case(),
        _run_readme_install_docs_case(),
        _run_sidecar_health_contract_case(),
        _run_sidecar_feature_error_case(),
        _run_active_prefix_status_case(),
        _run_indexer_preflight_case(),
        _run_v1_fallback_message_case(),
    ]
    passed = sum(1 for result in results if result["grade"] == "PASS")
    payload = {
        "summary": {
            "runner_version": RUNNER_VERSION,
            "matrix_name": "phase7a_lightweight_install_runner",
            "started_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "case_count": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "results": results,
    }
    if int(write_results):
        payload["results_path"] = _write_results(payload)
    return payload


def _run_dependency_split_case() -> dict[str, Any]:
    pyproject = _read(_PYPROJECT_PATH)
    core_requirements = _read(_REQ_CORE_PATH)
    vector_requirements = _read(_REQ_VECTOR_PATH)
    local_requirements = _read(_REQ_LOCAL_PATH)
    documents_requirements = _read(_REQ_DOCS_PATH)

    failures: list[str] = []
    for forbidden in ("lancedb", "pyarrow", "sentence-transformers", "torch"):
        if forbidden in core_requirements:
            failures.append(f"core requirements still contain {forbidden!r}")

    for required in ('legacy-vector = [', '"lancedb"', '"pyarrow"'):
        if required not in pyproject:
            failures.append(f"pyproject missing optional dependency marker {required!r}")

    for required in ('local-embeddings = [', '"sentence-transformers"', '"huggingface-hub"'):
        if required not in pyproject:
            failures.append(f"pyproject missing local embedding marker {required!r}")

    if "lancedb" not in vector_requirements or "pyarrow" not in vector_requirements:
        failures.append("legacy vector requirements file is incomplete")
    if "sentence-transformers" not in local_requirements or "huggingface-hub" not in local_requirements:
        failures.append("local embeddings requirements file is incomplete")
    if "Reserved for future file/document parser dependencies." not in documents_requirements:
        failures.append("documents requirements file is missing the reserved placeholder")

    return {
        "case_id": "dependency_split_present",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def _run_readme_install_docs_case() -> dict[str, Any]:
    readme = _read(_README_PATH)
    failures: list[str] = []
    for required in (
        "requirements-legacy-vector.txt",
        "requirements-local-embeddings.txt",
        "pip install -e 'apps/frapperag[legacy-vector]'",
        "pip install torch --index-url https://download.pytorch.org/whl/cpu",
    ):
        if required not in readme:
            failures.append(f"README missing install guidance {required!r}")
    return {
        "case_id": "readme_optional_install_docs_present",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def _run_sidecar_health_contract_case() -> dict[str, Any]:
    from frapperag.sidecar import main as sidecar_main

    payload = sidecar_main._health_payload()
    failures = [
        f"health payload missing {key!r}"
        for key in (
            "chat_available",
            "vector_available",
            "vector_reason",
            "provider",
            "local_embeddings_available",
            "table_prefix",
        )
        if key not in payload
    ]
    if payload.get("chat_available") is not True:
        failures.append("chat_available should be true in health payload")

    return {
        "case_id": "sidecar_health_contract_present",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": payload,
    }


def _run_sidecar_feature_error_case() -> dict[str, Any]:
    from frapperag.rag import sidecar_client

    class _FakeResponse:
        status_code = 409
        text = '{"detail":"Vector backend unavailable","error_code":"feature_unavailable"}'

        def json(self) -> dict[str, Any]:
            return {"detail": "Vector backend unavailable", "error_code": "feature_unavailable"}

    failures: list[str] = []
    try:
        sidecar_client._retry_call(lambda *args, **kwargs: _FakeResponse(), "http://sidecar.test/feature")
        failures.append("feature-unavailable response did not raise")
    except sidecar_client.SidecarFeatureUnavailableError:
        pass
    except Exception as exc:
        failures.append(f"unexpected exception type: {type(exc).__name__}: {exc}")

    return {
        "case_id": "sidecar_feature_unavailable_error_classified",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def _run_active_prefix_status_case() -> dict[str, Any]:
    from frapperag.api import local_model
    from frapperag.rag import sidecar_client

    original_get_cached_doc = frappe.get_cached_doc
    original_health_check = sidecar_client.health_check
    original_tables_populated = sidecar_client.tables_populated
    original_active_table_prefix = sidecar_client._active_table_prefix

    try:
        frappe.get_cached_doc = lambda *args, **kwargs: SimpleNamespace(
            allowed_doctypes=[SimpleNamespace(doctype_name="Sales Invoice")],
            embedding_provider="e5-small",
        )
        sidecar_client.health_check = lambda port=None: {
            "ok": True,
            "url": "http://127.0.0.1:8100/health",
            "detail": None,
            "data": {
                "vector_available": False,
                "vector_reason": "sentence_transformers unavailable",
                "local_embeddings_available": False,
                "can_install_local_model": False,
            },
        }
        sidecar_client.tables_populated = lambda prefix, port=None: {
            "populated": False,
            "tables": [],
            "prefix": prefix,
            "available": False,
            "reason": "sentence_transformers unavailable",
        }
        sidecar_client._active_table_prefix = lambda: "v6_e5small_"

        result = local_model.get_active_prefix_status()
    finally:
        frappe.get_cached_doc = original_get_cached_doc
        sidecar_client.health_check = original_health_check
        sidecar_client.tables_populated = original_tables_populated
        sidecar_client._active_table_prefix = original_active_table_prefix

    failures = [
        f"status payload missing {key!r}"
        for key in (
            "vector_available",
            "vector_reason",
            "local_embeddings_available",
            "can_install_local_model",
            "sidecar_ok",
        )
        if key not in result
    ]
    if result.get("vector_available") is not False:
        failures.append("vector_available should be false in the mocked e5-small missing-deps case")

    return {
        "case_id": "active_prefix_status_includes_capabilities",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
        "actual": result,
    }


def _run_indexer_preflight_case() -> dict[str, Any]:
    from frapperag.api import indexer
    from frapperag.rag import sidecar_client

    original_health_check = sidecar_client.health_check
    failures: list[str] = []
    try:
        sidecar_client.health_check = lambda port=None: {
            "ok": True,
            "url": "http://127.0.0.1:8100/health",
            "detail": None,
            "data": {
                "vector_available": False,
                "vector_reason": "lancedb unavailable",
            },
        }
        try:
            indexer._require_vector_backend_available()
            failures.append("indexer preflight did not raise when vector backend was unavailable")
        except frappe.ValidationError:
            pass
    finally:
        sidecar_client.health_check = original_health_check

    return {
        "case_id": "indexer_preflight_blocks_unavailable_vector_backend",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def _run_v1_fallback_message_case() -> dict[str, Any]:
    source = _read(_CHAT_RUNNER_PATH)
    failures: list[str] = []
    if "Legacy vector retrieval is unavailable in this install." not in source:
        failures.append("chat runner missing the v1 vector-unavailable fallback message")
    if "SidecarFeatureUnavailableError" not in source:
        failures.append("chat runner missing explicit SidecarFeatureUnavailableError handling")
    return {
        "case_id": "v1_fallback_message_present",
        "grade": "PASS" if not failures else "FAIL",
        "failures": failures,
    }


def _read(path: str) -> str:
    with open(path) as handle:
        return handle.read()


def _write_results(payload: dict[str, Any]) -> str:
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(_SPEC_DIR, f"phase7a_lightweight_install_results_{timestamp}.json")
    with open(path, "w") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return path
