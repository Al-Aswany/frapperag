from __future__ import annotations

import json
import time
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.analytics.analytics_executor import execute_validated_analytics_plan
from frapperag.assistant.analytics.analytics_plan_schema import (
    INTENT as INTENT_ANALYTICS_QUERY,
    SUPPORTED_ANALYSIS_TYPES,
    TOOL_NAME as ANALYTICS_TOOL_NAME,
)
from frapperag.assistant.analytics.analytics_validator import validate_analytics_plan
from frapperag.assistant.answer_composer import compose_analytics_answer, compose_structured_answer
from frapperag.assistant.executors.get_list_executor import execute_validated_plan
from frapperag.assistant.intent_router import INTENT_STRUCTURED_QUERY, route_question
from frapperag.assistant.plan_validator import validate_plan
from frapperag.assistant.planner import SUPPORTED_TOOL, plan_hybrid_query
from frapperag.assistant.tool_call_log import build_analytics_log_details, log_tool_call


HYBRID_MIN_ROUTE_CONFIDENCE = 0.65
HYBRID_MIN_ANALYTICS_PLAN_CONFIDENCE = 0.70


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

    request_id = _request_id_for_message(message_id)
    route = route or route_question(question, use_llm_fallback=False, settings=settings)
    if route.get("selected_intent") != INTENT_STRUCTURED_QUERY:
        _log_fallback(
            message_id=message_id,
            session_id=session_id,
            request_id=request_id,
            reason="non_structured",
            route=route,
            extra={"intent": route.get("selected_intent")},
        )
        return None

    if float(route.get("confidence") or 0.0) < HYBRID_MIN_ROUTE_CONFIDENCE:
        _log_fallback(
            message_id=message_id,
            session_id=session_id,
            request_id=request_id,
            reason="low_confidence",
            route=route,
            extra={"confidence": route.get("confidence")},
        )
        return None

    started = time.monotonic()

    try:
        plan = plan_hybrid_query(
            question,
            route,
            settings=settings,
            request_id=request_id,
        )
        if not plan:
            _log_fallback(
                message_id=message_id,
                session_id=session_id,
                request_id=request_id,
                reason="planner_rejected",
                route=route,
            )
            return None

        branch_result = _run_hybrid_plan(
            question=question,
            route=route,
            plan=plan,
            user=user,
            settings=settings,
            api_key=api_key,
            execute=True,
            compose=True,
        )
        if not int(branch_result.get("handled", 0)):
            _log_fallback(
                message_id=message_id,
                session_id=session_id,
                request_id=request_id,
                reason=branch_result.get("fallback_reason") or "unsupported_plan",
                route=route,
                plan=branch_result.get("validated_plan") or branch_result.get("plan"),
                extra={"error": branch_result.get("error") or ""},
            )
            return None

        execution_result = branch_result.get("execution_result") or {}
        _log().info(
            "[HYBRID_SUCCESS] message_id=%s session_id=%s request_id=%s rows=%s duration_ms=%s branch=%s",
            message_id,
            session_id,
            request_id,
            _row_count_for_execution_result(execution_result),
            _duration_ms(started),
            branch_result.get("hybrid_branch"),
        )
        return {
            "final_text": branch_result["final_text"],
            "citations": branch_result.get("citations") or [],
            "tokens_used": branch_result.get("tokens_used", 0),
            "request_id": request_id,
        }
    except Exception:
        _log_fallback(
            message_id=message_id,
            session_id=session_id,
            request_id=request_id,
            reason="hybrid_exception",
            route=route,
            extra={"error": "Unhandled hybrid runtime exception."},
        )
        _log().exception(
            "[HYBRID_FALLBACK_ERROR] message_id=%s session_id=%s request_id=%s",
            message_id,
            session_id,
            request_id,
        )
        return None


