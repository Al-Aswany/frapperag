"""
FrappeRAG v2 Regression Test Runner
=====================================
Runs all 50 questions in tests/v2_regression_matrix.json through the live chat
API, auto-grades each response, and writes a full results file.

Grading dimensions per question:
  - Tool match      : expected_tool matches citation template / type
  - Citation count  : len(citations) <= max_citations
  - Citation types  : every expected_citation_types member appears
  - must_contain    : each phrase must appear in the response text (case-insensitive)
  - must_not_contain: none of these phrases may appear in the response text
  - decline_expected: when true, response must contain a decline/clarification keyword
                      (English + Arabic) and no data citations

Invocation (via bench execute):

    # Pre-flight check only:
    bench --site golive.site1 execute frapperag.tests.v2_runner.main \\
        --kwargs "{'check_only': True}"

    # Full run:
    bench --site golive.site1 execute frapperag.tests.v2_runner.main

    # Single category:
    bench --site golive.site1 execute frapperag.tests.v2_runner.main \\
        --kwargs "{'only_category': 'citation_hygiene'}"

Do NOT invoke directly with 'python3' — requires Frappe app context.
"""

import json
import os
import time
from datetime import datetime

import frappe

from frapperag.api.chat import (
    archive_session,
    create_session,
    get_messages,
    send_message,
)
from frapperag.rag.sidecar_client import health_check

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

POLL_INTERVAL = 3        # seconds between status polls
TIMEOUT = 120            # default timeout per question (overridden by entry)
RUNNER_VERSION = "2.0"

# Non-admin user that exercises permission filtering (category: permissions)
# Must exist, have the RAG User role, and NOT have read access to restricted_records.
RAG_TEST_USER = "rag-tester@golive.site1"

_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
MATRIX_PATH = os.path.join(_SPEC_DIR, "v2_regression_matrix.json")

CATEGORIES_ORDERED = [
    "english_lookup",
    "arabic_lookup",
    "aggregation",
    "top_n_report",
    "cross_doctype",
    "stock_query",
    "permissions",
    "out_of_scope",
    "vague_capability",
    "citation_hygiene",
]

CATEGORY_LABELS = {
    "english_lookup":   "English Lookups",
    "arabic_lookup":    "Arabic Lookups",
    "aggregation":      "Aggregations",
    "top_n_report":     "Top-N Reports",
    "cross_doctype":    "Cross-DocType",
    "stock_query":      "Stock Queries",
    "permissions":      "Permissions",
    "out_of_scope":     "Out-of-Scope",
    "vague_capability": "Vague / Capability",
    "citation_hygiene": "Citation Hygiene",
}

# Decline keywords accepted in both English and Arabic
_DECLINE_KEYWORDS_EN = [
    "cannot", "can't", "sorry", "not able", "unable",
    "not available", "outside", "beyond", "don't have access",
    "do not have access", "only have access", "not within",
    "not able to", "permission", "no permission", "access denied",
    "not found", "could not find", "don't have", "do not have",
    "not in a position", "not designed", "limited to",
    "clarify", "specify", "could you clarify", "could you specify",
    "what specifically", "which specific",
    "elaborate", "tell me what", "what do you need", "what would you like",
    "more details", "more specific", "be more specific",
]
_DECLINE_KEYWORDS_AR = [
    "لا أستطيع", "لا يمكنني", "عذراً", "آسف", "لم أتمكن",
    "لم يتم", "لا أملك", "خارج نطاق", "غير متاح", "ليس لدي",
    "لا يمكن", "لا أعرف", "يرجى التوضيح", "يرجى تحديد",
]
_ALL_DECLINE_KEYWORDS = _DECLINE_KEYWORDS_EN + _DECLINE_KEYWORDS_AR

# Citation types that represent data (tool results). Their presence when
# decline_expected=true is a grading failure.
_DATA_CITATION_TYPES = frozenset({"record_detail", "query_result", "report_result"})

