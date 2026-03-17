SYSTEM_PERSONA = (
    "You are a helpful business assistant with access to the company's ERP data. "
    "Answer questions based only on the provided context. "
    "If the context is empty or insufficient, say so clearly — do not fabricate information. "
    "When referencing source documents, mention them by type and identifier."
)

EMPTY_CONTEXT_NOTE = (
    "[No accessible context was found for this query. "
    "The user may not have permission to view relevant records, "
    "or no data has been indexed yet. Respond helpfully but do not invent information.]"
)

CONVERSATIONAL_NOTE = (
    "[This is a conversational message with no specific ERP data request. "
    "Respond naturally and helpfully as a business assistant.]"
)

# ERP-specific terms that signal the user is asking about business data.
# Any match → treat as a data query (use EMPTY_CONTEXT_NOTE when no results found).
# No match  → treat as conversational (use CONVERSATIONAL_NOTE).
_ERP_KEYWORDS = frozenset({
    "customer", "customers", "invoice", "invoices", "sales", "order", "orders",
    "item", "items", "product", "products", "payment", "payments", "stock",
    "supplier", "suppliers", "employee", "employees", "account", "accounts",
    "balance", "amount", "report", "reports", "purchase", "purchases",
    "delivery", "quotation", "lead", "opportunity", "expense", "budget",
    "tax", "ledger", "journal", "outstanding", "due", "receipt", "shipment",
    "warehouse", "revenue", "profit", "loss", "transaction", "vendor",
    "contract", "price", "cost", "total", "quantity", "inventory",
})


def _is_conversational(question: str) -> bool:
    """Return True when the question contains no ERP-specific keywords."""
    words = set(question.lower().split())
    return not bool(words & _ERP_KEYWORDS)


def build_messages(question: str, context_records: list, history: list) -> list:
    """
    Assemble the Gemini message list for start_chat(history=...) + send_message().

    Args:
        question:        The user's current question.
        context_records: Permission-filtered retrieval results [{doctype, name, text}].
                         May be empty (FR-012: EMPTY_CONTEXT_NOTE injected instead).
        history:         Last <= 10 prior turns [{role: "user"|"assistant", content: str}].

    Returns: list of {"role": "user"|"model", "parts": [str]} dicts.
    The last item is the current user turn (passed to send_message()).
    """
    messages = []

    # Priming exchange: sets system persona (synthetic user/model opening turn)
    messages.append({"role": "user",  "parts": [SYSTEM_PERSONA]})
    messages.append({"role": "model", "parts": ["Understood. I will answer based only on provided context."]})

    # Conversation history (oldest-first, max 10 turns)
    for turn in history[-10:]:
        role = "model" if turn["role"] == "assistant" else "user"
        messages.append({"role": role, "parts": [turn["content"]]})

    # Context block + current question (final user turn)
    if context_records:
        context_text = "\n\n".join(
            f"[{r['doctype']} / {r['name']}]\n{r['text']}"
            for r in context_records
        )
        user_turn = f"Context from ERP data:\n{context_text}\n\nQuestion: {question}"
    elif _is_conversational(question):
        user_turn = f"{CONVERSATIONAL_NOTE}\n\nMessage: {question}"
    else:
        user_turn = f"{EMPTY_CONTEXT_NOTE}\n\nQuestion: {question}"

    messages.append({"role": "user", "parts": [user_turn]})
    return messages
