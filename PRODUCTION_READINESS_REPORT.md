# FrappeRAG v1.1 — Production Readiness Report

**Date:** 2026-04-14
**Scope:** Full app review against constitution, security, resilience, performance, install, and observability checklists.
**Verdict:** **Ready with caveats** — 3 blockers (all fixable in a day), several warnings worth addressing before first external users.

---

## Blockers

### [Blocker] Exception hierarchy breaks sync_runner error handling
**File:** `frapperag/rag/sidecar_client.py:18-38` + `frapperag/rag/sync_runner.py:43,72,101,131`
**Issue:** `SidecarUnavailableError` and `SidecarPermanentError` inherit from `Exception`, not from `SidecarError`. The sync_runner catches only `SidecarError`, so the two most common sidecar failure modes (sidecar down → `SidecarUnavailableError`, bad request → `SidecarPermanentError`) fall through to the outer `except Exception` block. They still get recorded as Failed, but with a full traceback instead of a clean `str(exc)` message — and the classified log tag `failure_reason=Sidecar unavailable` is skipped. More importantly, any future code that relies on the `SidecarError` catch doing something distinct from the generic fallback will silently not fire.
**Fix:** Make `SidecarUnavailableError` and `SidecarPermanentError` subclass `SidecarError` instead of `Exception`. One-line change per class.
**Effort:** S

### [Blocker] Sidecar sleep vs httpx timeout race creates zombie jobs
**File:** `frapperag/sidecar/main.py:390-401` + `frapperag/rag/sidecar_client.py:237`
**Issue:** When Gemini returns `ResourceExhausted`, the sidecar sleeps 60 seconds in `time.sleep()` and retries. Meanwhile, the worker's httpx timeout is 120 seconds total. If the Gemini retry takes >60 seconds after the sleep, the worker times out at 120s and marks the chat message Failed. But the sidecar continues processing and may receive a valid Gemini response that is silently discarded — the user sees "temporarily unavailable" while the response was actually generated. Worse, if the worker retries (3 attempts), the same question hits Gemini multiple times, each potentially triggering another 60s sleep cascade.
**Fix:** Replace `time.sleep(60)` in the sidecar with a shorter sleep (10-15s) and cap total sidecar-side retries to ensure the entire operation completes well within the 120s httpx timeout. Alternatively, bump the httpx timeout to 180s to accommodate one 60s sleep + a full Gemini round trip.
**Effort:** S

### [Blocker] `import lancedb` in install.py violates Constitution Principle IV
**File:** `frapperag/setup/install.py:21`
**Issue:** `_ensure_existing_lancedb_indices()` imports `lancedb` directly in a Frappe worker context during `after_install`. This means lancedb (and its transitive deps including pyarrow) must be installed in the Frappe web/worker Python environment, not just the sidecar. It increases the attack surface on web workers and contradicts the architectural rule "Workers MUST NOT import lancedb or sentence_transformers directly." The function also runs synchronously during `bench install-app`, blocking the CLI for potentially minutes on large datasets.
**Fix:** Move the ANN index creation to a sidecar endpoint (e.g., `POST /admin/rebuild-indices`) called by `after_install` via `sidecar_client`, or defer it to first sidecar startup. The `try/except Exception: pass` wrapper at line 65 means it is already optional — removing the call entirely is safe for fresh installs (no tables exist yet) and upgrades (flat scan still works).
**Effort:** M

---

## Warnings

### [Warning] `_get_port()` uses uncached Settings read on every sidecar call
**File:** `frapperag/rag/sidecar_client.py:49`
**Issue:** `_get_port()` calls `frappe.get_doc("AI Assistant Settings")` (uncached) and is invoked on every sidecar function call where `port` is not explicitly passed. A single chat job triggers 2-3 sidecar calls, each hitting the DB to re-read the port. This adds 2-3 unnecessary DB round trips per chat message.
**Fix:** Replace `frappe.get_doc(...)` with `frappe.get_cached_doc("AI Assistant Settings")` or `frappe.db.get_single_value("AI Assistant Settings", "sidecar_port")` which is lighter.
**Effort:** S