# Maps expected_tool → citation type that the API will return
_TOOL_TO_CITATION_TYPE = {
    "record_lookup":         "record_detail",
    "aggregate_doctype":     "query_result",
    "top_selling_items":     "query_result",
    "best_selling_pairs":    "query_result",
    "low_stock_recent_sales": "query_result",
}

# Templates that emit query_result citations — used to verify citation.template
_QUERY_TEMPLATE_TOOLS = frozenset({
    "aggregate_doctype",
    "top_selling_items",
    "best_selling_pairs",
    "low_stock_recent_sales",
})


# ---------------------------------------------------------------------------
# Grader
# ---------------------------------------------------------------------------

def grade_question(entry: dict, response_text: str, citations: list) -> tuple[str, str]:
    """Return (grade, failure_reason) where grade is 'PASS' or 'FAIL'.

    Checks (in order):
      1. Decline assertion  — when decline_expected=True
      2. Tool / citation type match — when decline_expected=False and expected_tool set
      3. Citation count ceiling (max_citations)
      4. Expected citation types present
      5. must_contain phrases in response
      6. must_not_contain phrases absent from response
    """
    failures = []
    text_lower = response_text.lower()

    # ------------------------------------------------------------------
    # 1. Decline assertion
    # ------------------------------------------------------------------
    if entry.get("decline_expected"):
        found_decline = any(kw.lower() in text_lower for kw in _ALL_DECLINE_KEYWORDS)
        if not found_decline:
            failures.append(
                "decline_expected=true but no decline/clarification keyword found"
            )
        data_citations = [c for c in citations if c.get("type") in _DATA_CITATION_TYPES]
        if data_citations:
            leaked = [c.get("type") for c in data_citations]
            failures.append(
                f"decline_expected=true but data citations present: {leaked}"
            )

    # ------------------------------------------------------------------
    # 2. Tool / citation type match  (only when an answer is expected)
    # ------------------------------------------------------------------
    elif entry.get("expected_tool"):
        tool = entry["expected_tool"]
        expected_cit_type = _TOOL_TO_CITATION_TYPE.get(tool)

        if expected_cit_type == "record_detail":
            matched = any(c.get("type") == "record_detail" for c in citations)
            if not matched:
                failures.append(
                    f"expected record_detail citation for tool={tool!r}, none found"
                )
        elif expected_cit_type == "query_result" and tool in _QUERY_TEMPLATE_TOOLS:
            # Prefer template match; fall back to bare query_result
            template_match = any(
                c.get("type") == "query_result" and c.get("template") == tool
                for c in citations
            )
            bare_match = any(c.get("type") == "query_result" for c in citations)
            if not template_match and not bare_match:
                failures.append(
                    f"expected query_result citation (template={tool!r}), none found"
                )
        elif expected_cit_type:
            matched = any(c.get("type") == expected_cit_type for c in citations)
            if not matched:
                failures.append(
                    f"expected {expected_cit_type!r} citation for tool={tool!r}, none found"
                )

    # ------------------------------------------------------------------
    # 3. Citation count ceiling
    # ------------------------------------------------------------------
    max_cit = entry.get("max_citations", 50)
    if len(citations) > max_cit:
        failures.append(
            f"citation count {len(citations)} exceeds max_citations={max_cit}"
        )

    # ------------------------------------------------------------------
    # 4. Expected citation types present  (skip when decline_expected)
    # ------------------------------------------------------------------
    if not entry.get("decline_expected"):
        for expected_type in (entry.get("expected_citation_types") or []):
            actual_types = {c.get("type") for c in citations}
            if expected_type not in actual_types:
                failures.append(
                    f"expected_citation_type {expected_type!r} not found "
                    f"(actual: {sorted(actual_types)})"
                )

    # ------------------------------------------------------------------
    # 5. must_contain
    # ------------------------------------------------------------------
    for phrase in (entry.get("must_contain") or []):
        if phrase.lower() not in text_lower:
            failures.append(f"must_contain {phrase!r} not found in response")

    # ------------------------------------------------------------------
    # 6. must_not_contain
    # ------------------------------------------------------------------
    for phrase in (entry.get("must_not_contain") or []):
        if phrase.lower() in text_lower:
            failures.append(f"must_not_contain {phrase!r} was found in response")

    if failures:
        return "FAIL", "; ".join(failures)
    return "PASS", "all checks passed"


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def pre_flight_checks(matrix: list, only_category: str | None = None):
    """Run pre-flight checks and raise SystemExit(1) on any failure."""
    subset = matrix if not only_category else [
        q for q in matrix if q["category"] == only_category
    ]

    # 1. Sidecar reachable
    try:
        h = health_check()
        if not h.get("ok"):
            raise RuntimeError(h.get("detail") or str(h))
    except SystemExit:
        raise
    except Exception as e:
        print(f"FAIL Sidecar pre-flight FAILED: {e}")
        raise SystemExit(1)
    print("OK   Sidecar reachable")

    # 2. RAG test user exists
    if not frappe.db.exists("User", RAG_TEST_USER):
        print(f"FAIL RAG test user '{RAG_TEST_USER}' does not exist")
        raise SystemExit(1)
    print(f"OK   RAG test user exists ({RAG_TEST_USER})")

    # 3. Matrix: 50 questions (or filtered subset), all have required fields
    required_keys = {"id", "category", "question", "expected_tool",
                     "expected_citation_types", "max_citations",
                     "must_contain", "must_not_contain", "decline_expected"}
    missing_fields = []
    for q in subset:
        missing = required_keys - set(q.keys())
        if missing:
            missing_fields.append(f"{q.get('id','?')}: {missing}")
    if missing_fields:
        print(f"FAIL Questions missing required fields: {missing_fields}")
        raise SystemExit(1)
    if not only_category and len(matrix) != 50:
        print(f"WARN Matrix has {len(matrix)} questions, expected 50")
    print(f"OK   Matrix loads ({len(subset)} question(s) to run, all well-formed)")

    # 4. Restricted records unreadable by RAG test user
    original_user = frappe.session.user
    frappe.set_user(RAG_TEST_USER)
    try:
        for q in subset:
            for r in (q.get("restricted_records") or []):
                try:
                    can_read = frappe.has_permission(r["doctype"], "read", r["name"])
                except frappe.DoesNotExistError:
                    print(
                        f"WARN {r['doctype']}/{r['name']} not found in DB "
                        f"({q['id']}) — update restricted_records in matrix"
                    )
                    continue
                if can_read:
                    print(
                        f"FAIL RAG test user CAN read {r['doctype']}/{r['name']}"
                        f" — invalidates permission test {q['id']}"
                    )
                    raise SystemExit(1)
    finally:
        frappe.set_user(original_user)
    print("OK   All restricted records unreadable by RAG test user")

    print("OK   Pre-flight passed\n")


