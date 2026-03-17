import frappe
from frappe.utils import now_datetime, add_to_date
import json


def run_chat_job(message_id: str, session_id: str, user: str, **kwargs):
    """
    Background job entry point. Called by frappe.enqueue (queue="short").
    Site context already initialised by the Frappe worker.

    api_key is read from AI Assistant Settings here — NOT passed via enqueue kwargs
    (keeps credential out of Redis serialisation — same pattern as Phase 1).

    message_id kwarg name avoids collision with Frappe/RQ reserved 'job_id' kwarg.
    """
    from frapperag.rag.retriever      import embed_query, search_all_tables, filter_by_permission
    from frapperag.rag.prompt_builder import build_messages
    from frapperag.rag.chat_engine    import generate_response

    # Read api_key from Settings — never from enqueue kwargs
    api_key = frappe.get_doc("AI Assistant Settings").get_password("gemini_api_key")

    # Enforce the calling user's permission context (Principle III)
    frappe.set_user(user)

    try:
        msg      = frappe.get_doc("Chat Message", message_id)
        question = msg.content

        # 1. Embed the query (RETRIEVAL_QUERY task type)
        query_vector = embed_query(question, api_key)

        # 2. Search all v1_* LanceDB tables
        candidates = search_all_tables(query_vector)

        # 3. Filter by user permissions per-record (Principle III)
        filtered = filter_by_permission(candidates, user)

        # 4. Load last 10 conversation turns (excluding the current Pending message)
        history_docs = frappe.db.get_all(
            "Chat Message",
            filters={"session": session_id, "name": ["!=", message_id]},
            fields=["role", "content"],
            order_by="creation desc",
            limit=10,
            ignore_permissions=False,
        )
        history = [{"role": d.role, "content": d.content} for d in reversed(history_docs)]

        # 5. Build Gemini message list
        messages = build_messages(question, filtered, history)

        # 6. Generate response (gemini-2.5-flash)
        result = generate_response(messages, filtered, api_key)

        # 7. Update the user's Pending message to Completed
        frappe.db.set_value("Chat Message", message_id, {"status": "Completed"})
        frappe.db.commit()

        # 8. Insert assistant reply message
        reply = frappe.get_doc({
            "doctype":     "Chat Message",
            "session":     session_id,
            "role":        "assistant",
            "content":     result["text"],
            "citations":   json.dumps(result["citations"]),
            "status":      "Completed",
            "tokens_used": result["tokens_used"],
        })
        reply.insert(ignore_permissions=True)
        frappe.db.commit()

        # 9. Set session title from first user question (FR-002)
        #    Only written when title is blank — idempotent on retry.
        session = frappe.get_doc("Chat Session", session_id)
        if not session.title:
            frappe.db.set_value("Chat Session", session_id, "title", question[:80].strip())
            frappe.db.commit()

        # 10. Publish realtime response to the user (FR-014)
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

    except Exception:
        import traceback
        tb = traceback.format_exc()
        frappe.db.set_value(
            "Chat Message",
            message_id,
            {"status": "Failed", "error_detail": tb[:2000]},
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
