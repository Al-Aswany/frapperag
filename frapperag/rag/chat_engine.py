CHAT_MODEL = "gemini-2.5-flash"


def generate_response(messages: list, context_records: list, api_key: str) -> dict:
    """
    Call gemini-2.5-flash via the RAG sidecar's /chat endpoint.
    Returns {"text": str, "citations": [{doctype, name}], "tokens_used": int}.

    The Gemini SDK (google.generativeai) is initialized once in the sidecar process
    and reused across calls — no cold-start overhead per job.
    Workers communicate via HTTP only; no google.generativeai import here.

    Rate-limit handling (FR-015) is performed inside the sidecar:
      ResourceExhausted → 60s flat sleep → one retry.
      All other errors surface as SidecarError and propagate immediately.
    """
    import time
    import frappe
    from frapperag.rag.sidecar_client import chat as sidecar_chat

    t0 = time.monotonic()
    result = sidecar_chat(messages=messages, api_key=api_key, model=CHAT_MODEL)
    frappe.logger("frapperag").info(
        "[TIMING][chat_engine] sidecar /chat %.3fs", time.monotonic() - t0
    )

    text        = result["text"]
    tokens_used = result.get("tokens_used", 0)

    # Build deduplicated citation list from permission-filtered context records
    seen      = set()
    citations = []
    for r in context_records:
        key = (r["doctype"], r["name"])
        if key not in seen:
            seen.add(key)
            citations.append({"doctype": r["doctype"], "name": r["name"]})

    return {"text": text, "citations": citations, "tokens_used": tokens_used}