# ---------------------------------------------------------------------------
# Single-question driver
# ---------------------------------------------------------------------------

def _resolve_user(run_as: str) -> str:
    """Return the Frappe username to use for this question."""
    if run_as == "rag_user":
        return RAG_TEST_USER
    return "Administrator"


def _error_result(entry: dict, reason: str) -> dict:
    return {
        "id":               entry["id"],
        "category":         entry["category"],
        "question":         entry["question"],
        "run_as":           entry.get("run_as", "administrator"),
        "response":         "",
        "citations":        [],
        "citation_count":   0,
        "citation_types":   [],
        "elapsed_s":        0.0,
        "status":           "Failed",
        "pass":             False,
        "failure_reason":   reason,
    }


def run_question(session_id: str, entry: dict) -> dict:
    """Drive a single question through the chat API and return a graded result dict."""
    run_as_user = _resolve_user(entry.get("run_as", "administrator"))
    timeout = entry.get("timeout_seconds", TIMEOUT)
    original_user = frappe.session.user

    frappe.set_user(run_as_user)
    try:
        messages_before = get_messages(session_id=session_id)["messages"]
        assistant_count_before = sum(
            1 for m in messages_before if m["role"] == "assistant"
        )
        t_start = time.time()

        try:
            resp = send_message(session_id=session_id, content=entry["question"])
        except frappe.PermissionError as e:
            return _error_result(entry, f"PermissionError on send: {e}")
        except frappe.ValidationError:
            # A previous question may still be processing — wait and retry once
            time.sleep(5)
            try:
                resp = send_message(session_id=session_id, content=entry["question"])
            except Exception as e2:
                return _error_result(entry, f"ValidationError on send (retry): {e2}")

        while True:
            elapsed = time.time() - t_start
            if elapsed > timeout:
                return _error_result(
                    entry, f"Timeout after {elapsed:.1f}s (limit={timeout}s)"
                )

            frappe.db.commit()
            messages = get_messages(session_id=session_id)["messages"]
            assistant_msgs = [m for m in messages if m["role"] == "assistant"]

            # Detect worker failure: the user message was set to Failed without
            # producing an assistant reply (e.g. sidecar 500, uncaught exception).
            user_msgs = [m for m in messages if m["role"] == "user"]
            if user_msgs and user_msgs[-1].get("status") == "Failed":
                elapsed = round(time.time() - t_start, 2)
                reason = user_msgs[-1].get("failure_reason") or "Worker failed without assistant reply"
                return _error_result(entry, f"Worker error after {elapsed}s: {reason}")

            if len(assistant_msgs) > assistant_count_before:
                latest = assistant_msgs[-1]
                if latest.get("status") in ("Completed", "Failed"):
                    elapsed = round(time.time() - t_start, 2)
                    response_text = latest.get("content") or ""

                    citations = []
                    raw_cit = latest.get("citations")
                    if raw_cit:
                        if isinstance(raw_cit, str):
                            try:
                                citations = json.loads(raw_cit)
                            except Exception:
                                citations = [{"raw": raw_cit}]
                        elif isinstance(raw_cit, list):
                            citations = raw_cit

                    grade, reason = grade_question(entry, response_text, citations)
                    return {
                        "id":               entry["id"],
                        "category":         entry["category"],
                        "question":         entry["question"],
                        "run_as":           entry.get("run_as", "administrator"),
                        "response":         response_text[:800],
                        "citations":        citations,
                        "citation_count":   len(citations),
                        "citation_types":   list({c.get("type") for c in citations if c.get("type")}),
                        "elapsed_s":        elapsed,
                        "status":           latest.get("status", ""),
                        "pass":             grade == "PASS",
                        "failure_reason":   "" if grade == "PASS" else reason,
                    }

            time.sleep(POLL_INTERVAL)

    except SystemExit:
        raise
    except Exception as e:
        return _error_result(entry, f"Unexpected: {type(e).__name__}: {e}")
    finally:
        frappe.set_user(original_user)


