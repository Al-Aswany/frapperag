from __future__ import annotations

import json
import re
from typing import Any

import frappe
from frappe.utils import cint

from frapperag.assistant.schema_catalog import load_schema_catalog
from frapperag.assistant.schema_policy import (
    build_safe_schema_slice,
    classify_field_safety,
    load_allowed_doctype_policies,
)


INTENT_STRUCTURED_QUERY = "structured_query"
INTENT_ERPNEXT_HELP = "erpnext_help"
INTENT_DOCUMENT_RAG = "document_rag"
INTENT_REPORT_QUERY = "report_query"
INTENT_MIXED_QUERY = "mixed_query"
INTENT_OUT_OF_SCOPE = "out_of_scope"
INTENT_UNCLEAR = "unclear"

VALID_INTENTS = (
    INTENT_STRUCTURED_QUERY,
    INTENT_ERPNEXT_HELP,
    INTENT_DOCUMENT_RAG,
    INTENT_REPORT_QUERY,
    INTENT_MIXED_QUERY,
    INTENT_OUT_OF_SCOPE,
    INTENT_UNCLEAR,
)

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "any",
    "are",
    "at",
    "be",
    "by",
    "for",
    "from",
    "have",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "please",
    "show",
    "tell",
    "the",
    "to",
    "us",
    "we",
    "what",
    "which",
    "who",
    "with",
}
_STRUCTURED_KEYWORDS = {
    "amount",
    "average",
    "balance",
    "compare",
    "count",
    "counts",
    "declining",
    "grand",
    "howmany",
    "latest",
    "list",
    "open",
    "outstanding",
    "pairs",
    "ratio",
    "overdue",
    "recent",
    "revenue",
    "sold",
    "status",
    "sum",
    "total",
    "totals",
    "top",
    "trend",
    "unpaid",
    "value",
    "اجمالي",
    "اعلى",
    "الأعلى",
    "الشهر",
    "فاتورة",
    "فواتير",
    "عميل",
    "عملاء",
    "قيمة",
    "مبيعات",
    "مشتريات",
    "مقارنة",
    "مجموع",
    "متوسط",
    "مستحق",
}
_REPORT_KEYWORDS = {
    "analysis",
    "chart",
    "dashboard",
    "kpi",
    "report",
    "reports",
    "summary",
    "summaries",
}
_DOCUMENT_KEYWORDS = {
    "agreement",
    "attachment",
    "attachments",
    "contract",
    "document",
    "documents",
    "file",
    "files",
    "handbook",
    "manual",
    "pdf",
    "policy",
    "policies",
    "procedure",
    "procedures",
    "sop",
    "wiki",
}
_HELP_KEYWORDS = {
    "configure",
    "create",
    "disable",
    "enable",
    "explain",
    "setup",
    "step",
    "steps",
    "submit",
    "workflow",
    "كيف",
    "خطوات",
    "شرح",
}
_OUT_OF_SCOPE_KEYWORDS = {
    "capital",
    "joke",
    "lyrics",
    "movie",
    "poem",
    "recipe",
    "translate",
    "weather",
}
_ERP_TERMS = {
    "doctype",
    "erpnext",
    "frappe",
    "invoice",
    "item",
    "purchase",
    "quotation",
    "report",
    "sales",
    "stock",
    "supplier",
    "workflow",
    "فاتورة",
    "فواتير",
    "مبيعات",
    "مشتريات",
    "مخزون",
}
_DATA_VERBS = {
    "count",
    "counts",
    "find",
    "list",
    "show",
    "summarize",
    "total",
    "اعرض",
    "اظهر",
    "احسب",
    "لخص",
}
_ARABIC_ANALYTICS_TERMS = {
    "اعرض",
    "الشهر",
    "فاتورة",
    "فواتير",
    "قيمة",
    "مبيعات",
    "مقارنة",
    "مجموع",
    "متوسط",
}
_MIX_CONNECTORS = {"and", "also", "along", "plus", "together", "with"}
_QUESTION_TOKEN_RE = re.compile(r"[a-z0-9\u0600-\u06FF]+")
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_HELP_PREFIXES = (
    "how do i",
    "how can i",
    "how to",
    "where do i",
    "where can i",
    "what is the workflow",
    "why can't i",
    "why cant i",
    "كيف",
    "كيف يمكنني",
)


def _log():
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger


