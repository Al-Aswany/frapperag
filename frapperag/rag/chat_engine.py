CHAT_MODEL = "gemini-2.5-flash"


def generate_response(
    messages: list,
    context_records: list,
    api_key: str,
    tools: list | None = None,
) -> dict:
    """
    Call gemini-2.5-flash via the RAG sidecar's /chat endpoint.

    Returns one of:
        RAG path:    {"text": str, "citations": [{doctype, name}], "tokens_used": int}
        Report path: {"tool_call": {"name": str, "args": dict}, "citations": [],
                      "tokens_used": int}

    The Gemini SDK (google.generativeai) is initialized once in the sidecar process
    and reused across calls — no cold-start overhead per job.
    Workers communicate via HTTP only; no google.generativeai import here.

    Rate-limit handling (FR-015) is performed inside the sidecar:
      ResourceExhausted → 60s flat sleep → one retry.
      All other errors surface as SidecarError and propagate immediately.

    tools: optional list of per-report function-declaration dicts built by
      prompt_builder.build_report_tool_definitions(). Passed through to the sidecar
      which constructs genai.types.Tool objects. None when whitelist is empty.
    """
    import time
    import frappe
    from frapperag.rag.sidecar_client import chat as sidecar_chat

    t0 = time.monotonic()
    result = sidecar_chat(messages=messages, api_key=api_key, model=CHAT_MODEL, tools=tools)
    frappe.logger("frapperag").info(
        "[TIMING][chat_engine] sidecar /chat %.3fs", time.monotonic() - t0
    )

    tokens_used = result.get("tokens_used", 0)

    # Branch: report path (AI returned a function call)
    if "tool_call" in result:
        return {
            "tool_call": result["tool_call"],
            "citations": [],
            "tokens_used": tokens_used,
        }

    # Existing RAG path — build deduplicated citation list.
    # Suppress doc citations when the model declines the question: the retrieved
    # records were not used to answer, so surfacing them as citations is misleading
    # and produces a raw-data wall in the UI.
    text = result["text"]

    _DECLINE_PREFIXES = (
        "I am sorry", "I'm sorry", "I cannot", "I can't",
        "I am unable", "I'm unable", "Unfortunately",
        "I do not have", "I don't have",
    )
    is_decline = text.strip().startswith(_DECLINE_PREFIXES)

    citations = []
    if not is_decline:
        seen = set()
        for r in context_records:
            key = (r["doctype"], r["name"])
            if key not in seen:
                seen.add(key)
                citations.append({"type": "doc", "doctype": r["doctype"], "name": r["name"]})

    return {"text": text, "citations": citations, "tokens_used": tokens_used}
