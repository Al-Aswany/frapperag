from __future__ import annotations

import json
from typing import Any

import frappe
from frappe.utils import cint


LOG_DOCTYPE = "AI Tool Call Log"
_MAX_JSON_CHARS = 20_000


def log_tool_call(
    operation: str,
    status: str,
    *,
    tool_name: str | None = None,
    doctype_name: str | None = None,
    user: str | None = None,
    request_id: str | None = None,
    intent: str | None = None,
    assistant_mode: str | None = None,
    row_count: int | None = None,
    duration_ms: int | None = None,
    error_message: str | None = None,
    plan: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "doctype": LOG_DOCTYPE,
        "operation": (operation or "").strip()[:140],
        "status": (status or "").strip()[:40] or "Unknown",
        "tool_name": (tool_name or "").strip()[:140],
        "doctype_name": (doctype_name or "").strip()[:140],
        "user_id": (user or frappe.session.user or "Guest")[:140],
        "request_id": (request_id or "").strip()[:140],
        "intent": (intent or "").strip()[:140],
        "assistant_mode": (assistant_mode or _get_assistant_mode())[:40],
        "row_count": cint(row_count or 0),
        "duration_ms": cint(duration_ms or 0),
        "error_message": (error_message or "")[:1000],
        "plan_json": _dump_json(plan),
        "details_json": _dump_json(details),
    }

    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    logger.info(
        "[AI_TOOL_CALL] operation=%s status=%s tool=%s doctype=%s request_id=%s rows=%s duration_ms=%s error=%s",
        payload["operation"],
        payload["status"],
        payload["tool_name"],
        payload["doctype_name"],
        payload["request_id"],
        payload["row_count"],
        payload["duration_ms"],
        payload["error_message"] or "",
    )

    if not frappe.db.exists("DocType", LOG_DOCTYPE):
        return {"logged": False, "reason": "missing_doctype"}

    try:
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        return {"logged": True, "name": doc.name}
    except Exception:
        logger.exception(
            "[AI_TOOL_CALL_LOG_INSERT_FAILED] operation=%s status=%s request_id=%s",
            payload["operation"],
            payload["status"],
            payload["request_id"],
        )
        return {"logged": False, "reason": "insert_failed"}


def debug_get_recent_tool_logs(limit: int = 10) -> dict[str, Any]:
    limit = max(1, min(cint(limit or 10), 50))
    if not frappe.db.exists("DocType", LOG_DOCTYPE):
        return {"logs": [], "available": False}
    rows = frappe.get_all(
        LOG_DOCTYPE,
        fields=[
            "name",
            "creation",
            "operation",
            "status",
            "tool_name",
            "doctype_name",
            "user_id",
            "request_id",
            "intent",
            "assistant_mode",
            "row_count",
            "duration_ms",
            "error_message",
        ],
        order_by="creation desc",
        limit=limit,
        ignore_permissions=True,
    )
    return {"logs": [dict(row) for row in rows], "available": True}


def _dump_json(value: dict[str, Any] | None) -> str:
    if not value:
        return ""

    text = json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)
    if len(text) > _MAX_JSON_CHARS:
        text = text[: _MAX_JSON_CHARS - 15] + "...[truncated]"
    return text


def _get_assistant_mode() -> str:
    try:
        return frappe.db.get_single_value("AI Assistant Settings", "assistant_mode") or "v1"
    except Exception:
        return "v1"
