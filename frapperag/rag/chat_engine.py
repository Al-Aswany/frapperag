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

    # Existing RAG path — build deduplicated citation list
    text = result["text"]
    seen = set()
    citations = []
    for r in context_records:
        key = (r["doctype"], r["name"])
        if key not in seen:
            seen.add(key)
            citations.append({"type": "doc", "doctype": r["doctype"], "name": r["name"]})

    return {"text": text, "citations": citations, "tokens_used": tokens_used}
