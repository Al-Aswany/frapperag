"""ExecuteQuery tool for the FrappeRAG chat pipeline.

Provides parameterized SQL templates that the AI can invoke as Gemini tool calls.
Each template is a pre-defined query with validated parameters — no raw SQL from
the LLM ever reaches the database.

Called synchronously from chat_runner.run_chat_job() when the AI returns a
tool_call matching an ``execute_*`` slug. Runs inside the frappe.set_user(user)
context already established at job start.

Returns the same envelope shape as report_executor: {"text", "citations", "tokens_used"}.
"""

import frappe

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The 11 DocTypes indexed by text_converter.py — record_lookup is restricted
# to these so the AI cannot attempt lookups on arbitrary DocTypes.
ALLOWED_LOOKUP_DOCTYPES = frozenset([
    "Customer",
    "Item",
    "Sales Invoice",
    "Purchase Invoice",
    "Sales Order",
    "Purchase Order",
    "Delivery Note",
    "Purchase Receipt",
    "Item Price",
    "Stock Entry",
    "Supplier",
])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(msg: str) -> dict:
    """Return a plain-language error envelope (never raises)."""
    return {"text": msg, "citations": [], "tokens_used": 0}


def _validate_int(value, *, name: str, default: int, minimum: int = 1, maximum: int = 50) -> int:
    """Coerce *value* to an int within [minimum, maximum], falling back to *default*."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(v, maximum))


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

# Each entry:
#   "description"          — fed to Gemini as the tool description
#   "parameters"           — Gemini function-parameter schema (type/description/required)
#   "execute"              — callable(args: dict, user: str) -> envelope dict
#   "permission_doctypes"  — list of DocTypes checked before execute; None = dynamic
#
# Templates are added in subsequent commits (record_lookup, top_selling_items,
# best_selling_pairs, low_stock_recent_sales).

QUERY_TEMPLATES: dict = {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def execute_query(args: dict, user: str) -> dict:
    """Execute a parameterised query template.

    Args:
        args:  {"template": "<template_key>", "params": {<template-specific>}}
        user:  frappe.session.user value from the calling job

    Returns one of:
        Success: {"text": str, "citations": [...], "tokens_used": 0}
        Error:   {"text": "<plain-language error>", "citations": [], "tokens_used": 0}

    Never raises — all exceptions are caught and converted to error envelopes.
    """
    template_key = (args or {}).get("template", "")
    params = (args or {}).get("params") or {}

    # Check 1 — template exists
    template = QUERY_TEMPLATES.get(template_key)
    if not template:
        return _error(
            f"I tried to run a query called '{template_key}', but it is not "
            "available. Please rephrase your question."
        )

    # Check 2 — DocType permissions
    permission_doctypes = template.get("permission_doctypes")
    if permission_doctypes:
        for dt in permission_doctypes:
            if not frappe.has_permission(dt, ptype="read", user=user):
                return _error(
                    f"You do not have permission to access {dt} data. "
                    "Please contact your administrator if you need access."
                )

    # Check 3 — execute
    try:
        return template["execute"](params, user)
    except Exception as exc:
        short = str(exc)[:200]
        return _error(
            f"The query '{template_key}' could not be executed: {short}. "
            "Please try again or contact your administrator."
        )
