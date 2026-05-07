from __future__ import annotations

import time
from typing import Any

import frappe

from frapperag.assistant.answer_composer import compose_structured_answer
from frapperag.assistant.executors.get_list_executor import execute_validated_plan
from frapperag.assistant.intent_router import INTENT_STRUCTURED_QUERY, route_question
from frapperag.assistant.plan_validator import validate_plan
from frapperag.assistant.planner import SUPPORTED_TOOL, plan_structured_query
from frapperag.assistant.tool_call_log import log_tool_call


HYBRID_MIN_ROUTE_CONFIDENCE = 0.65


def try_generate_hybrid_response(
    *,
    question: str,
    user: str,
    message_id: str | None = None,
    session_id: str | None = None,
    route: dict[str, Any] | None = None,
    settings: Any | None = None,
    api_key: str | None = None,
) -> dict[str, Any] | None:
    settings = settings or frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    assistant_mode = ((getattr(settings, "assistant_mode", None) or "v1").strip() or "v1").lower()
    if assistant_mode != "hybrid":
        return None

    route = route or route_question(question, use_llm_fallback=False, settings=settings)
    if route.get("selected_intent") != INTENT_STRUCTURED_QUERY:
        _log().info(
            "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=non_structured intent=%s",
            message_id,
            session_id,
            route.get("selected_intent"),
        )
        return None

    if float(route.get("confidence") or 0.0) < HYBRID_MIN_ROUTE_CONFIDENCE:
        _log().info(
            "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=low_confidence confidence=%s",
            message_id,
            session_id,
            route.get("confidence"),
        )
        return None

    request_id = _request_id_for_message(message_id)
    started = time.monotonic()

    try:
        plan = plan_structured_query(
            question,
            route,
            settings=settings,
            request_id=request_id,
        )
        if not plan:
            _log().info(
                "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=planner_rejected request_id=%s",
                message_id,
                session_id,
                request_id,
            )
            return None

        if not _is_supported_hybrid_plan(plan):
            _log_unsupported_plan(plan, request_id=request_id, user=user)
            _log().info(
                "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=unsupported_plan request_id=%s",
                message_id,
                session_id,
                request_id,
            )
            return None

        validated = validate_plan(plan, require_validated_flag=False, log_result=True)
        if not _is_supported_hybrid_plan(validated):
            _log_unsupported_plan(validated, request_id=request_id, user=user)
            _log().info(
                "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=validated_plan_unsupported request_id=%s",
                message_id,
                session_id,
                request_id,
            )
            return None

        execution_result = execute_validated_plan(validated)
        composed = compose_structured_answer(
            question=question,
            route=route,
            validated_plan=validated,
            execution_result=execution_result,
            settings=settings,
            api_key=api_key,
        )
        citations = build_query_result_citations(validated, execution_result)
        _log().info(
            "[HYBRID_SUCCESS] message_id=%s session_id=%s request_id=%s rows=%s duration_ms=%s",
            message_id,
            session_id,
            request_id,
            execution_result.get("total_rows"),
            _duration_ms(started),
        )
        return {
            "final_text": composed["text"],
            "citations": citations,
            "tokens_used": composed["tokens_used"],
            "request_id": request_id,
        }
    except frappe.ValidationError:
        _log().info(
            "[HYBRID_FALLBACK] message_id=%s session_id=%s reason=validation_rejection request_id=%s",
            message_id,
            session_id,
            request_id,
        )
        return None
    except Exception:
        _log().exception(
            "[HYBRID_FALLBACK_ERROR] message_id=%s session_id=%s request_id=%s",
            message_id,
            session_id,
            request_id,
        )
        return None


def build_query_result_citations(
    validated_plan: dict[str, Any],
    execution_result: dict[str, Any],
) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    steps_by_id = {
        step.get("step_id"): step
        for step in (validated_plan.get("steps") or [])
        if isinstance(step, dict)
    }
    for step_result in (execution_result.get("steps") or []):
        step_id = step_result.get("step_id")
        step = steps_by_id.get(step_id, {})
        fields = step_result.get("fields") or step.get("fields") or []
        citations.append(
            {
                "type": "query_result",
                "doctype": step_result.get("doctype") or step.get("doctype"),
                "columns": fields,
                "rows": [
                    [_normalize_cell(row.get(field)) for field in fields]
                    for row in (step_result.get("rows") or [])
                ],
                "row_count": step_result.get("row_count") or 0,
            }
        )
    return citations


def _is_supported_hybrid_plan(plan: dict[str, Any]) -> bool:
    steps = plan.get("steps") or []
    if len(steps) != 1:
        return False
    step = steps[0] if isinstance(steps[0], dict) else {}
    return (
        step.get("tool") == SUPPORTED_TOOL
        and isinstance(step.get("doctype"), str)
        and bool((step.get("doctype") or "").strip())
    )


def _log_unsupported_plan(plan: dict[str, Any], *, request_id: str, user: str) -> None:
    doctype_name = ",".join(
        str(step.get("doctype")).strip()
        for step in (plan.get("steps") or [])
        if isinstance(step, dict) and str(step.get("doctype") or "").strip()
    )
    log_tool_call(
        "hybrid.supported_plan_check",
        "Rejected",
        tool_name=SUPPORTED_TOOL,
        doctype_name=doctype_name,
        user=user,
        request_id=request_id,
        intent=plan.get("intent") or INTENT_STRUCTURED_QUERY,
        error_message="Hybrid mode only supports a single validated get_list step.",
        plan=plan,
        details={"step_count": len(plan.get("steps") or [])},
    )


def _request_id_for_message(message_id: str | None) -> str:
    candidate = (message_id or "").strip()
    if candidate:
        return f"hybrid-{candidate}"
    return f"hybrid-{frappe.generate_hash(length=10)}"


def _normalize_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _log():
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger
