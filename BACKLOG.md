Backlog

## CH-05 record_lookup citation missing for PUR-ORD-2026-00077 (v1.2)

- **Question:** (citation_hygiene category — asks about a specific Purchase Order by number)
- **Expected:** `record_detail` citation + `must_contain: PUR-ORD-2026-00077`
- **Observed:** FAIL — 0 citations, response does not mention the PO number
- **Run:** v2_results_20260426T043904.json, 2026-04-26
- **Root cause candidates:**
  1. PUR-ORD-2026-00077 not yet indexed under the new `v5_gemini_*` prefix (was indexed under old `v4_*` with e5-small; provider changed to Gemini in v1.2 → full re-index required).
  2. Exact alphanumeric ID lookup known weakness of dense-only retrieval (see 7-D-002). `record_lookup` tool should cover this but may not have fired.
- **Fix options:**
  1. Run Index All from `/rag-admin` to populate `v5_gemini_*` tables, then re-run CH-05.
  2. Verify the `record_lookup` SQL template is handling Purchase Order lookups correctly.
  3. Long-term: hybrid retrieval (dense + BM25) for exact-ID queries.

---

## EM-03 Timeout (non-regression)

- **Question:** "List all stock entries of type Transfer for item FAKE-ITEM-ZZZ-9999."
- **Expected:** decline (mode: `decline`, any_of: cannot/not have/only/unable/not able)
- **Observed:** SKIP — timeout after 120.6s. Passed in prior run (9.1s, matched 'cannot').
- **Root cause:** Session-level timeout cascade. Vague/Capability questions in the same run took 69s and 51s respectively, exhausting the session budget before EM-03 could execute.
- **Fix options:**
  1. Increase per-session or per-question timeout beyond 120s.
  2. Run Empty Results in its own isolated session (separate from slow categories).
  3. Investigate why VG-01 (69s) and CA-02 (51s) are slow — may be a sidecar retry/backoff issue.

 
Production needs nginx in front so socket.io actually works and users don't eat the 2s poll floor on every message.
Keep the polling loop even after socket.io works — it's cheap insurance against dropped events from reconnects, worker restarts, and backgrounded tabs.

## Notices from 2026-04-14 production-readiness fixes (not acted on)

- `api/indexer.py:48` — `list_jobs` and `get_job_status` both call `frappe.has_permission("AI Indexing Job", throw=True)`. `list_jobs` still uses a DocType-level check (no `doc=` arg). The IDOR fix was scoped to `get_job_status` per the report; `list_jobs` may deserve the same treatment once `get_all` pagination is in use by external users.
- `sidecar_client.py` — `_retry_call` sleeps between transient HTTP retries using `time.sleep` (1s, 2s). Under the new 180s timeout with a 15s sidecar sleep, worst-case is: 3 sidecar attempts × (15s sleep + Gemini RTT) + 3s retry back-off = still well under 180s. Budget appears safe but worth re-checking if Gemini RTT increases.
- `query_executor.py:465` — `frappe.get_single` was being used (not `frappe.get_doc`). Both return a doc object for Single DocTypes; `get_cached_doc` is a valid replacement for both.