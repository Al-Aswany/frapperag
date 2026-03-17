CHAT_MODEL       = "gemini-2.5-flash"
RATE_LIMIT_SLEEP = 60.0  # seconds; matches Phase 1 embedder pattern (FR-015)


def generate_response(messages: list, context_records: list, api_key: str) -> dict:
    """
    Call gemini-2.5-flash with the assembled message list.
    Returns {"text": str, "citations": [{doctype, name}], "tokens_used": int}.

    Rate-limit handling (FR-015):
      ResourceExhausted → 60s flat sleep → one retry.
      All other exceptions propagate immediately (non-transient failure — fail fast).

    All google.generativeai imports inside function — no module-level state.
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(CHAT_MODEL)

    # history = all messages except the final user turn
    history      = messages[:-1]
    last_message = messages[-1]["parts"][0]
    chat         = model.start_chat(history=history)

    response = None
    for attempt in range(2):   # one retry on rate-limit only
        try:
            response = chat.send_message(last_message)
            break
        except ResourceExhausted:
            if attempt == 0:
                time.sleep(RATE_LIMIT_SLEEP)
                continue
            raise
        # All other exceptions propagate immediately — non-transient

    text        = response.text
    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = getattr(response.usage_metadata, "total_token_count", 0)

    # Build deduplicated citation list from permission-filtered context records
    seen      = set()
    citations = []
    for r in context_records:
        key = (r["doctype"], r["name"])
        if key not in seen:
            seen.add(key)
            citations.append({"doctype": r["doctype"], "name": r["name"]})

    return {"text": text, "citations": citations, "tokens_used": tokens_used}