### [Warning] `_load_aggregate_allowlists()` uses uncached Settings read
**File:** `frapperag/rag/query_executor.py:465`
**Issue:** `frappe.get_single("AI Assistant Settings")` is called on every chat job that routes to an aggregate query. `get_single` is not cached. Combined with the `_get_port()` issue, a single aggregate chat job makes 4-5 uncached Settings reads.
**Fix:** Use `frappe.get_cached_doc("AI Assistant Settings")`.
**Effort:** S

### [Warning] `get_job_status` lacks record-level permission check (IDOR)
**File:** `frapperag/api/indexer.py:28`
**Issue:** The endpoint uses `frappe.has_permission("AI Indexing Job", throw=True)` — a DocType-level check. Any authenticated user with read on the `AI Indexing Job` DocType can retrieve status, `error_detail`, and timing metadata for any job by supplying its ID. This is a minor Insecure Direct Object Reference.
**Fix:** Change to `frappe.has_permission("AI Indexing Job", doc=job_id, ptype="read", throw=True)`.
**Effort:** S

### [Warning] Stalled indexing jobs never notify the user via realtime
**File:** `frapperag/rag/indexer.py:307-334`
**Issue:** `mark_stalled_jobs()` sets status to `"Failed (Stalled)"` in the DB but never calls `frappe.publish_realtime`. If a user is watching the RAG Admin UI during a stalled indexing run, the progress spinner never resolves — they must manually refresh.
**Fix:** Add a `frappe.publish_realtime("rag_index_error", {...}, user=job.owner)` call after marking the job stalled.
**Effort:** S

### [Warning] Stalled-job sweepers miss Queued-forever jobs
**File:** `frapperag/rag/indexer.py:307-334`, `frapperag/rag/sync_runner.py:146-161`
**Issue:** The indexing sweeper only catches `status = "Running"` jobs older than 2 hours. The sync sweeper only catches `outcome = "Running"`. If an RQ worker crashes at dequeue time, the job stays `Queued` indefinitely — never swept. Only the chat message sweeper (which checks `status = "Pending"` by creation time) implicitly covers this case.
**Fix:** Add a second pass to each sweeper for `Queued` jobs older than a threshold (e.g., 30 min for sync, 4 hours for indexing).
**Effort:** S

### [Warning] `sidecar_health()` blocks gunicorn worker for up to 5 seconds
**File:** `frapperag/api/indexer.py:171-180`
**Issue:** This `@frappe.whitelist()` endpoint makes a synchronous `httpx.get(url, timeout=5.0)` call in the HTTP handler. If the sidecar is slow or down, the gunicorn worker is blocked for 5 seconds per request. Under load, this can exhaust the gunicorn worker pool.
**Fix:** Either lower the timeout to 2s (it is a liveness check — fast or fail), or return cached health from the `RAG System Health` Single DocType (which is already updated every scheduler tick) instead of probing live.
**Effort:** S

### [Warning] No LanceDB compaction scheduled
**File:** (absent — no compaction calls anywhere in codebase)
**Issue:** LanceDB's `merge_insert` appends delta fragment files internally. Without periodic `table.compact_files()` and `table.cleanup_old_versions()`, the number of fragment files grows over time and vector search latency degrades. On a busy ERPNext with frequent saves, this becomes noticeable within weeks.
**Fix:** Add a sidecar endpoint (e.g., `POST /admin/compact`) that iterates all `v3_*` tables and calls `compact_files()` + `cleanup_old_versions()`. Schedule it via a daily or weekly scheduler event using `sidecar_client`.
**Effort:** M

### [Warning] `run_purge_job` has zero logging
**File:** `frapperag/rag/sync_runner.py:119-143`
**Issue:** The purge job has no `_log()` calls at all — no `[JOB_START]`, no success tag, no failure log. If a purge silently fails, the only record is the `Sync Event Log` DB row. The scheduler sweepers also have no logging (`mark_stalled_jobs`, `mark_stalled_sync_jobs`, `prune_sync_event_log`).
**Fix:** Add `_log().info(...)` at entry and exit of each function.
**Effort:** S