# ---------------------------------------------------------------------------
# Scorecard printer
# ---------------------------------------------------------------------------

def print_scorecard(results: list):
    """Print per-category pass rates and an overall total."""
    print()
    print("=" * 68)
    print("  FrappeRAG v2 Regression — Scorecard")
    print("=" * 68)
    header = f"  {'Category':<22} {'PASS':>5} {'FAIL':>5} {'SKIP':>5}   {'Rate':>6}"
    print(header)
    print("  " + "-" * 64)

    overall_pass = overall_total = 0

    for cat in CATEGORIES_ORDERED:
        label = CATEGORY_LABELS.get(cat, cat)
        rows = [r for r in results if r["category"] == cat]
        passes = sum(1 for r in rows if r["pass"] is True)
        fails  = sum(1 for r in rows if r["pass"] is False and r["status"] != "")
        skips  = sum(1 for r in rows if r["status"] == "")
        total  = len(rows)
        overall_pass  += passes
        overall_total += total
        rate = int(round(passes / total * 100)) if total else 0
        print(f"  {label:<22} {passes:>5} {fails:>5} {skips:>5}   {rate:>5}%")

    print("  " + "-" * 64)
    overall_rate = int(round(overall_pass / overall_total * 100)) if overall_total else 0
    print(f"  {'TOTAL':<22} {overall_pass:>5} {overall_total - overall_pass:>5}"
          f"         {overall_rate:>5}%")
    print()

    # Failures detail
    failed = [r for r in results if not r["pass"] and r["failure_reason"]]
    if failed:
        print("  Failed questions:")
        for r in failed:
            print(f"    [{r['id']}] {r['failure_reason']}")
        print()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(check_only: bool = False, only_category: str | None = None):
    """
    Args:
        check_only:     Run pre-flight checks only, then exit.
        only_category:  Run only questions in this category slug
                        (e.g. 'citation_hygiene').
    """
    with open(MATRIX_PATH, encoding="utf-8") as f:
        matrix = json.load(f)

    subset = matrix if not only_category else [
        q for q in matrix if q["category"] == only_category
    ]
    if only_category and not subset:
        print(f"No questions found for category {only_category!r}")
        raise SystemExit(1)

    pre_flight_checks(matrix, only_category=only_category)

    if check_only:
        print("Pre-flight passed. Ready to run.")
        return

    all_results = []
    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    results_path = os.path.join(_SPEC_DIR, f"v2_results_{timestamp}.json")

    # Group by category so each category gets its own session
    by_category: dict = {}
    for q in subset:
        by_category.setdefault(q["category"], []).append(q)

    for cat in CATEGORIES_ORDERED:
        questions = by_category.get(cat, [])
        if not questions:
            continue

        label = CATEGORY_LABELS.get(cat, cat)

        # Permissions category uses rag_user sessions; all others use admin
        session_user = RAG_TEST_USER if cat == "permissions" else "Administrator"

        frappe.set_user(session_user)
        session = create_session()
        session_id = session["session_id"]
        frappe.set_user("Administrator")

        # Warm up the session to avoid Gemini empty-first-response edge case
        frappe.set_user(session_user)
        send_message(session_id=session_id, content="Hi")
        frappe.set_user("Administrator")
        for _ in range(20):
            frappe.db.commit()
            frappe.set_user(session_user)
            msgs = get_messages(session_id=session_id)["messages"]
            frappe.set_user("Administrator")
            assistant_msgs = [m for m in msgs if m["role"] == "assistant"]
            if assistant_msgs and assistant_msgs[-1].get("status") in ("Completed", "Failed"):
                break
            time.sleep(POLL_INTERVAL)

        print(f"\n--- {label} (session {session_id}) ---")

        for entry in questions:
            result = run_question(session_id, entry)
            all_results.append(result)

            icon = "PASS" if result["pass"] else "FAIL"
            note = f"  — {result['failure_reason']}" if result["failure_reason"] else ""
            cit_info = (
                f"  [{result['citation_count']} cit: "
                f"{','.join(result['citation_types']) or 'none'}]"
                if result.get("status") else ""
            )
            print(
                f"  [{result['id']}] {icon}"
                f"  {result['elapsed_s']}s{cit_info}{note}"
            )

        frappe.set_user(session_user)
        archive_session(session_id=session_id)
        frappe.set_user("Administrator")

    # Write full results
    output = {
        "meta": {
            "runner_version": RUNNER_VERSION,
            "executed_at":    datetime.now().isoformat(timespec="seconds"),
            "matrix":         "tests/v2_regression_matrix.json",
            "rag_test_user":  RAG_TEST_USER,
            "only_category":  only_category,
            "total_questions": len(all_results),
        },
        "results": all_results,
    }
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    passes = sum(1 for r in all_results if r["pass"])
    print(f"\nDone. {len(all_results)} questions → {results_path}")
    print(f"      {passes}/{len(all_results)} passed")

    print_scorecard(all_results)