def debug_probe_hybrid_path(
    *,
    question: str,
    route_json: str | None = None,
    plan_json: str | None = None,
    execute: int = 1,
    override_assistant_mode: str = "hybrid",
) -> dict[str, Any]:
    route = _coerce_debug_json(route_json, default=None, expected_type=dict)
    plan = _coerce_debug_json(plan_json, default=None, expected_type=dict)
    assistant_mode = ((override_assistant_mode or "hybrid").strip() or "hybrid").lower()

    if assistant_mode != "hybrid":
        return {
            "handled": 0,
            "fallback_reason": "assistant_mode_not_hybrid",
            "assistant_mode": assistant_mode,
        }

    previous_override = getattr(frappe.flags, "frapperag_assistant_mode_override", None)
    frappe.flags.frapperag_assistant_mode_override = assistant_mode
    try:
        route = route or route_question(question, use_llm_fallback=False)
        if route.get("selected_intent") != INTENT_STRUCTURED_QUERY:
            return {
                "handled": 0,
                "fallback_reason": "non_structured",
                "assistant_mode": assistant_mode,
                "route": route,
            }

        if float(route.get("confidence") or 0.0) < HYBRID_MIN_ROUTE_CONFIDENCE:
            return {
                "handled": 0,
                "fallback_reason": "low_confidence",
                "assistant_mode": assistant_mode,
                "route": route,
            }

        if not plan:
            return {
                "handled": 0,
                "fallback_reason": "planner_required",
                "assistant_mode": assistant_mode,
                "route": route,
            }

        result = _run_hybrid_plan(
            question=question,
            route=route,
            plan=plan,
            user=frappe.session.user,
            settings=None,
            api_key=None,
            execute=bool(cint(execute)),
            compose=False,
        )
        if not int(result.get("handled", 0)) and plan:
            _log_fallback(
                message_id="",
                session_id="",
                request_id=str(plan.get("request_id") or "").strip(),
                reason=result.get("fallback_reason") or "unsupported_plan",
                route=route,
                plan=result.get("validated_plan") or result.get("plan") or plan,
                extra={"error": result.get("error") or ""},
            )
        result["assistant_mode"] = assistant_mode
        result["route"] = route
        return result
    finally:
        if previous_override:
            frappe.flags.frapperag_assistant_mode_override = previous_override
        elif hasattr(frappe.flags, "frapperag_assistant_mode_override"):
            delattr(frappe.flags, "frapperag_assistant_mode_override")


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


def build_analytics_result_citations(
    validated_plan: dict[str, Any],
    execution_result: dict[str, Any],
) -> list[dict[str, Any]]:
    columns = execution_result.get("columns") or []
    rows = execution_result.get("rows") or []
    return [
        {
            "type": "query_result",
            "doctype": execution_result.get("source_doctype") or validated_plan.get("source_doctype"),
            "columns": columns,
            "rows": [
                [_normalize_cell(row.get(column)) for column in columns]
                for row in rows
            ],
            "row_count": execution_result.get("row_count") or 0,
            "result_kind": "analytics",
            "analysis_type": execution_result.get("analysis_type") or validated_plan.get("analysis_type"),
            "source_doctype": execution_result.get("source_doctype") or validated_plan.get("source_doctype"),
            "final_answer_shape": validated_plan.get("final_answer_shape") or "",
            "details": execution_result.get("details") or {},
        }
    ]


def _run_hybrid_plan(
    *,
    question: str,
    route: dict[str, Any],
    plan: dict[str, Any],
    user: str,
    settings: Any | None,
    api_key: str | None,
    execute: bool,
    compose: bool,
) -> dict[str, Any]:
    branch_intent = (plan.get("intent") or "").strip()
    if branch_intent == INTENT_STRUCTURED_QUERY:
        return _run_structured_branch(
            question=question,
            route=route,
            plan=plan,
            settings=settings,
            api_key=api_key,
            execute=execute,
            compose=compose,
        )
    if branch_intent == INTENT_ANALYTICS_QUERY:
        return _run_analytics_branch(
            question=question,
            route=route,
            plan=plan,
            user=user,
            settings=settings,
            api_key=api_key,
            execute=execute,
            compose=compose,
        )
    return {
        "handled": 0,
        "fallback_reason": "unsupported_plan",
        "plan": plan,
        "error": f"Unsupported hybrid intent '{branch_intent or '<empty>'}'.",
    }