def route_question(
    question: str,
    *,
    use_llm_fallback: bool = False,
    settings: Any | None = None,
    max_candidate_doctypes: int = 5,
    max_candidate_reports: int = 5,
) -> dict[str, Any]:
    question = (question or "").strip()
    settings = settings or frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    catalog = load_schema_catalog() or {}
    policies = load_allowed_doctype_policies(settings=settings)
    reports = _load_allowed_reports(settings=settings, catalog=catalog)
    context = _build_routing_context(
        question,
        catalog=catalog,
        policies=policies,
        reports=reports,
        max_candidate_doctypes=max_candidate_doctypes,
        max_candidate_reports=max_candidate_reports,
    )

    heuristic_route = _route_by_heuristic(question, context)
    if not use_llm_fallback or not _should_try_llm_fallback(heuristic_route):
        return heuristic_route

    try:
        llm_route = _route_by_llm(question, heuristic_route, context, settings=settings)
    except Exception:
        _log().exception("[ROUTER_LLM_FALLBACK_FAILED] question=%s", question)
        return heuristic_route

    return llm_route or heuristic_route


def log_shadow_route_decision(
    route: dict[str, Any],
    *,
    question: str,
    message_id: str | None = None,
    session_id: str | None = None,
    user: str | None = None,
) -> None:
    payload = {
        "event": "router_shadow_decision",
        "message_id": message_id,
        "session_id": session_id,
        "user": user,
        "question": question,
        "selected_intent": route.get("selected_intent"),
        "confidence": route.get("confidence"),
        "reason": route.get("reason"),
        "candidate_doctypes": route.get("candidate_doctypes") or [],
        "candidate_reports": route.get("candidate_reports") or [],
        "router_source": route.get("router_source"),
        "shadow_only_status": route.get("shadow_only_status", "shadow_only"),
    }
    _log().info("[ROUTER_SHADOW] %s", json.dumps(payload, sort_keys=True, default=str))


def debug_route_question(
    question: str,
    use_llm_fallback: int = 0,
    log_decision: int = 1,
) -> dict[str, Any]:
    route = route_question(question, use_llm_fallback=bool(cint(use_llm_fallback)))
    if cint(log_decision):
        log_shadow_route_decision(route, question=question, user=frappe.session.user)
    return route


def _build_routing_context(
    question: str,
    *,
    catalog: dict[str, Any],
    policies: dict[str, dict[str, Any]],
    reports: list[dict[str, Any]],
    max_candidate_doctypes: int,
    max_candidate_reports: int,
) -> dict[str, Any]:
    normalized_question = " ".join((question or "").strip().lower().split())
    tokens = _tokenize(normalized_question)
    token_set = set(tokens)
    enabled_doctypes = [
        {
            "name": entry.get("name"),
            "module": (entry.get("module") or "").strip(),
            "fields": entry.get("fields") or [],
        }
        for entry in (catalog.get("doctypes") or [])
        if entry.get("name") in policies and policies[entry.get("name")]["enabled"]
    ]

    candidate_doctypes = _rank_candidate_doctypes(
        normalized_question,
        token_set,
        enabled_doctypes,
        max_candidates=max_candidate_doctypes,
    )
    candidate_reports = _rank_candidate_reports(
        normalized_question,
        token_set,
        reports,
        max_candidates=max_candidate_reports,
    )

    return {
        "question": question,
        "normalized_question": normalized_question,
        "tokens": tokens,
        "token_set": token_set,
        "has_erp_signal": bool(
            candidate_doctypes
            or candidate_reports
            or (token_set & _ERP_TERMS)
        ),
        "candidate_doctype_rows": candidate_doctypes,
        "candidate_report_rows": candidate_reports,
        "candidate_doctypes": [row["name"] for row in candidate_doctypes],
        "candidate_reports": [row["name"] for row in candidate_reports],
    }


