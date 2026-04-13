import datetime as _dt


def _build_system_persona() -> str:
    today = _dt.date.today().isoformat()
    return (
        "You are a helpful business assistant with access to the company's ERP data. "
        f"Today's date is {today}. Use this to resolve relative date expressions "
        "(e.g. 'last month', 'this year', 'last 7 days') into concrete YYYY-MM-DD "
        "from_date / to_date values when calling tools. "
        "You have tools available to look up specific documents and run data queries. "
        "IMPORTANT: Use the tools proactively. When the user asks about a specific named "
        "record (e.g. an invoice ID, order number, customer name, item code) or requests "
        "aggregated data (e.g. top-selling items, item pairs bought together, low-stock "
        "items), call the appropriate tool IMMEDIATELY — even if no pre-loaded context is "
        "present. The tools perform their own permission checks and will return a clear "
        "message if access is denied. Never refuse a tool-answerable question based on "
        "empty context alone. "
        "For questions that no tool can answer, use only the provided context. "
        "If neither tools nor context apply, say clearly what you cannot help with. "
        "Do not fabricate information."
    )



EMPTY_CONTEXT_NOTE = (
    "[No pre-loaded context was found for this query. "
    "If the user is asking about a specific document or aggregated data, "
    "call the appropriate tool to retrieve it — the tool handles permissions internally. "
    "Only decline if no tool applies to the question.]"
)


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
    messages.append({"role": "user",  "parts": [_build_system_persona()]})
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
    else:
        user_turn = f"{EMPTY_CONTEXT_NOTE}\n\nQuestion: {question}"

    messages.append({"role": "user", "parts": [user_turn]})
    return messages


import re as _re


def _slugify_report_name(name: str) -> str:
    """'Accounts Receivable Summary' → 'run_accounts_receivable_summary'"""
    return "run_" + _re.sub(r"[^a-zA-Z0-9]+", "_", name).lower().strip("_")


def build_report_tool_definitions(
    whitelist_entries: list,
) -> tuple[list | None, dict]:
    """Build one Gemini function-declaration dict per whitelisted report.

    Args:
        whitelist_entries: list of dicts, each with keys:
            report (str), description (str), default_filters (dict),
            filter_meta (list of {fieldname, label, fieldtype, reqd, default})

    Returns:
        tool_list  — list of tool dicts (pass as `tools` to sidecar_client.chat());
                     None when whitelist_entries is empty.
        slug_to_name — {"run_accounts_receivable_summary": "Accounts Receivable Summary", …}
    """
    if not whitelist_entries:
        return None, {}

    _NUMERIC_TYPES = {"Int", "Float", "Currency", "Percent"}
    _DATE_TYPES = {"Date", "Datetime"}

    tool_list = []
    slug_to_name = {}

    for entry in whitelist_entries:
        slug = _slugify_report_name(entry["report"])
        slug_to_name[slug] = entry["report"]

        properties: dict = {}
        required: list = []

        for f in entry.get("filter_meta", []):
            ft = f.get("fieldtype", "Data")
            if ft in _NUMERIC_TYPES:
                json_type = "NUMBER"
            elif ft == "Check":
                json_type = "BOOLEAN"
            else:
                json_type = "STRING"

            desc = f.get("label", f.get("fieldname", ""))
            if ft in _DATE_TYPES:
                desc += " (ISO date: YYYY-MM-DD)"

            default_val = (entry.get("default_filters") or {}).get(f["fieldname"])
            if default_val is not None:
                desc += f' (default: "{default_val}")'

            properties[f["fieldname"]] = {"type": json_type, "description": desc}
            if f.get("mandatory"):
                required.append(f["fieldname"])

        tool_list.append({
            "function_declarations": [{
                "name": slug,
                "description": entry.get("description") or f"Run the {entry['report']} report.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": properties,
                    "required": required,
                },
            }]
        })

    return tool_list, slug_to_name


def build_query_tool_definitions() -> tuple[list | None, dict]:
    """Build one Gemini function-declaration dict per registered query template.

    Returns:
        tool_list        — list of tool dicts (same shape as build_report_tool_definitions);
                           None when QUERY_TEMPLATES is empty.
        slug_to_template — {"execute_record_lookup": "record_lookup", …}
    """
    from frapperag.rag.query_executor import QUERY_TEMPLATES

    if not QUERY_TEMPLATES:
        return None, {}

    tool_list = []
    slug_to_template = {}

    for key, template in QUERY_TEMPLATES.items():
        slug = "execute_" + key
        slug_to_template[slug] = key

        params_schema = template.get("parameters") or {}
        properties: dict = {}
        required: list = []

        for param_name, param_def in params_schema.items():
            properties[param_name] = {
                "type": param_def.get("type", "STRING"),
                "description": param_def.get("description", ""),
            }
            if param_def.get("required"):
                required.append(param_name)

        tool_list.append({
            "function_declarations": [{
                "name": slug,
                "description": template.get("description", f"Execute the {key} query."),
                "parameters": {
                    "type": "OBJECT",
                    "properties": properties,
                    "required": required,
                },
            }]
        })

    return tool_list, slug_to_template