def _run_structured_branch(
    *,
    question: str,
    route: dict[str, Any],
    plan: dict[str, Any],
    settings: Any | None,
    api_key: str | None,
    execute: bool,
    compose: bool,
) -> dict[str, Any]:
    if not _is_supported_hybrid_structured_plan(plan):
        _log_unsupported_plan(plan, request_id=plan.get("request_id") or "", user=frappe.session.user, route=route)
        return {
            "handled": 0,
            "fallback_reason": "unsupported_plan",
            "plan": plan,
            "error": "Hybrid mode only supports a single validated get_list step.",
        }

    try:
        validated = validate_plan(plan, require_validated_flag=False, log_result=True)
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "validation_rejection",
            "plan": plan,
            "error": str(exc),
        }

    if not _is_supported_hybrid_structured_plan(validated):
        _log_unsupported_plan(validated, request_id=validated.get("request_id") or "", user=frappe.session.user, route=route)
        return {
            "handled": 0,
            "fallback_reason": "unsupported_plan",
            "validated_plan": validated,
            "error": "Validated hybrid plan is not supported.",
        }

    result: dict[str, Any] = {
        "handled": 1,
        "fallback_reason": "",
        "hybrid_branch": "structured",
        "validated_plan": validated,
    }
    if not execute:
        return result

    try:
        execution_result = execute_validated_plan(validated)
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "execution_failure",
            "validated_plan": validated,
            "error": str(exc),
        }
    result["execution_result"] = execution_result

    if not compose:
        return result

    try:
        composed = compose_structured_answer(
            question=question,
            route=route,
            validated_plan=validated,
            execution_result=execution_result,
            settings=settings,
            api_key=api_key,
        )
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "composer_failure",
            "validated_plan": validated,
            "execution_result": execution_result,
            "error": str(exc),
        }

    result.update(
        {
            "final_text": composed["text"],
            "citations": build_query_result_citations(validated, execution_result),
            "tokens_used": composed["tokens_used"],
        }
    )
    return result


def _run_analytics_branch(
    *,
    question: str,
    route: dict[str, Any],
    plan: dict[str, Any],
    user: str,
    settings: Any | None,
    api_key: str | None,
    execute: bool,
    compose: bool,
) -> dict[str, Any]:
    if not _is_supported_hybrid_analytics_plan(plan):
        _log_unsupported_plan(plan, request_id=plan.get("request_id") or "", user=user, route=route)
        return {
            "handled": 0,
            "fallback_reason": "unsupported_plan",
            "plan": plan,
            "error": "Hybrid mode only supports approved analytics DSL plans.",
        }

    if float(plan.get("confidence") or 1.0) < HYBRID_MIN_ANALYTICS_PLAN_CONFIDENCE:
        return {
            "handled": 0,
            "fallback_reason": "planner_low_confidence",
            "plan": plan,
            "error": f"Analytics planner confidence {plan.get('confidence')} is below threshold.",
        }

    try:
        validated = validate_analytics_plan(plan, require_validated_flag=False, log_result=True)
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "validation_rejection",
            "plan": plan,
            "error": str(exc),
        }

    if not _is_supported_hybrid_analytics_plan(validated):
        _log_unsupported_plan(validated, request_id=validated.get("request_id") or "", user=user, route=route)
        return {
            "handled": 0,
            "fallback_reason": "unsupported_plan",
            "validated_plan": validated,
            "error": "Validated analytics plan is not supported.",
        }

    result: dict[str, Any] = {
        "handled": 1,
        "fallback_reason": "",
        "hybrid_branch": "analytics",
        "validated_plan": validated,
    }
    if not execute:
        return result

    try:
        execution_result = execute_validated_analytics_plan(validated)
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "execution_failure",
            "validated_plan": validated,
            "error": str(exc),
        }
    result["execution_result"] = execution_result

    if execution_result.get("status") == "unsupported":
        return {
            "handled": 0,
            "fallback_reason": "execution_unsupported",
            "validated_plan": validated,
            "execution_result": execution_result,
            "error": execution_result.get("error") or "Analytics executor returned unsupported.",
        }

    if execution_result.get("status") not in {"success", "permission_denied"}:
        return {
            "handled": 0,
            "fallback_reason": "execution_failure",
            "validated_plan": validated,
            "execution_result": execution_result,
            "error": f"Unexpected analytics execution status '{execution_result.get('status')}'.",
        }

    if not compose:
        return result

    try:
        composed = compose_analytics_answer(
            question=question,
            route=route,
            validated_plan=validated,
            execution_result=execution_result,
            settings=settings,
            api_key=api_key,
        )
    except Exception as exc:
        return {
            "handled": 0,
            "fallback_reason": "composer_failure",
            "validated_plan": validated,
            "execution_result": execution_result,
            "error": str(exc),
        }

    result.update(
        {
            "final_text": composed["text"],
            "citations": build_analytics_result_citations(validated, execution_result),
            "tokens_used": composed["tokens_used"],
        }
    )
    return result


