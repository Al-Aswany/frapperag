import time as _time

import frappe
from frappe.utils import now_datetime, add_to_date
import json


def _log():
    """Return a site-aware logger with INFO level guaranteed."""
    logger = frappe.logger("frapperag", allow_site=True, file_count=5, max_size=250_000)
    logger.setLevel("INFO")
    return logger


def run_chat_job(message_id: str, session_id: str, user: str, question: str = "", **kwargs):
    """
    Background job entry point. Called by frappe.enqueue (queue="short").
    Site context already initialised by the Frappe worker.

    api_key is read from AI Assistant Settings here — NOT passed via enqueue kwargs
    (keeps credential out of Redis serialisation — same pattern as Phase 1).

    message_id kwarg name avoids collision with Frappe/RQ reserved 'job_id' kwarg.
    """
    from frapperag.rag.retriever      import search_candidates, filter_by_permission
    from frapperag.rag.prompt_builder import build_messages
    from frapperag.rag.chat_engine    import generate_response

    job_start = _time.monotonic()
    _log().info(f"[TIMING][{message_id}] run_chat_job START")

    # Read api_key from Settings — never from enqueue kwargs
    t0 = _time.monotonic()
    api_key = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings").get_password("gemini_api_key")
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

        # Close the implicit transaction opened by settings_read before the heavy I/O.
        # All subsequent work (search + generate) runs outside any open transaction,
        # so the final writes are in a clean, minimal transaction.
        t0 = _time.monotonic()
        frappe.db.commit()
        _log().info(f"[TIMING][{message_id}] pre_commit {_time.monotonic() - t0:.3f}s")

        # 1+2. Embed query + search all v3_* tables via sidecar (single HTTP call)
        t0 = _time.monotonic()
        candidates = search_candidates(question)
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

        # 6. Generate response (gemini-2.5-flash)
        t0 = _time.monotonic()
        result = generate_response(messages, filtered, api_key)
        _log().info(
            f"[TIMING][{message_id}] generate_response {_time.monotonic() - t0:.3f}s"
            f" tokens_used={result['tokens_used']}"
        )

        # 7. Update the user's Pending message to Completed (bypass ORM lifecycle)
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
        t0 = _time.monotonic()
        reply_name = frappe.generate_hash(length=10)
        frappe.db.sql(
            """INSERT INTO `tabChat Message`
               (name, creation, modified, modified_by, owner, docstatus, idx,
                session, role, content, citations, status, tokens_used)
               VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s, %s, %s, %s)""",
            (
                reply_name, now, now, user, user,
                session_id, "assistant", result["text"],
                json.dumps(result["citations"]), "Completed", result["tokens_used"],
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
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id":  message_id,
                "session_id":  session_id,
                "status":      "Completed",
                "content":     result["text"],
                "citations":   result["citations"],
                "tokens_used": result["tokens_used"],
            },
            user=user,
            after_commit=False,
        )
        _log().info(f"[TIMING][{message_id}] publish_realtime {_time.monotonic() - t0:.3f}s")

        _log().info(
            f"[TIMING][{message_id}] run_chat_job DONE total={_time.monotonic() - job_start:.3f}s"
        )

    except Exception:
        import traceback
        tb = traceback.format_exc()
        _log().error(
            f"[TIMING][{message_id}] run_chat_job FAILED after {_time.monotonic() - job_start:.3f}s"
        )
        frappe.db.sql(
            """UPDATE `tabChat Message`
               SET status = %s, error_detail = %s, modified = %s, modified_by = %s
               WHERE name = %s""",
            ("Failed", tb[:2000], now_datetime(), user, message_id),
        )
        frappe.db.commit()
        frappe.publish_realtime(
            event="rag_chat_response",
            message={
                "message_id": message_id,
                "session_id": session_id,
                "status":     "Failed",
                "error":      tb[:500],
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
        pluck="name",
    )
    for name in stalled:
        frappe.db.set_value(
            "Chat Message",
            name,
            {
                "status":       "Failed",
                "error_detail": "Message exceeded 10-minute processing timeout. Worker may have crashed.",
            },
        )
    if stalled:
        frappe.db.commit()
