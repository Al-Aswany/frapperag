"""Whitelisted API for local model install and active-prefix status.

Workers talk to the sidecar via HTTP (Constitution Principle IV) — this module
never imports lancedb or sentence_transformers directly.
"""

import time
import frappe
from frappe import _


def _read_total_memory_bytes() -> int:
    """Read total available memory from cgroup v2, cgroup v1, or psutil (last resort)."""
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            val = f.read().strip()
        if val != "max":
            return int(val)
    except Exception:
        pass
    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            return int(f.read().strip())
    except Exception:
        pass
    try:
        import psutil
        return psutil.virtual_memory().total
    except Exception:
        pass
    return 0


@frappe.whitelist()
def install_local_model(hf_token: str | None = None) -> dict:
    """Enqueue a background job that downloads and tests multilingual-e5-small.

    Memory check runs here (fast, synchronous) to fail fast before queuing.
    Returns {"job_id": str}.
    """
    mem_total = _read_total_memory_bytes()
    if mem_total and mem_total < 2 * 1024 ** 3:
        frappe.throw(
            _("Local model requires ≥2 GB RAM. Detected {0:.2f} GB.").format(
                mem_total / 1024 ** 3
            )
        )

    from frappe.utils import now_datetime
    job_id = f"rag_local_install_{now_datetime():%Y%m%d_%H%M%S}"
    frappe.enqueue(
        "frapperag.api.local_model._run_install",
        queue="long",
        timeout=1800,
        job_name=job_id,
        hf_token=hf_token,
        job_id=job_id,
        user=frappe.session.user,
    )
    return {"job_id": job_id}


def _run_install(hf_token: str | None, job_id: str, user: str) -> None:
    """Background worker: delegates heavy download to the sidecar, polls and re-publishes progress."""
    from frapperag.rag.sidecar_client import (
        install_local_model as sidecar_install,
        install_local_model_status,
    )

    resp = sidecar_install(hf_token)
    install_id = resp["install_id"]

    while True:
        s = install_local_model_status(install_id)
        frappe.publish_realtime(
            event="rag_local_model_install_progress",
            message={"job_id": job_id, **s},
            user=user,
            after_commit=False,
        )
        if s.get("terminal"):
            break
        time.sleep(1.0)


@frappe.whitelist()
def get_active_prefix_status() -> dict:
    """Return the active prefix, populated tables, and expected doctypes.

    Used by the dashboard banner in AI Assistant Settings to detect an empty prefix.
    """
    from frapperag.rag.sidecar_client import tables_populated, _active_table_prefix

    try:
        settings = frappe.get_cached_doc("AI Assistant Settings")
        expected = [r.doctype_name for r in (settings.allowed_doctypes or [])]
        provider = settings.embedding_provider or "gemini"
        prefix = _active_table_prefix()
        result = tables_populated(prefix)
        return {
            "provider": provider,
            "prefix": prefix,
            "populated_tables": result.get("tables", []),
            "expected_doctypes": expected,
        }
    except Exception as exc:
        frappe.log_error(title="get_active_prefix_status failed", message=str(exc))
        return {"provider": "gemini", "prefix": "v5_gemini_", "populated_tables": [], "expected_doctypes": []}
