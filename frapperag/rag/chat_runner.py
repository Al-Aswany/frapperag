import time as _time

import frappe
from frappe.utils import now_datetime, add_to_date
import json


def _log():
    """Return a site-aware logger with INFO level guaranteed."""
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger


def _load_report_whitelist() -> tuple[set, list]:
    """Load the allowed-report whitelist from AI Assistant Settings.

    Fetches all Report Filter rows for whitelisted reports in a single query
    (no N+1 loop). Returns a snapshot set of names for O(1) membership checks
    and a list of rich entry dicts for build_report_tool_definitions().

    Returns:
        whitelist_names: set of report name strings
        whitelist_entries: list of dicts {report, description, default_filters, filter_meta}
    """
    import json as _json

    settings = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    rows = [row for row in (settings.allowed_reports or []) if row.report]

    if not rows:
        return set(), []

    report_names = [row.report for row in rows]

    # Single query — no N+1 (research.md Decision 6)
    all_filter_meta = frappe.get_all(
        "Report Filter",
        filters={"parent": ["in", report_names]},
        fields=["parent", "fieldname", "label", "fieldtype", "mandatory", "default"],
    )
    filters_by_report: dict = {}
    for f in all_filter_meta:
        filters_by_report.setdefault(f.parent, []).append(f)

    entries = []
    names: set = set()
    for row in rows:
        default_filters: dict = {}
        if row.default_filters:
            try:
                default_filters = _json.loads(row.default_filters)
            except Exception:
                pass  # validated on save; defensive fallback only
        entries.append({
            "report": row.report,
            "description": row.description or "",
            "default_filters": default_filters,
            "filter_meta": filters_by_report.get(row.report, []),
        })
        names.add(row.report)
    return names, entries