def _is_supported_hybrid_structured_plan(plan: dict[str, Any]) -> bool:
    if (plan.get("intent") or "").strip() != INTENT_STRUCTURED_QUERY:
        return False
    steps = plan.get("steps") or []
    if len(steps) != 1:
        return False
    step = steps[0] if isinstance(steps[0], dict) else {}
    return (
        step.get("tool") == SUPPORTED_TOOL
        and isinstance(step.get("doctype"), str)
        and bool((step.get("doctype") or "").strip())
    )


def _is_supported_hybrid_analytics_plan(plan: dict[str, Any]) -> bool:
    return (
        (plan.get("intent") or "").strip() == INTENT_ANALYTICS_QUERY
        and bool((plan.get("source_doctype") or "").strip())
        and (plan.get("analysis_type") or "").strip() in SUPPORTED_ANALYSIS_TYPES
    )


def _log_unsupported_plan(
    plan: dict[str, Any],
    *,
    request_id: str,
    user: str,
    route: dict[str, Any],
) -> None:
    hybrid_branch = "analytics" if (plan.get("intent") or "").strip() == INTENT_ANALYTICS_QUERY else "structured"
    doctype_name = _plan_doctype_name(plan)
    analysis_type = (plan.get("analysis_type") or "").strip()
    error_message = (
        "Hybrid mode only supports approved analytics DSL plans."
        if hybrid_branch == "analytics"
        else "Hybrid mode only supports a single validated get_list step."
    )
    log_tool_call(
        "hybrid.supported_plan_check",
        "Rejected",
        tool_name=ANALYTICS_TOOL_NAME if hybrid_branch == "analytics" else SUPPORTED_TOOL,
        doctype_name=doctype_name,
        user=user,
        request_id=request_id,
        intent=plan.get("intent") or INTENT_STRUCTURED_QUERY,
        error_message=error_message,
        plan=plan,
        details={
            **build_analytics_log_details(
                hybrid_branch=hybrid_branch,
                analysis_type=analysis_type,
                source_doctype=_plan_doctype_name(plan),
                planner_mode=(plan.get("planner_mode") or ""),
                route_confidence=float(route.get("confidence") or 0.0),
                candidate_doctypes=route.get("candidate_doctypes") or [],
                requested_limit=cint(plan.get("limit") or 0),
                effective_limit=cint(plan.get("limit") or 0),
                policy_limit=0,
                date_filter_required=0,
                date_filter_present=0,
                metrics=plan.get("metrics") or [],
                dimensions=plan.get("dimensions") or [],
                relationships=plan.get("relationships") or [],
                result_status="rejected",
                fallback_reason="unsupported_plan",
                error_code="unsupported_plan",
                error_class="ValidationError",
            ),
            "final_answer_shape": plan.get("final_answer_shape") or "",
        },
    )


