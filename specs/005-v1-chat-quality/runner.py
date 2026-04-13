"""
FrappeRAG v1.0 Chat Quality Test Runner
========================================
Drives all 30 questions in test-matrix.json through the chat API, auto-grades
each response against its pass_criteria, and prints a category-level scorecard
for comparison with the v0.9 Phase 7 results.

Unlike the v0.9 runner (which left grade fields blank for human review), this
runner grades automatically — no manual step required.

Invocation (via bench execute):

    # Pre-flight check only:
    bench --site golive.site1 execute frapperag.v1_runner.main \
        --kwargs "{'check_only': True}"

    # Full run:
    bench --site golive.site1 execute frapperag.v1_runner.main

Do NOT invoke directly with `python3` — requires Frappe app context.
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

POLL_INTERVAL = 3
TIMEOUT = 60

_SPEC_DIR = os.path.dirname(os.path.abspath(__file__))
MATRIX_PATH = os.path.join(_SPEC_DIR, "test-matrix.json")
RESULTS_PATH = os.path.join(_SPEC_DIR, "raw-results.json")

TEST_USER = "rag-tester@golive.site1"
RUNNER_VERSION = "1.0"

CATEGORIES_ORDERED = [
    "English Lookups",
    "Arabic Lookups",
    "Aggregations",
    "Cross-DocType",
    "Stock",
    "Out-of-Scope",
    "Empty Results",
    "Vague",
    "Capability",
    "Permissions",
]

# ---------------------------------------------------------------------------
# Auto-grader
# ---------------------------------------------------------------------------

def auto_grade(result: dict, criteria: dict) -> tuple:
    """Return (grade, reason) for a completed result.

    grade is one of: "PASS" | "FAIL" | "SKIP"

    Grading modes (set via criteria["mode"]):
      tool_call      — PASS if expected citation type is present (matching
                       optional template) OR if the tool ran and the user
                       was denied via permission_denied_pattern.
      report_result  — PASS if a report_result citation with the expected
                       report name is present.
      decline        — PASS if the response contains any keyword from
                       criteria["any_of"] (AI honestly refused or clarified).
      permission_gate — PASS if the response explicitly denies access and
                       contains no data citations (record_detail, query_result,
                       report_result).
    """
    if result.get("status") == "Failed" and not result.get("response_excerpt"):
        return "SKIP", "runner error — no response captured"

    mode = criteria.get("mode", "decline")
    text = (result.get("response_excerpt") or "").lower()
    citations = result.get("citations") or []

    # -----------------------------------------------------------------------
    if mode == "tool_call":
        expected_type = criteria.get("expected_citation_type")
        expected_tmpl = criteria.get("expected_template")  # only for query_result

        # Tier 1: citation present and matches
        for c in citations:
            if c.get("type") == expected_type:
                if expected_tmpl is None or c.get("template") == expected_tmpl:
                    label = expected_type
                    if expected_tmpl:
                        label += f"/{expected_tmpl}"
                    return "PASS", f"{label} citation present"

        # Tier 2: tool ran but access was denied
        pattern = (criteria.get("permission_denied_pattern") or "").lower()
        if pattern and pattern in text:
            return "PASS", "tool called — permission correctly denied"

        # Fail: tool was not called (vector fallback) or wrong tool
        detail = f"expected {expected_type!r}"
        if expected_tmpl:
            detail += f" (template={expected_tmpl!r})"
        return "FAIL", f"{detail} citation absent and no permission message"

    # -----------------------------------------------------------------------
    elif mode == "report_result":
        expected_report = criteria.get("expected_report")
        for c in citations:
            if c.get("type") == "report_result":
                if not expected_report or c.get("report_name") == expected_report:
                    return "PASS", f"report_result '{c.get('report_name')}' present"
        return "FAIL", f"no report_result citation (expected {expected_report!r})"

    # -----------------------------------------------------------------------
    elif mode == "decline":
        keywords = criteria.get("any_of") or [
            "cannot", "sorry", "not able", "unable", "can't",
            "don't have", "do not have", "not within",
        ]
        for kw in keywords:
            if kw.lower() in text:
                return "PASS", f"appropriate response (matched {kw!r})"
        return "FAIL", "no expected decline/response keyword found in excerpt"

    # -----------------------------------------------------------------------
    elif mode == "permission_gate":
        # Must NOT return data citations
        data_types = {"record_detail", "query_result", "report_result"}
        for c in citations:
            if c.get("type") in data_types:
                return "FAIL", f"data citation {c['type']!r} returned for restricted record"
        # Must contain a denial signal
        denial_signals = [
            "permission", "cannot", "not able", "unable",
            "don't have", "do not have", "no accessible", "not accessible",
        ]
        for sig in denial_signals:
            if sig in text:
                return "PASS", f"access correctly denied (matched {sig!r})"
        return "FAIL", "no denial signal — response may have leaked restricted data"

    # -----------------------------------------------------------------------
    return "FAIL", f"unknown grader mode {mode!r}"


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def pre_flight_checks(matrix):
    """Run pre-flight checks and raise SystemExit(1) on any failure."""

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

    # 2. Test user exists
    if not frappe.db.exists("User", TEST_USER):
        print(f"FAIL Test user '{TEST_USER}' does not exist")
        raise SystemExit(1)
    print("OK   Test user exists")

    # 3. Matrix: 30 questions, all have pass_criteria
    if len(matrix) != 30:
        print(f"FAIL Matrix must have 30 questions, found {len(matrix)}")
        raise SystemExit(1)
    missing_criteria = [q["question_id"] for q in matrix if not q.get("pass_criteria")]
    if missing_criteria:
        print(f"FAIL Questions missing pass_criteria: {missing_criteria}")
        raise SystemExit(1)
    print(f"OK   Matrix loads ({len(matrix)} questions, all have pass_criteria)")

    # 4. Restricted records are unreadable by test user
    original_user = frappe.session.user
    frappe.set_user(TEST_USER)
    try:
        for q in matrix:
            for r in (q.get("restricted_records") or []):
                try:
                    can_read = frappe.has_permission(r["doctype"], "read", r["name"])
                except frappe.DoesNotExistError:
                    print(
                        f"WARN {r['doctype']}/{r['name']} does not exist in DB"
                        f" ({q['question_id']}) — update test-matrix.json"
                    )
                    continue
                if can_read:
                    print(
                        f"FAIL Test user CAN read {r['doctype']}/{r['name']}"
                        f" — invalidates {q['question_id']}"
                    )
                    raise SystemExit(1)
    finally:
        frappe.set_user(original_user)
    print("OK   All restricted records unreadable by test user")

    # 5. Output path clear
    if os.path.exists(RESULTS_PATH):
        print(f"FAIL {RESULTS_PATH} already exists — move or delete it first")
        raise SystemExit(1)
    print("OK   Output path clear")


# ---------------------------------------------------------------------------
# Single-question driver  (race-condition-safe, same as v0.9 runner)
# ---------------------------------------------------------------------------

def run_question(session_id, q, timeout=TIMEOUT):
    original_user = frappe.session.user
    frappe.set_user(TEST_USER)
    try:
        messages_before = get_messages(session_id=session_id)["messages"]
        assistant_count_before = sum(
            1 for m in messages_before if m["role"] == "assistant"
        )
        t_start = time.time()

        try:
            resp = send_message(session_id=session_id, content=q["question"])
        except frappe.PermissionError as e:
            return _error_result(q, f"PermissionError on send: {e}")
        except frappe.ValidationError:
            time.sleep(5)
            try:
                resp = send_message(session_id=session_id, content=q["question"])
            except Exception as e2:
                return _error_result(q, f"ValidationError on send (retry): {e2}")

        user_message_id = resp.get("message_id", "")

        while True:
            elapsed = time.time() - t_start
            if elapsed > timeout:
                return _error_result(q, f"Timeout after {elapsed:.1f}s")

            frappe.db.commit()
            messages = get_messages(session_id=session_id)["messages"]
            assistant_msgs = [m for m in messages if m["role"] == "assistant"]

            if len(assistant_msgs) > assistant_count_before:
                latest = assistant_msgs[-1]
                if latest.get("status") in ("Completed", "Failed"):
                    citations = []
                    if latest.get("citations"):
                        try:
                            citations = json.loads(latest["citations"])
                        except Exception:
                            citations = [{"raw": latest["citations"]}]
                    return {
                        "question_id":      q["question_id"],
                        "category":         q["category"],
                        "question":         q["question"],
                        "user_message_id":  user_message_id,
                        "response_excerpt": latest["content"][:500],
                        "elapsed_s":        round(elapsed, 2),
                        "citations":        citations,
                        "status":           latest["status"],
                        "tokens_used":      latest.get("tokens_used", 0),
                        "grade":            "",
                        "grade_notes":      "",
                    }
            time.sleep(POLL_INTERVAL)

    except SystemExit:
        raise
    except Exception as e:
        return _error_result(q, f"Unexpected: {type(e).__name__}: {e}")
    finally:
        frappe.set_user(original_user)


def _error_result(q, reason):
    return {
        "question_id":      q["question_id"],
        "category":         q["category"],
        "question":         q["question"],
        "user_message_id":  "",
        "response_excerpt": "",
        "elapsed_s":        0.0,
        "citations":        [],
        "status":           "Failed",
        "grade":            "SKIP",
        "grade_notes":      reason,
    }


# ---------------------------------------------------------------------------
# Category summary printer
# ---------------------------------------------------------------------------

def print_scorecard(raw_results):
    """Print the category pass/fail table and overall verdict."""
    print()
    print("=" * 62)
    print("  FrappeRAG v1.0 — Chat Quality Scorecard")
    print("=" * 62)
    header = f"  {'Category':<20} {'PASS':>5} {'FAIL':>5} {'SKIP':>5}   {'Rate':>6}"
    print(header)
    print("  " + "-" * 58)

    # v0.9 baseline for comparison (from findings.md)
    V09_RATES = {
        "English Lookups":  0,
        "Arabic Lookups":   33,
        "Aggregations":     0,
        "Cross-DocType":    0,
        "Stock":            0,
        "Out-of-Scope":     100,
        "Empty Results":    0,
        "Vague":            100,
        "Capability":       100,
        "Permissions":      100,
    }

    overall_pass = overall_total = 0

    for cat in CATEGORIES_ORDERED:
        rows = [r for r in raw_results if r["category"] == cat]
        passes = sum(1 for r in rows if r["grade"] == "PASS")
        fails  = sum(1 for r in rows if r["grade"] == "FAIL")
        skips  = sum(1 for r in rows if r["grade"] == "SKIP")
        total  = passes + fails + skips

        overall_pass  += passes
        overall_total += total

        rate_pct = int(round(passes / total * 100)) if total else 0
        v09 = V09_RATES.get(cat, "?")
        delta = f"+{rate_pct - v09}%" if rate_pct > v09 else (
            f"{rate_pct - v09}%" if rate_pct < v09 else "  ="
        )
        print(
            f"  {cat:<20} {passes:>5} {fails:>5} {skips:>5}"
            f"   {rate_pct:>5}%  (v0.9: {v09:>3}%  {delta})"
        )

    overall_rate = int(round(overall_pass / overall_total * 100)) if overall_total else 0
    print("  " + "-" * 58)
    print(f"  {'TOTAL':<20} {overall_pass:>5} {overall_total - overall_pass:>5}"
          f"   {'':>5}   {overall_rate:>5}%")
    print()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(check_only=False):
    with open(MATRIX_PATH) as f:
        matrix = json.load(f)

    pre_flight_checks(matrix)

    if check_only:
        print("\nPre-flight passed. Ready to run.")
        return

    raw_results = []

    by_category = {}
    for q in matrix:
        by_category.setdefault(q["category"], []).append(q)

    for category in CATEGORIES_ORDERED:
        questions = by_category.get(category, [])
        if not questions:
            continue

        frappe.set_user(TEST_USER)
        session = create_session()
        session_id = session["session_id"]
        frappe.set_user("Administrator")

        print(f"\n--- {category} (session {session_id}) ---")

        for q in questions:
            result = run_question(session_id, q)

            # Auto-grade (skipped if runner already set grade="SKIP" on error)
            if result["grade"] != "SKIP":
                criteria = q.get("pass_criteria") or {}
                grade, reason = auto_grade(result, criteria)
                result["grade"] = grade
                result["grade_notes"] = reason

            raw_results.append(result)

            icon = "PASS" if result["grade"] == "PASS" else (
                "SKIP" if result["grade"] == "SKIP" else "FAIL"
            )
            note = f"  — {result['grade_notes']}" if result["grade_notes"] else ""
            print(
                f"  [{result['question_id']}] {icon}"
                f"  {result['elapsed_s']}s{note}"
            )

        frappe.set_user(TEST_USER)
        archive_session(session_id=session_id)
        frappe.set_user("Administrator")

    # Write results
    output = {
        "meta": {
            "bench":               "golive-bench",
            "site":                "golive.site1",
            "sidecar_port":        8100,
            "runner_version":      RUNNER_VERSION,
            "executed_at":         datetime.now().isoformat(timespec="seconds"),
            "test_user":           TEST_USER,
            "matrix":              "specs/005-v1-chat-quality/test-matrix.json",
        },
        "results": raw_results,
    }

    with open(RESULTS_PATH, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    skip_count = sum(1 for r in raw_results if r["grade"] == "SKIP")
    print(f"\nDone. {len(raw_results)} results → {RESULTS_PATH}  ({skip_count} SKIP)")

    print_scorecard(raw_results)
