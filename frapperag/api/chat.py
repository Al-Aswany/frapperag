import frappe
import json


def _assert_session_owner(session_id: str):
    """
    Raises frappe.PermissionError immediately if the caller does not own the session.
    Returns the session doc on success (FR-013).
    """
    if not frappe.db.exists("Chat Session", session_id):
        frappe.throw(f"Chat Session '{session_id}' not found.", frappe.DoesNotExistError)
    session = frappe.get_doc("Chat Session", session_id)
    if session.owner != frappe.session.user:
        frappe.throw("Access denied.", frappe.PermissionError)
    return session


@frappe.whitelist()
def create_session() -> dict:
    """
    Create a new Open Chat Session and return its ID synchronously (FR-018).
    title is left blank — set by run_chat_job() after first successful response (FR-002).
    """
    session = frappe.get_doc({
        "doctype": "Chat Session",
        "status":  "Open",
        "title":   "",
    })
    session.insert(ignore_permissions=True)
    frappe.db.commit()
    return {"session_id": session.name}


@frappe.whitelist()
def send_message(session_id: str, content: str) -> dict:
    """
    Create a Pending Chat Message, enqueue run_chat_job, return message_id immediately.
    Server-side guard: rejects if content is empty or if any Pending message already
    exists in this session (FR-019).
    message_id kwarg avoids Frappe/RQ reserved 'job_id' collision.
    """
    _assert_session_owner(session_id)

    if not content or not content.strip():
        frappe.throw("Message content cannot be empty.", frappe.ValidationError)

    # Server-side enforcement of FR-019 (mirrors UI input lock)
    if frappe.db.exists("Chat Message", {"session": session_id, "status": "Pending"}):
        frappe.throw(
            "A message is already being processed in this session. Please wait.",
            frappe.ValidationError,
        )

    msg = frappe.get_doc({
        "doctype": "Chat Message",
        "session": session_id,
        "role":    "user",
        "content": content,
        "status":  "Pending",
    })
    msg.insert(ignore_permissions=True)
    frappe.db.commit()

    frappe.enqueue(
        "frapperag.rag.chat_runner.run_chat_job",
        queue="short",
        timeout=300,
        site=frappe.local.site,   # explicit site: per-client isolation (Principle II)
        message_id=msg.name,      # NOT job_id — reserved by Frappe/RQ
        session_id=session_id,
        user=frappe.session.user,
        question=content,         # pass content to avoid frappe.get_doc() in the worker
    )
    return {"message_id": msg.name, "status": "Pending"}


@frappe.whitelist()
def list_sessions(include_archived: int = 0) -> dict:
    """Return the current user's chat sessions, newest first."""
    filters = {"owner": frappe.session.user}
    if not int(include_archived):
        filters["status"] = "Open"
    sessions = frappe.db.get_all(
        "Chat Session",
        filters=filters,
        fields=["name", "title", "status", "creation"],
        order_by="creation desc",
        ignore_permissions=False,
    )
    return {"sessions": [dict(s, session_id=s.name) for s in sessions]}


@frappe.whitelist()
def get_messages(session_id: str) -> dict:
    """Return all messages for a session the caller owns, ordered oldest-first."""
    _assert_session_owner(session_id)
    messages = frappe.db.get_all(
        "Chat Message",
        filters={"session": session_id},
        fields=["name", "role", "content", "citations", "status", "tokens_used", "creation"],
        order_by="creation asc",
        ignore_permissions=False,
    )
    return {"messages": [dict(m, message_id=m.name) for m in messages]}


@frappe.whitelist()
def get_message_status(message_id: str) -> dict:
    """
    Return the response status for a pending user message, in the same shape
    as the rag_chat_response realtime event so the JS polling path and the
    realtime path share a single handle_response() function.

    - Pending  → {"message_id", "status": "Pending"}
    - Failed   → {"message_id", "status": "Failed", "failure_reason"}
    - Completed → fetch the assistant reply inserted atomically with the status
                  update and return {"message_id", "status": "Completed",
                  "content", "citations"}.  message_id is always the user
                  message id so the JS closure guard matches correctly.
    """
    msg = frappe.db.get_value(
        "Chat Message",
        message_id,
        ["name", "session", "status", "failure_reason", "modified"],
        as_dict=True,
    )
    if not msg:
        frappe.throw(f"Chat Message '{message_id}' not found.", frappe.DoesNotExistError)
    _assert_session_owner(msg.session)

    if msg.status == "Pending":
        return {"message_id": msg.name, "status": "Pending"}

    if msg.status == "Failed":
        return {
            "message_id": msg.name,
            "status": "Failed",
            "failure_reason": msg.failure_reason or "",
        }

    # Completed: the worker updated the user message and inserted the assistant
    # reply in the same frappe.db.commit(), so both share the same `now`
    # timestamp.  Query by creation >= msg.modified to find the right row.
    reply = frappe.db.get_value(
        "Chat Message",
        {"session": msg.session, "role": "assistant", "creation": [">=", msg.modified]},
        ["content", "citations"],
        as_dict=True,
        order_by="creation asc",
    )
    if reply:
        citations = reply.citations
        if isinstance(citations, str) and citations:
            try:
                citations = json.loads(citations)
            except Exception:
                citations = []
        return {
            "message_id": msg.name,   # user message_id — matches JS closure guard
            "status": "Completed",
            "content": reply.content or "",
            "citations": citations or [],
        }

    # Atomicity guarantee means the reply should always exist at this point,
    # but if somehow absent (e.g. clock skew), keep polling rather than crashing.
    return {"message_id": msg.name, "status": "Pending"}


@frappe.whitelist()
def archive_session(session_id: str) -> dict:
    """Transition a session from Open to Archived (FR-020)."""
    _assert_session_owner(session_id)
    frappe.db.set_value("Chat Session", session_id, "status", "Archived")
    frappe.db.commit()
    return {"session_id": session_id, "status": "Archived"}