def _route_by_heuristic(question: str, context: dict[str, Any]) -> dict[str, Any]:
    normalized_question = context["normalized_question"]
    token_set = context["token_set"]
    candidate_doctypes = context["candidate_doctypes"]
    candidate_reports = context["candidate_reports"]

    if len(normalized_question) < 3 or len(context["tokens"]) <= 1:
        return _make_route(
            INTENT_UNCLEAR,
            0.22,
            "Question is too short for reliable routing.",
            context,
        )

    if _is_greeting(normalized_question):
        return _make_route(
            INTENT_UNCLEAR,
            0.31,
            "Greeting without a routable business question.",
            context,
        )

    structured_score, structured_reason = _structured_signal(normalized_question, token_set, candidate_doctypes)
    report_score, report_reason = _report_signal(normalized_question, token_set, candidate_reports)
    document_score, document_reason = _document_signal(normalized_question, token_set)
    help_score, help_reason = _help_signal(normalized_question, token_set, candidate_doctypes)
    out_of_scope_score, out_of_scope_reason = _out_of_scope_signal(normalized_question, token_set, context["has_erp_signal"])
    mixed_signal = _is_mixed_question(
        normalized_question,
        structured_score=structured_score,
        document_score=document_score,
        help_score=help_score,
    )

    if out_of_scope_score >= 0.85:
        return _make_route(INTENT_OUT_OF_SCOPE, out_of_scope_score, out_of_scope_reason, context)

    if report_score >= 0.84:
        return _make_route(INTENT_REPORT_QUERY, report_score, report_reason, context)

    if mixed_signal:
        return _make_route(
            INTENT_MIXED_QUERY,
            max(structured_score, document_score, help_score, 0.77),
            _join_reason(structured_reason, document_reason or help_reason),
            context,
        )

    if document_score >= 0.72 and structured_score < 0.66 and report_score < 0.70:
        return _make_route(INTENT_DOCUMENT_RAG, document_score, document_reason, context)

    if help_score >= 0.78 and structured_score < 0.68:
        return _make_route(INTENT_ERPNEXT_HELP, help_score, help_reason, context)

    if structured_score >= 0.65:
        return _make_route(INTENT_STRUCTURED_QUERY, structured_score, structured_reason, context)

    if report_score >= 0.40:
        return _make_route(INTENT_REPORT_QUERY, report_score, report_reason, context)

    if help_score >= 0.62:
        return _make_route(INTENT_ERPNEXT_HELP, help_score, help_reason, context)

    if document_score >= 0.55:
        return _make_route(INTENT_DOCUMENT_RAG, document_score, document_reason, context)

    if candidate_doctypes:
        return _make_route(
            INTENT_STRUCTURED_QUERY,
            0.58,
            f"Matched ERP DocTypes {candidate_doctypes[:3]} but the request is underspecified.",
            context,
        )

    return _make_route(
        INTENT_UNCLEAR,
        0.39,
        "No strong structured, report, document, or ERP-help signal was found.",
        context,
    )


def _should_try_llm_fallback(route: dict[str, Any]) -> bool:
    return route["selected_intent"] in {INTENT_UNCLEAR, INTENT_MIXED_QUERY} or route["confidence"] < 0.60


def _route_by_llm(
    question: str,
    heuristic_route: dict[str, Any],
    context: dict[str, Any],
    *,
    settings: Any,
) -> dict[str, Any] | None:
    api_key = settings.get_password("gemini_api_key")
    if not api_key:
        return None

    schema_snippets = _build_safe_schema_snippets(context["candidate_doctypes"][:3])
    report_snippets = _build_report_snippets(context["candidate_report_rows"][:5])
    prompt_payload = {
        "question": question,
        "heuristic_route": {
            "selected_intent": heuristic_route["selected_intent"],
            "confidence": heuristic_route["confidence"],
            "reason": heuristic_route["reason"],
        },
        "allowed_intents": list(VALID_INTENTS),
        "candidate_doctypes": context["candidate_doctypes"][:5],
        "candidate_reports": context["candidate_reports"][:5],
        "safe_schema_snippets": schema_snippets,
        "report_snippets": report_snippets,
    }

    messages = [
        {
            "role": "user",
            "parts": [
                "You classify ERP assistant questions for shadow intent routing only. "
                "Pick exactly one intent from this list: "
                f"{', '.join(VALID_INTENTS)}. "
                "Use only the provided question, candidate report names, and safe schema snippets. "
                "Never assume access to schema or reports not shown. "
                "Return JSON only with keys intent, confidence, reason, candidate_doctypes, candidate_reports."
            ],
        },
        {"role": "model", "parts": ["Understood. I will return JSON only."]},
        {"role": "user", "parts": [json.dumps(prompt_payload, sort_keys=True, default=str)]},
    ]

    from frapperag.rag.sidecar_client import chat

    response = chat(messages=messages, api_key=api_key, tools=None)
    parsed = _parse_llm_route_response(
        response.get("text") or "",
        allowed_doctypes={entry["name"] for entry in schema_snippets},
        allowed_reports={entry["name"] for entry in report_snippets},
    )
    if not parsed:
        return None

    return {
        "selected_intent": parsed["selected_intent"],
        "confidence": parsed["confidence"],
        "reason": parsed["reason"],
        "candidate_doctypes": parsed["candidate_doctypes"],
        "candidate_reports": parsed["candidate_reports"],
        "router_source": "llm",
        "shadow_only_status": "shadow_only",
    }