### [Warning] `seed_all_settings()` uses `ignore_validate=True`
**File:** `frapperag/setup/install.py:205`
**Issue:** The install/migrate hook saves AI Assistant Settings with `flags.ignore_validate = True`, bypassing the `_FIELDNAME_RE` regex validation on aggregate fields. The hardcoded seed values are safe, but if a future developer adds a malformed fieldname to `_DEFAULT_AGGREGATE_FIELDS`, it would bypass the SQL-injection guard that the validate hook enforces.
**Fix:** Remove `flags.ignore_validate = True` and ensure the seed values pass validation naturally (they already do). Keep `flags.ignore_mandatory = True` since `gemini_api_key` is empty at install time.
**Effort:** S

### [Warning] Python version mismatch between pyproject.toml and README
**File:** `pyproject.toml:6` + `README.md:85`
**Issue:** `pyproject.toml` declares `requires-python = ">=3.10"` while the README states "Python 3.11+". If someone installs on Python 3.10, they won't get a warning from pip but may hit features only available in 3.11+.
**Fix:** Align both to the same version. If 3.10 is supported, update the README. If 3.11 is the true floor, update `pyproject.toml`.
**Effort:** S

### [Warning] `pyproject.toml` has no version pins; `requirements.txt` only has floors
**File:** `pyproject.toml:10-18` + `frapperag/requirements.txt`
**Issue:** `pyproject.toml` lists bare dependency names with no version constraints. `requirements.txt` uses `>=` floors but no ceilings. A `pip install` six months from now could pull a breaking `lancedb` or `google-generativeai` release. The `torch>=2.0.0` floor in `requirements.txt` could also pull the CUDA wheel if the CPU index URL is not used.
**Fix:** Add upper-bound pins (e.g., `lancedb>=0.8.0,<0.12`) in `pyproject.toml` or add a `constraints.txt`. At minimum, pin `google-generativeai` which has had breaking API changes across minor versions.
**Effort:** S

### [Warning] Aggregate queries bypass per-record permission_query_conditions
**File:** `frapperag/rag/query_executor.py:529`
**Issue:** The aggregate query path (`_execute_aggregate_doctype`) and the SQL template queries (`_execute_top_selling_items`, etc.) check DocType-level `frappe.has_permission` but issue raw `frappe.db.sql` that ignores `permission_query_conditions`. A user with read access to Sales Invoice but restricted to their own customer's invoices (via `permission_query_conditions`) can see aggregates across all invoices. This is by design for analytics, but undocumented.
**Fix:** Document this as a known trade-off in the README under Query Execution. Optionally add a warning banner in the AI Assistant Settings UI when aggregate queries are enabled for a DocType that has `permission_query_conditions`.
**Effort:** S (doc) / M (UI warning)

---

## Nice-to-have

