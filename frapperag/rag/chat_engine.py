DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
GOOGLE_SEARCH_ALLOWED_INTENTS = frozenset({
    "erpnext_help",
    "out_of_scope",
    "web_current_info",
})
TEST_CHAT_MODELS = (
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
)


def get_chat_runtime_settings() -> dict:
    import frappe
    from frappe.utils import cint

    settings = frappe.get_cached_doc("AI Assistant Settings", "AI Assistant Settings")
    model = (getattr(settings, "chat_model", None) or "").strip() or DEFAULT_CHAT_MODEL

    return {
        "model": model,
        "google_search_enabled": bool(cint(getattr(settings, "enable_chat_google_search", 0) or 0)),
        "google_search_allowed_intents": sorted(GOOGLE_SEARCH_ALLOWED_INTENTS),
        "testing_models": list(TEST_CHAT_MODELS),
    }


def _should_enable_google_search(runtime_intent: str | None, has_erp_context: bool, runtime: dict) -> bool:
    return bool(
        runtime_intent
        and runtime["google_search_enabled"]
        and runtime_intent in GOOGLE_SEARCH_ALLOWED_INTENTS
        and not has_erp_context
    )


def debug_chat_runtime_settings(intent: str | None = None, has_erp_context: int = 0) -> dict:
    runtime = get_chat_runtime_settings()
    runtime["google_search_would_be_used"] = _should_enable_google_search(
        intent,
        bool(int(has_erp_context)),
        runtime,
    )
    runtime["intent"] = intent
    runtime["has_erp_context"] = bool(int(has_erp_context))
    return runtime


def generate_response(
    messages: list,
    context_records: list,
    api_key: str,
    tools: list | None = None,
    runtime_intent: str | None = None,
) -> dict:
    """
    Call the configured Gemini chat model via the RAG sidecar's /chat endpoint.

    Returns one of:
        RAG path:    {"text": str, "citations": [{doctype, name}], "tokens_used": int}
        Report path: {"tool_call": {"name": str, "args": dict}, "citations": [],
                      "tokens_used": int}

    The Gemini SDK runtime is initialized inside the sidecar process and reused
    across calls. Workers communicate via HTTP only; no Gemini SDK import here.

    Rate-limit handling (FR-015) is performed inside the sidecar:
      HTTP 429 → one short backoff retry.
      All other errors surface as SidecarError and propagate immediately.

    tools: optional list of per-report function-declaration dicts built by
      prompt_builder.build_report_tool_definitions(). Passed through to the sidecar
      which constructs google-genai tool objects. None when whitelist is empty.
    """
    import time
    import frappe
    from frapperag.rag.sidecar_client import chat as sidecar_chat

    runtime = get_chat_runtime_settings()
    google_search = None
    if _should_enable_google_search(runtime_intent, bool(context_records), runtime):
        google_search = {"enabled": True, "intent": runtime_intent}

    t0 = time.monotonic()
    result = sidecar_chat(
        messages=messages,
        api_key=api_key,
        model=runtime["model"],
        tools=tools,
        google_search=google_search,
    )
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