def _parse_llm_route_response(
    raw_text: str,
    *,
    allowed_doctypes: set[str],
    allowed_reports: set[str],
) -> dict[str, Any] | None:
    if not raw_text:
        return None

    candidate = raw_text.strip()
    if not candidate.startswith("{"):
        match = _JSON_OBJECT_RE.search(candidate)
        if not match:
            return None
        candidate = match.group(0)

    try:
        payload = json.loads(candidate)
    except Exception:
        return None

    intent = (payload.get("intent") or "").strip()
    if intent not in VALID_INTENTS:
        return None

    confidence = _clamp_confidence(payload.get("confidence"), default=0.55)
    reason = str(payload.get("reason") or "LLM fallback selected this route.").strip()[:240]
    candidate_doctypes = [
        name for name in (payload.get("candidate_doctypes") or [])
        if isinstance(name, str) and name in allowed_doctypes
    ]
    candidate_reports = [
        name for name in (payload.get("candidate_reports") or [])
        if isinstance(name, str) and name in allowed_reports
    ]
    return {
        "selected_intent": intent,
        "confidence": confidence,
        "reason": reason,
        "candidate_doctypes": candidate_doctypes,
        "candidate_reports": candidate_reports,
    }


def _build_safe_schema_snippets(doctype_names: list[str]) -> list[dict[str, Any]]:
    if not doctype_names:
        return []

    safe_slice = build_safe_schema_slice(doctype_names)
    snippets: list[dict[str, Any]] = []
    for entry in safe_slice.get("doctypes") or []:
        fields = []
        for field in (entry.get("fields") or [])[:8]:
            fields.append(
                {
                    "fieldname": field.get("fieldname"),
                    "label": field.get("label"),
                    "fieldtype": field.get("fieldtype"),
                }
            )

        snippets.append(
            {
                "name": entry.get("name"),
                "module": entry.get("module"),
                "links": (entry.get("links") or [])[:5],
                "query_policy": {
                    "allow_get_list": entry.get("query_policy", {}).get("allow_get_list"),
                    "allow_query_builder": entry.get("query_policy", {}).get("allow_query_builder"),
                    "default_date_field": entry.get("query_policy", {}).get("default_date_field"),
                    "default_title_field": entry.get("query_policy", {}).get("default_title_field"),
                    "large_table_requires_date_filter": entry.get("query_policy", {}).get("large_table_requires_date_filter"),
                },
                "fields": fields,
            }
        )

    return snippets


def _build_report_snippets(candidate_report_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": row["name"],
            "description": row.get("description") or "",
            "ref_doctype": row.get("ref_doctype") or "",
            "report_type": row.get("report_type") or "",
        }
        for row in candidate_report_rows
    ]