### [Nice-to-have] Sync Event Log missing diagnostic fields
**File:** `frapperag/frapperag/doctype/sync_event_log/sync_event_log.json`
**Issue:** The log is missing `user` (which user's context the sync ran under), `old_name` (for Rename events), and `retry_count`. An admin debugging a failed sync cannot answer "who triggered this?" or "was this a rename, and from what name?" without correlating with RQ logs.
**Fix:** Add `user` (Data), `old_name` (Data), and `retry_count` (Int, default 0) fields to the DocType. Populate in `run_sync_job` and `retry_sync`.
**Effort:** S

### [Nice-to-have] Chat Message missing `queue_job_id` field
**File:** `frapperag/frapperag/doctype/chat_message/chat_message.json`
**Issue:** Unlike `AI Indexing Job` which stores a `queue_job_id`, `Chat Message` has no reference to the RQ job. Cross-referencing a failed chat message with RQ worker logs requires matching by timestamp.
**Fix:** Add a `queue_job_id` (Data) field and populate it in `send_message` after `frappe.enqueue`.
**Effort:** S

### [Nice-to-have] `query_executor` tool executions produce no log output
**File:** `frapperag/rag/query_executor.py` (entire file)
**Issue:** The `execute_query()` dispatcher and all SQL template functions have no `_log()` calls. When a query tool call fails or returns unexpected results, there is no log trail — only the chat message's `context_sources` JSON captures what happened.
**Fix:** Add `_log().info(f"[TOOL_CALL] template={template} params={...}")` at entry and `[TOOL_RESULT]` with row count at exit.
**Effort:** S

### [Nice-to-have] Permission denial in sync is silent
**File:** `frapperag/rag/sync_runner.py:56-58,82-85`
**Issue:** When `frappe.has_permission` returns False, the sync log is set to `"Skipped"` with no log line and no `error_message` explaining why. An admin reviewing the Sync Event Log sees "Skipped" with no further context.
**Fix:** Set `error_message` to something like `"Permission denied for user {user} on {doctype} {name}"` and add a `_log().info(...)` call.
**Effort:** S

### [Nice-to-have] Scheduler sweepers and pruner produce no log output
**File:** `frapperag/rag/indexer.py:307-334`, `frapperag/rag/sync_runner.py:146-168`
**Issue:** `mark_stalled_jobs`, `mark_stalled_sync_jobs`, and `prune_sync_event_log` run silently. It is impossible to tell from logs when they last ran or how many records they affected.
**Fix:** Add `_log().info(f"[SWEEP] marked {len(stalled)} stalled ...")` and `_log().info(f"[PRUNE] deleted entries older than {cutoff}")`.
**Effort:** S

### [Nice-to-have] `health.py` scheduler does uncached Settings read every tick
**File:** `frapperag/rag/health.py:25`
**Issue:** `run_health_check()` runs on every scheduler tick (`scheduler_events["all"]`) and calls `frappe.get_doc("AI Assistant Settings")` uncached. Low individual cost but unnecessarily frequent.
**Fix:** Use `frappe.get_cached_doc` or `frappe.db.get_single_value`.
**Effort:** S

### [Nice-to-have] README missing nginx/socket.io production note
**File:** `README.md`
**Issue:** The BACKLOG.md notes "Production needs nginx in front so socket.io actually works and users don't eat the 2s poll floor on every message." This is not documented in the README's Running or Installation sections. A production deployer would hit the polling fallback and see degraded chat responsiveness without knowing why.
**Fix:** Add a "Production deployment" section to the README documenting the nginx requirement for WebSocket proxying and the expected Procfile layout.
**Effort:** S

### [Nice-to-have] Hybrid retrieval gap not documented as a known limitation
**File:** `README.md`
**Issue:** The current retrieval pipeline is pure vector search (semantic only). There is no keyword/BM25 component. Exact-match queries (e.g., "show me invoice INV-2024-001") rely on semantic similarity, which can miss when the query is a verbatim identifier. This is a known architectural limitation not documented anywhere user-facing.
**Fix:** Add a "Known limitations" section to the README noting that retrieval is semantic-only and exact identifier lookups may require the record_lookup tool rather than vector search.
**Effort:** S

### [Nice-to-have] PHASE_9_BACKLOG.md referenced but does not exist
**File:** `FrappeRAG_Master.md` (references "mid-phase ideas go to PHASE_9_BACKLOG.md")
**Issue:** The file does not exist. Backlog items are in `BACKLOG.md` instead. Items flagged "before prod" cannot be checked against a file that doesn't exist. The only backlog items found (in `BACKLOG.md`) are the EM-03 timeout cascade and the nginx/socket.io note — neither is flagged as a prod blocker.
**Fix:** Either create `PHASE_9_BACKLOG.md` or update `FrappeRAG_Master.md` to point to `BACKLOG.md`.
**Effort:** S

### [Nice-to-have] No `patches.txt` entries for migration safety
**File:** `frapperag/patches.txt`
**Issue:** The file is empty (no pre- or post-model-sync patches). This is fine for a fresh v1.0 install, but if any schema changes are made post-release (e.g., adding the suggested `user`, `old_name`, `retry_count` fields), they should go through proper Frappe patches rather than relying on `bench migrate` auto-sync alone.
**Fix:** No action needed now. Just ensure any future schema changes include a patch entry.
**Effort:** N/A

---

## Checklist Summary

| Area | Status | Notes |
|---|---|---|
| **Frappe-native (no direct lancedb/ST imports)** | FAIL | `setup/install.py` line 21 imports lancedb directly |
| **Permission-aware** | PASS | All retrieval, tool, and citation paths check `frappe.has_permission`; minor IDOR in `get_job_status` (Warning) |
| **Async-default** | PASS | All heavy work enqueued; `sidecar_health()` is the one exception (Warning) |
| **Zero external infra** | PASS | Only Gemini API and localhost sidecar; HuggingFace download on first run is one-time |
| **Session ownership** | PASS | `_assert_session_owner` on all chat endpoints; indexer endpoints use role checks |
| **SQL safety** | PASS | All identifiers allowlisted, all values parameterized; `ignore_validate` bypass is install-only (Warning) |
| **Report 3-layer guard** | PASS | Whitelist → report_type → has_permission — all intact |
| **Aggregate fail-closed** | PASS | Empty allowlist on Settings read failure; numeric fieldtype enforced at config time |
| **No secrets logged** | PASS | API key never appears in any log statement |
| **Sidecar localhost-only** | PASS | Hardcoded `host="127.0.0.1"` in `__main__` block |
| **Sidecar try/except coverage** | PASS (with caveat) | All calls go through `_retry_call`; but exception hierarchy is broken (Blocker) |
| **Stalled job sweepers** | PARTIAL | All three job types covered; Queued-forever gap (Warning); no realtime on stalled indexing (Warning) |
| **Realtime + polling fallback** | PASS | `get_message_status` polling endpoint mirrors realtime payload |
| **JSON default=str** | PASS | All `json.dumps` of citations/context use `default=str` |
| **Gemini timeout handling** | FAIL | Sidecar 60s sleep races with worker 120s httpx timeout (Blocker) |
| **N+1 queries** | PASS (acceptable) | Indexer N+1 is structural (child tables); retriever is bounded by TOP_K=5 |
| **Cached Settings in hot paths** | PARTIAL | `chat_runner` and `sync_hooks` use cache; `sidecar_client`, `query_executor`, `health` do not (Warning) |
| **Sync log pruning** | PASS | Daily scheduler, 30-day retention, bulk delete |
| **LanceDB unbounded growth** | PARTIAL | `merge_insert` prevents row duplication; no compaction scheduled (Warning) |
| **after_install idempotent** | PASS | `exist_ok=True` for dir, `if "rag_sidecar:" in content` guard for Procfile, set-based dedup for seed data |
| **hooks.py fixtures complete** | PASS | Roles fixture present; DocTypes auto-created by Frappe; child tables included |
| **requirements.txt** | PARTIAL | Floors present, no ceilings; version mismatch between pyproject.toml and README (Warning) |
| **Procfile entry** | PASS | Dynamically written by `after_install`; idempotent |
| **Migration patches** | N/A | No schema changes since v1.0; `patches.txt` is empty |
| **Structured logging** | PARTIAL | Good coverage in chat/index/sync hot paths; gaps in sweepers, query_executor, purge (Nice-to-have) |
| **Sync Event Log debuggability** | PARTIAL | Captures outcome and error; missing user, old_name, retry_count (Nice-to-have) |
| **"Why did this chat fail?" from DocType** | PASS | `failure_reason` + `error_detail` + `context_sources` on Chat Message is sufficient |
| **PHASE_9_BACKLOG.md** | N/A | File does not exist; BACKLOG.md has no "before prod" flags |
| **nginx/socket.io note in README** | MISSING | Documented only in BACKLOG.md (Nice-to-have) |
| **Hybrid retrieval gap documented** | MISSING | Not documented anywhere user-facing (Nice-to-have) |

---

## Verdict

**Ready with caveats.**

### Prioritized Fix List

| Priority | Item | Effort |
|---|---|---|
| 1 | Fix exception hierarchy (`SidecarError` subclassing) | S |
| 2 | Fix sidecar sleep/timeout race | S |
| 3 | Remove `import lancedb` from install.py | M |
| 4 | Cache Settings reads in `sidecar_client._get_port()` and `query_executor._load_aggregate_allowlists()` | S |
| 5 | Add record-level permission check in `get_job_status` | S |
| 6 | Add realtime notification for stalled indexing jobs | S |
| 7 | Add Queued-forever sweep pass to stalled job sweepers | S |
| 8 | Remove `ignore_validate=True` from `seed_all_settings()` | S |
| 9 | Pin dependency upper bounds in `pyproject.toml` | S |
| 10 | Schedule LanceDB compaction | M |
| 11 | Fix `sidecar_health()` blocking (use cached health or lower timeout) | S |
| 12 | Add logging to `run_purge_job` and sweepers | S |
| 13 | Document nginx requirement and hybrid retrieval limitation in README | S |