def run_chat_job(message_id: str, session_id: str, user: str, question: str = "", **kwargs):
    """
    Background job entry point. Called by frappe.enqueue (queue="short").
    Site context already initialised by the Frappe worker.

    api_key is read from AI Assistant Settings here — NOT passed via enqueue kwargs
    (keeps credential out of Redis serialisation — same pattern as Phase 1).

    message_id kwarg name avoids collision with Frappe/RQ reserved 'job_id' kwarg.
    """
    from frapperag.rag.retriever import search_candidates, filter_by_permission
    from frapperag.rag.prompt_builder import build_messages, build_report_tool_definitions, build_query_tool_definitions
    from frapperag.rag.chat_engine import generate_response
    from frapperag.rag.report_executor import execute_report
    from frapperag.rag.query_executor import execute_query

    job_start = _time.monotonic()
    _log().info(f"[CHAT_START] message_id={message_id} session_id={session_id} user={user}")
    _log().info(f"[TIMING][{message_id}] run_chat_job START")

    # Read api_key from Settings — never from enqueue kwargs
    t0 = _time.monotonic()
    settings = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    api_key = settings.get_password("gemini_api_key")
    assistant_mode = ((getattr(settings, "assistant_mode", None) or "v1").strip() or "v1").lower()
    _log().info(f"[TIMING][{message_id}] settings_read {_time.monotonic() - t0:.3f}s")

    # Enforce the calling user's permission context (Principle III)
    frappe.set_user(user)

    try:
        # question is passed via enqueue kwargs — no frappe.get_doc() needed here,
        # which avoids Frappe's after_load hooks touching the Chat Message row and
        # creating a short-lived lock that would block the later UPDATE.
        # Fallback: web process was running old code that didn't pass question yet.
        if not question:
            question = frappe.db.get_value("Chat Message", message_id, "content") or ""
        _log().info(f"[TIMING][{message_id}] question_len={len(question)} chars")

        # Phase 2 shadow routing: classify the message for observability only.
        # This must never block or modify the existing v1 answer path.
        route = None
        try:
            from frapperag.assistant.intent_router import log_shadow_route_decision, route_question

            route = route_question(question, use_llm_fallback=False, settings=settings)
            log_shadow_route_decision(
                route,
                question=question,
                message_id=message_id,
                session_id=session_id,
                user=user,
            )
        except Exception:
            _log().exception("[ROUTER_SHADOW_FAILED] message_id=%s session_id=%s", message_id, session_id)

        # Close the implicit transaction opened by settings_read before the heavy I/O.
        # All subsequent work (search + generate) runs outside any open transaction,
        # so the final writes are in a clean, minimal transaction.
        t0 = _time.monotonic()
        frappe.db.commit()
        _log().info(f"[TIMING][{message_id}] pre_commit {_time.monotonic() - t0:.3f}s")

        hybrid_result = None
        if assistant_mode == "hybrid":
            try:
                from frapperag.assistant.chat_orchestrator import try_generate_hybrid_response

                t0 = _time.monotonic()
                hybrid_result = try_generate_hybrid_response(
                    question=question,
                    user=user,
                    message_id=message_id,
                    session_id=session_id,
                    route=route,
                    settings=settings,
                    api_key=api_key,
                )
                _log().info(
                    f"[TIMING][{message_id}] hybrid_attempt {_time.monotonic() - t0:.3f}s"
                    f" → handled={'yes' if hybrid_result else 'no'}"
                )
            except Exception:
                _log().exception("[HYBRID_ATTEMPT_FAILED] message_id=%s session_id=%s", message_id, session_id)

        filtered = []
        if hybrid_result:
            final_text = hybrid_result["final_text"]
            final_citations = hybrid_result.get("citations") or []
            tokens_used = hybrid_result.get("tokens_used", 0)
        else:
            # Load whitelist + build per-report Gemini tool declarations (FR-004, FR-005)
            t0 = _time.monotonic()
            whitelist_names, whitelist_entries = _load_report_whitelist()
            report_tools, slug_to_name = build_report_tool_definitions(whitelist_entries)
            query_tools, slug_to_template = build_query_tool_definitions()
            # Slug collision guard — execute_* and run_* prefixes must never overlap
            _slug_overlap = slug_to_name.keys() & slug_to_template.keys()
            assert not _slug_overlap, f"Tool slug collision detected: {_slug_overlap}"
            # Merge both tool lists; handle all four None combinations
            all_tools = (report_tools or []) + (query_tools or []) or None
            _log().info(
                f"[TIMING][{message_id}] load_whitelist {_time.monotonic() - t0:.3f}s"
                f" → {len(whitelist_names)} reports, tools={'yes' if all_tools else 'no'}"
            )

            # 1+2. Embed query + search all v4_* tables via sidecar (single HTTP call)
            t0 = _time.monotonic()
            candidates = search_candidates(question, api_key=api_key)
            _log().info(
                f"[TIMING][{message_id}] search_candidates {_time.monotonic() - t0:.3f}s"
                f" → {len(candidates)} candidates"
            )

            # 3. Filter by user permissions per-record (Principle III)
            t0 = _time.monotonic()
            filtered = filter_by_permission(candidates, user)
            _log().info(
                f"[TIMING][{message_id}] filter_by_permission {_time.monotonic() - t0:.3f}s"
                f" ({len(candidates)} → {len(filtered)} records)"
            )

            # 4. Load last 10 conversation turns (excluding the current Pending message)
            t0 = _time.monotonic()
            history_docs = frappe.db.get_all(
                "Chat Message",
                filters={"session": session_id, "name": ["!=", message_id]},
                fields=["role", "content"],
                order_by="creation desc",
                limit=10,
                ignore_permissions=False,
            )
            history = [{"role": d.role, "content": d.content} for d in reversed(history_docs)]
            _log().info(
                f"[TIMING][{message_id}] load_history {_time.monotonic() - t0:.3f}s"
                f" → {len(history)} turns"
            )

            # 5. Build Gemini message list
            t0 = _time.monotonic()
            messages = build_messages(question, filtered, history)
            prompt_chars = sum(len(p) for m in messages for p in m.get("parts", []))
            _log().info(
                f"[TIMING][{message_id}] build_messages {_time.monotonic() - t0:.3f}s"
                f" → {len(messages)} turns, ~{prompt_chars} chars in prompt"
            )

            # 6. Generate response via the configured chat runtime — pass tools when whitelist non-empty
            t0 = _time.monotonic()
            result = generate_response(messages, filtered, api_key, tools=all_tools)
            _log().info(
                f"[TIMING][{message_id}] generate_response {_time.monotonic() - t0:.3f}s"
                f" tokens_used={result['tokens_used']}"
            )

            # Branch on response type (FR-007, FR-008)
            if "tool_call" in result:
                tool_name = result["tool_call"]["name"]
                if tool_name in slug_to_name:
                    # Report execution path (existing)
                    t0 = _time.monotonic()
                    report_result = execute_report(
                        {"report_name": slug_to_name[tool_name], "filters": dict(result["tool_call"]["args"])},
                        user,
                        whitelist_names,
                    )
                    final_text = report_result["text"]
                    final_citations = report_result["citations"]
                    tokens_used = result["tokens_used"]
                    _log().info(f"[TIMING][{message_id}] execute_report {_time.monotonic() - t0:.3f}s")
                elif tool_name in slug_to_template:
                    # Query execution path (new — ExecuteQuery tool)
                    t0 = _time.monotonic()
                    query_result = execute_query(
                        {"template": slug_to_template[tool_name], "params": dict(result["tool_call"]["args"])},
                        user,
                    )
                    final_text = query_result["text"]
                    final_citations = query_result["citations"]
                    tokens_used = result["tokens_used"]
                    _log().info(f"[TIMING][{message_id}] execute_query {_time.monotonic() - t0:.3f}s")
                else:
                    # Unknown slug (should not happen — hallucinated function name)
                    final_text = result.get("text", "")
                    final_citations = result.get("citations", [])
                    tokens_used = result["tokens_used"]
            else:
                # Existing RAG path (unchanged)
                final_text = result["text"]
                final_citations = result["citations"]
                tokens_used = result["tokens_used"]

        # Separate tool-call results from retriever candidates.
        # citations      → only items the AI actually used (report_result, query_result,
        #                  record_detail) — rendered as chips in the UI.
        # context_sources → retriever candidates that were passed to the prompt as
        #                   background context — stored for auditability, not shown in UI.
        _TOOL_RESULT_TYPES = frozenset({"report_result", "query_result", "record_detail"})
        citations = [c for c in (final_citations or []) if c.get("type") in _TOOL_RESULT_TYPES]
        context_sources = [
            {"doctype": r["doctype"], "name": r["name"]}
            for r in filtered
        ]

        # 7. Update the user's Pending message to Completed (bypass ORM lifecycle)
        #
        # RAW SQL COUPLING — `tabChat Message` UPDATE (success path)
        # Columns written: status, modified, modified_by
        # Reason for raw SQL: frappe.set_value() acquires an after_load row-lock that
        # conflicts with the earlier get_all() read; raw SQL avoids the ORM lifecycle
        # entirely and keeps the write path in a single minimal transaction.
        # WARNING: If you add fields to chat_message.json that need to be stamped on
        # completion, you MUST add them to this UPDATE statement as well.
        t0 = _time.monotonic()
        now = now_datetime()
        frappe.db.sql(
            """UPDATE `tabChat Message`
               SET status = %s, modified = %s, modified_by = %s
               WHERE name = %s""",
            ("Completed", now, user, message_id),
        )
        _log().info(f"[TIMING][{message_id}] set_value_status {_time.monotonic() - t0:.3f}s")

        # 8. Insert assistant reply message (bypass ORM lifecycle)
        #
        # RAW SQL COUPLING — `tabChat Message` INSERT (assistant reply)
        # Columns written (positional, in order):
        #   name, creation, modified, modified_by, owner, docstatus (0), idx (0),
        #   session, role, content, citations, status, tokens_used
        # Reason for raw SQL: frappe.get_doc().insert() triggers after_insert hooks
        # and realtime events we don't want for internal assistant messages; raw SQL
        # keeps the insert atomic within the same transaction as the status UPDATE above.
        # WARNING: If you add fields to chat_message.json that must be populated on
        # creation, you MUST add them to this INSERT column list and VALUES tuple.
        t0 = _time.monotonic()
        reply_name = frappe.generate_hash(length=10)
        frappe.db.sql(
            """INSERT INTO `tabChat Message`
               (name, creation, modified, modified_by, owner, docstatus, idx,
                session, role, content, citations, context_sources, status, tokens_used)
               VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s, %s, %s)""",
            (
                reply_name, now, now, user, user,
                session_id, "assistant", final_text,
                json.dumps(citations, default=str),
                json.dumps(context_sources, default=str),
                "Completed", tokens_used,
            ),
        )
        _log().info(f"[TIMING][{message_id}] reply_insert {_time.monotonic() - t0:.3f}s")

        # 9. Set session title from first user question (FR-002)
        #    Only written when title is blank — idempotent on retry.
        t0 = _time.monotonic()
        session = frappe.get_doc("Chat Session", session_id)
        if not session.title:
            frappe.db.set_value("Chat Session", session_id, "title", question[:80].strip())
        _log().info(f"[TIMING][{message_id}] session_title {_time.monotonic() - t0:.3f}s")

        # Flush steps 7-9 in a single round-trip
        t0 = _time.monotonic()
        frappe.db.commit()
        _log().info(f"[TIMING][{message_id}] db_commit {_time.monotonic() - t0:.3f}s")

        # 10. Publish realtime response to the user (FR-014)
        t0 = _time.monotonic()
        _log().info(f"[REALTIME][{message_id}] publishing rag_chat_response to user={user!r}")
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id":  message_id,
                "session_id":  session_id,
                "status":      "Completed",
                "content":     final_text,
                "citations":   citations,
                "tokens_used": tokens_used,
            },
            user=user,
            after_commit=False,
        )
        _log().info(f"[TIMING][{message_id}] publish_realtime {_time.monotonic() - t0:.3f}s")

        _log().info(
            f"[TIMING][{message_id}] run_chat_job DONE total={_time.monotonic() - job_start:.3f}s"
        )
        _log().info(f"[CHAT_SUCCESS] message_id={message_id}")

    except Exception as exc:
        import traceback
        import httpx as _httpx
        from frapperag.rag.sidecar_client import SidecarUnavailableError as _SidecarUnavailableError
        from frapperag.rag.sidecar_client import SidecarPermanentError as _SidecarPermanentError
        tb = traceback.format_exc()
        _log().error(
            f"[TIMING][{message_id}] run_chat_job FAILED after {_time.monotonic() - job_start:.3f}s"
        )

        # Classify the exception into a user-readable failure reason (T016, T021)
        try:
            exc_type = type(exc).__name__
            exc_module = type(exc).__module__ or ""
            if isinstance(exc, _SidecarUnavailableError):
                failure_reason = "Assistant is temporarily unavailable — please try again shortly"
            elif isinstance(exc, _SidecarPermanentError):
                sc_suffix = f" (HTTP {exc.status_code})" if exc.status_code else ""
                failure_reason = f"Sidecar error{sc_suffix}"
            elif isinstance(exc, (_httpx.ConnectError, _httpx.TimeoutException)):
                failure_reason = "Assistant is temporarily unavailable — please try again shortly"
            elif (
                getattr(exc, "status_code", None) == 429
                or "ResourceExhausted" in exc_type
                or "quota" in str(exc).lower()
                or "429" in str(exc)
            ):
                failure_reason = "Too many requests — please wait a moment before trying again"
            elif "google" in exc_module or "generativeai" in exc_module:
                failure_reason = "Gemini API error"
            else:
                failure_reason = "Unknown error"
        except Exception:
            failure_reason = "Unknown error"
        failure_reason = failure_reason[:140]

        _log().warning(f"[CHAT_FAIL] message_id={message_id} failure_reason={failure_reason!r} error={tb[:200]}")
        # RAW SQL COUPLING — `tabChat Message` UPDATE (error path)
        # Columns written: status, error_detail, failure_reason, modified, modified_by
        # See step 7 above for why raw SQL is used instead of the ORM.
        # WARNING: If you add error-state fields to chat_message.json, update this
        # UPDATE statement to include them.
        frappe.db.sql(
            """UPDATE `tabChat Message`
               SET status = %s, error_detail = %s, failure_reason = %s,
                   modified = %s, modified_by = %s
               WHERE name = %s""",
            ("Failed", tb[:2000], failure_reason, now_datetime(), user, message_id),
        )
        frappe.db.commit()
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id":    message_id,
                "session_id":    session_id,
                "status":        "Failed",
                "failure_reason": failure_reason,
                "error":         tb[:500],
            },
            user=user,
            after_commit=False,
        )
        frappe.log_error(
            title=f"RAG Chat Job Failed [{message_id}]",
            message=tb,
        )


def mark_stalled_chat_messages():
    """
    Scheduler (every 5 min): transition Pending messages older than 10 minutes to Failed.
    Covers worker crashes and jobs that never dequeued (FR-016, SC-005).
    """
    cutoff  = add_to_date(now_datetime(), minutes=-10)
    stalled = frappe.db.get_all(
        "Chat Message",
        filters={"status": "Pending", "creation": ["<", cutoff]},
        fields=["name", "owner"],
    )
    _STALL_REASON = "Response timed out"
    for msg in stalled:
        frappe.db.set_value(
            "Chat Message",
            msg.name,
            {
                "status":         "Failed",
                "failure_reason": _STALL_REASON,
                "error_detail":   "Message exceeded 10-minute processing timeout. Worker may have crashed.",
            },
        )
    if stalled:
        frappe.db.commit()
        # Notify any open chat tab so the spinner resolves without a page reload
        for msg in stalled:
            frappe.publish_realtime(
                "chat_message_update",
                {
                    "message_id":    msg.name,
                    "status":        "Failed",
                    "failure_reason": _STALL_REASON,
                },
                user=msg.owner,
            )