def _rank_candidate_doctypes(
    normalized_question: str,
    token_set: set[str],
    doctypes: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for entry in doctypes:
        name = entry["name"]
        name_tokens = _expand_tokens(_tokenize(name))
        module_tokens = _expand_tokens(_tokenize(entry.get("module") or ""))
        score = 0.0

        if name.lower() in normalized_question:
            score += 0.70

        overlap = token_set & name_tokens
        if overlap:
            score += min(0.55, len(overlap) * 0.22)

        module_overlap = token_set & module_tokens
        if module_overlap:
            score += min(0.12, len(module_overlap) * 0.06)

        safe_field_hits = 0
        for field in entry.get("fields") or []:
            if not classify_field_safety(field)["safe_for_ai"]:
                continue
            field_tokens = _expand_tokens(
                _tokenize(field.get("label") or "")
                + _tokenize(field.get("fieldname") or "")
            )
            if token_set & field_tokens:
                safe_field_hits += 1
                if safe_field_hits >= 3:
                    break
        if safe_field_hits:
            score += min(0.18, safe_field_hits * 0.06)

        if score >= 0.18:
            scored.append({"name": name, "score": round(score, 2)})

    return sorted(scored, key=lambda row: (-row["score"], row["name"]))[:max_candidates]


def _rank_candidate_reports(
    normalized_question: str,
    token_set: set[str],
    reports: list[dict[str, Any]],
    *,
    max_candidates: int,
) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in reports:
        name = row["name"]
        name_tokens = _expand_tokens(_tokenize(name))
        description_tokens = _expand_tokens(_tokenize(row.get("description") or ""))
        ref_doctype_tokens = _expand_tokens(_tokenize(row.get("ref_doctype") or ""))
        score = 0.0

        if name.lower() in normalized_question:
            score += 0.75

        overlap = token_set & name_tokens
        if overlap:
            score += min(0.55, len(overlap) * 0.20)

        description_overlap = token_set & description_tokens
        if description_overlap:
            score += min(0.18, len(description_overlap) * 0.06)

        ref_overlap = token_set & ref_doctype_tokens
        if ref_overlap:
            score += min(0.14, len(ref_overlap) * 0.07)

        if "report" in token_set or "dashboard" in token_set:
            score += 0.10

        if score >= 0.20:
            scored.append(
                {
                    "name": name,
                    "score": round(score, 2),
                    "description": row.get("description") or "",
                    "ref_doctype": row.get("ref_doctype") or "",
                    "report_type": row.get("report_type") or "",
                }
            )

    return sorted(scored, key=lambda row: (-row["score"], row["name"]))[:max_candidates]


def _structured_signal(
    normalized_question: str,
    token_set: set[str],
    candidate_doctypes: list[str],
) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []
    matched = sorted(token_set & _STRUCTURED_KEYWORDS)
    if matched:
        score += min(0.44, len(matched) * 0.14)
        reasons.append(f"matched structured terms {matched[:4]}")
    arabic_matches = sorted(token_set & _ARABIC_ANALYTICS_TERMS)
    if arabic_matches:
        score += min(0.26, len(arabic_matches) * 0.09)
        reasons.append(f"matched Arabic ERP terms {arabic_matches[:4]}")

    if candidate_doctypes:
        score += 0.34
        reasons.append(f"matched DocTypes {candidate_doctypes[:3]}")

    if re.search(r"\b(count|how many|total|sum|list|show|latest|overdue|status)\b", normalized_question):
        score += 0.20
    if any(term in normalized_question for term in ("اعرض", "اظهر", "اجمالي", "إجمالي", "مبيعات", "فواتير")):
        score += 0.18

    if re.search(r"\b(today|yesterday|week|month|quarter|year|date)\b", normalized_question):
        score += 0.06
    if any(term in normalized_question for term in ("اليوم", "السنة", "الشهر", "شهر", "تاريخ")):
        score += 0.06

    analytics_phrase_patterns = (
        (r"\bby month\b", "by month"),
        (r"\btrend\b", "trend"),
        (r"\bcompare\b", "compare"),
        (r"\bratio\b", "ratio"),
        (r"\bpairs?\b", "pairs"),
        (r"\bsales by\b", "sales by"),
        (r"\bunpaid\b", "unpaid"),
        (r"\bmost sold\b", "most sold"),
        (r"\bdeclining\b", "declining"),
        (r"حسب الشهر", "حسب الشهر"),
        (r"مبيعات حسب", "مبيعات حسب"),
        (r"مقارنة", "مقارنة"),
        (r"متوسط", "متوسط"),
        (r"غير مدفوع", "غير مدفوع"),
    )
    matched_phrases = [
        label
        for pattern, label in analytics_phrase_patterns
        if re.search(pattern, normalized_question)
    ]
    if matched_phrases:
        score += min(0.24, len(matched_phrases) * 0.06)
        reasons.append(f"matched analytics phrasing {matched_phrases[:4]}")

    return min(score, 0.96), _finalize_reason(reasons, "Structured ERP data request.")


def _report_signal(
    normalized_question: str,
    token_set: set[str],
    candidate_reports: list[str],
) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []
    matched = sorted(token_set & _REPORT_KEYWORDS)
    if matched:
        score += min(0.35, len(matched) * 0.15)
        reasons.append(f"matched report terms {matched[:4]}")

    if candidate_reports:
        score += 0.42
        reasons.append(f"matched reports {candidate_reports[:3]}")

    if re.search(r"\b(run|open|show|give|generate)\b", normalized_question) and ("report" in token_set or candidate_reports):
        score += 0.28

    return min(score, 0.97), _finalize_reason(reasons, "Explicit report-oriented request.")


def _document_signal(normalized_question: str, token_set: set[str]) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []
    matched = sorted(token_set & _DOCUMENT_KEYWORDS)
    if matched:
        score += min(0.62, len(matched) * 0.20)
        reasons.append(f"matched document terms {matched[:4]}")

    if re.search(r"\b(summarize|summarise|find in|search|lookup)\b", normalized_question) and matched:
        score += 0.16

    return min(score, 0.95), _finalize_reason(reasons, "Unstructured document-style request.")


def _help_signal(
    normalized_question: str,
    token_set: set[str],
    candidate_doctypes: list[str],
) -> tuple[float, str]:
    score = 0.0
    reasons: list[str] = []
    if normalized_question.startswith(_HELP_PREFIXES):
        score += 0.50
        reasons.append("matched ERP how-to phrasing")

    matched = sorted(token_set & _HELP_KEYWORDS)
    if matched:
        score += min(0.28, len(matched) * 0.12)
        reasons.append(f"matched help terms {matched[:4]}")

    if candidate_doctypes and any(term in normalized_question for term in ("workflow", "create", "submit", "configure")):
        score += 0.15
        reasons.append(f"matched DocTypes {candidate_doctypes[:3]}")

    return min(score, 0.94), _finalize_reason(reasons, "ERPNext help or workflow request.")


def _out_of_scope_signal(
    normalized_question: str,
    token_set: set[str],
    has_erp_signal: bool,
) -> tuple[float, str]:
    matched = sorted(token_set & _OUT_OF_SCOPE_KEYWORDS)
    if not matched:
        return 0.0, "No out-of-scope signal."

    score = min(0.92, len(matched) * 0.35)
    if has_erp_signal:
        score -= 0.30
    return max(score, 0.0), f"Matched out-of-scope terms {matched[:4]}."


def _is_mixed_question(
    normalized_question: str,
    *,
    structured_score: float,
    document_score: float,
    help_score: float,
) -> bool:
    has_connector = bool(set(_tokenize(normalized_question)) & _MIX_CONNECTORS)
    has_dual_signal = structured_score >= 0.65 and (document_score >= 0.55 or help_score >= 0.72)
    return has_dual_signal and has_connector


def _load_allowed_reports(settings: Any, catalog: dict[str, Any]) -> list[dict[str, Any]]:
    catalog_reports = {
        entry.get("name"): entry
        for entry in (catalog.get("reports") or [])
        if entry.get("name")
    }
    reports: list[dict[str, Any]] = []
    for row in (getattr(settings, "allowed_reports", None) or []):
        report_name = (getattr(row, "report", None) or "").strip()
        if not report_name:
            continue
        catalog_entry = catalog_reports.get(report_name, {})
        reports.append(
            {
                "name": report_name,
                "description": (getattr(row, "description", None) or "").strip(),
                "ref_doctype": (catalog_entry.get("ref_doctype") or "").strip(),
                "report_type": (catalog_entry.get("report_type") or "").strip(),
            }
        )
    return reports


def _make_route(intent: str, confidence: float, reason: str, context: dict[str, Any]) -> dict[str, Any]:
    return {
        "selected_intent": intent,
        "confidence": round(float(confidence), 2),
        "reason": reason,
        "candidate_doctypes": context["candidate_doctypes"][:5],
        "candidate_reports": context["candidate_reports"][:5],
        "router_source": "heuristic",
        "shadow_only_status": "shadow_only",
    }


def _tokenize(value: str) -> list[str]:
    return [token for token in _QUESTION_TOKEN_RE.findall((value or "").lower()) if token]


def _expand_tokens(tokens: list[str]) -> set[str]:
    expanded: set[str] = set()
    for token in tokens:
        if token in _STOP_WORDS:
            continue
        expanded.add(token)
        collapsed = token.replace("_", "")
        if collapsed:
            expanded.add(collapsed)
        if token.endswith("ies") and len(token) > 4:
            expanded.add(token[:-3] + "y")
        elif token.endswith("s") and len(token) > 3:
            expanded.add(token[:-1])
    return expanded


def _is_greeting(normalized_question: str) -> bool:
    return normalized_question in {"hi", "hello", "hey", "thanks", "thank you"}


def _finalize_reason(reasons: list[str], fallback: str) -> str:
    if reasons:
        return "; ".join(reasons)
    return fallback


def _join_reason(primary: str, secondary: str) -> str:
    if primary and secondary:
        return f"{primary}; {secondary}"
    return primary or secondary or "Mixed structured and context-help request."


def _clamp_confidence(value: Any, *, default: float) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = default
    return round(min(1.0, max(0.0, confidence)), 2)