def _plan_doctype_name(plan: dict[str, Any]) -> str:
    if (plan.get("intent") or "").strip() == INTENT_ANALYTICS_QUERY:
        return str(plan.get("source_doctype") or "").strip()
    return ",".join(
        str(step.get("doctype")).strip()
        for step in (plan.get("steps") or [])
        if isinstance(step, dict) and str(step.get("doctype") or "").strip()
    )


def _request_id_for_message(message_id: str | None) -> str:
    candidate = (message_id or "").strip()
    if candidate:
        return f"hybrid-{candidate}"
    return f"hybrid-{frappe.generate_hash(length=10)}"


def _row_count_for_execution_result(execution_result: dict[str, Any]) -> int:
    if "total_rows" in execution_result:
        return cint(execution_result.get("total_rows"))
    return cint(execution_result.get("row_count"))


def _log_fallback(
    *,
    message_id: str | None,
    session_id: str | None,
    request_id: str,
    reason: str,
    route: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    route = route or {}
    plan = plan or {}
    payload = {
        "message_id": message_id,
        "session_id": session_id,
        "reason": reason,
        "request_id": request_id,
    }
    if extra:
        payload.update(extra)
    doctype_name = _plan_doctype_name(plan) if plan else ",".join((route.get("candidate_doctypes") or [])[:3])
    hybrid_branch = (
        "analytics"
        if (plan.get("intent") or "").strip() == INTENT_ANALYTICS_QUERY
        else ("structured" if (plan.get("intent") or "").strip() == INTENT_STRUCTURED_QUERY else "router")
    )
    log_tool_call(
        "hybrid.fallback",
        "Rejected",
        tool_name=ANALYTICS_TOOL_NAME if hybrid_branch == "analytics" else SUPPORTED_TOOL,
        doctype_name=doctype_name,
        request_id=request_id,
        intent=(plan.get("intent") or route.get("selected_intent") or INTENT_STRUCTURED_QUERY),
        error_message=str((extra or {}).get("error") or reason),
        plan=plan or None,
        details={
            **build_analytics_log_details(
                hybrid_branch=hybrid_branch,
                analysis_type=(plan.get("analysis_type") or ""),
                source_doctype=(plan.get("source_doctype") or doctype_name),
                planner_mode=(plan.get("planner_mode") or ""),
                route_confidence=float(route.get("confidence") or 0.0),
                candidate_doctypes=route.get("candidate_doctypes") or [],
                requested_limit=cint(plan.get("limit") or 0),
                effective_limit=cint(plan.get("limit") or 0),
                policy_limit=0,
                date_filter_required=0,
                date_filter_present=0,
                metrics=plan.get("metrics") or [],
                dimensions=plan.get("dimensions") or [],
                relationships=plan.get("relationships") or [],
                result_status="fallback",
                fallback_reason=reason,
                error_code=reason,
                error_class="Fallback",
            ),
            "message_id": message_id or "",
            "session_id": session_id or "",
            **(extra or {}),
        },
    )
    _log().info("[HYBRID_FALLBACK] %s", json.dumps(payload, sort_keys=True, default=str))


def _normalize_cell(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _coerce_debug_json(raw_value: str | None, *, default: Any, expected_type: type) -> Any:
    if raw_value in (None, ""):
        return default
    try:
        value = json.loads(raw_value)
    except Exception as exc:
        frappe.throw(f"Invalid JSON input: {exc}", frappe.ValidationError)
    if not isinstance(value, expected_type):
        frappe.throw(
            f"Expected JSON {expected_type.__name__} input for debug probe.",
            frappe.ValidationError,
        )
    return value


def _log():
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger
