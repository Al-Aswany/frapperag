"""ExecuteQuery tool for the FrappeRAG chat pipeline.

Provides parameterized SQL templates that the AI can invoke as Gemini tool calls.
Each template is a pre-defined query with validated parameters — no raw SQL from
the LLM ever reaches the database.

Called synchronously from chat_runner.run_chat_job() when the AI returns a
tool_call matching an ``execute_*`` slug. Runs inside the frappe.set_user(user)
context already established at job start.

Returns the same envelope shape as report_executor: {"text", "citations", "tokens_used"}.
"""

import datetime
from decimal import Decimal

import frappe

from frapperag.rag.text_converter import to_text

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


_json_primitives = (type(None), bool, int, float, str)


def _safe(v):
    """Coerce a value to a JSON-safe type (mirrors report_executor._safe)."""
    if isinstance(v, _json_primitives):
        return v
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return str(v)


# ---------------------------------------------------------------------------
# Template execute functions
# ---------------------------------------------------------------------------


def _execute_record_lookup(params: dict, user: str) -> dict:
    """Look up a single Frappe document by DocType + name."""
    doctype = (params.get("doctype") or "").strip()
    name = (params.get("name") or "").strip()

    if not doctype:
        return _error("Please specify a DocType (e.g. 'Sales Invoice', 'Customer').")
    if not name:
        return _error("Please specify the document name or ID to look up.")
    if doctype not in ALLOWED_LOOKUP_DOCTYPES:
        supported = ", ".join(sorted(ALLOWED_LOOKUP_DOCTYPES))
        return _error(
            f"'{doctype}' is not a supported DocType for lookup. "
            f"Supported types are: {supported}."
        )

    # Dynamic permission check — frappe.has_permission(doctype, doc=name) respects
    # the full Frappe permission system including role permissions and owner checks.
    if not frappe.has_permission(doctype, doc=name, ptype="read", user=user):
        return _error(
            f"You do not have permission to view this {doctype} record. "
            "Please contact your administrator if you need access."
        )

    try:
        doc = frappe.get_doc(doctype, name)
    except frappe.DoesNotExistError:
        return _error(f"No {doctype} found with the name or ID '{name}'.")

    doc_dict = doc.as_dict()

    # Build the narrative using text_converter — same text the vector store indexed.
    narrative = to_text(doctype, doc_dict)
    if not narrative:
        # Fallback for any DocType that to_text doesn't cover (shouldn't happen
        # given ALLOWED_LOOKUP_DOCTYPES == SUPPORTED_DOCTYPES, but be safe).
        narrative = f"Here are the details for {doctype} '{name}':"

    # Coerce all field values to JSON-safe types.
    safe_fields = {k: _safe(v) for k, v in doc_dict.items() if not k.startswith("__")}

    citation = {
        "type": "record_detail",
        "doctype": doctype,
        "name": name,
        "fields": safe_fields,
    }
    return {"text": narrative, "citations": [citation], "tokens_used": 0}


def _execute_top_selling_items(params: dict, user: str) -> dict:
    """Return the top-N items by quantity or revenue across submitted Sales Invoices."""
    import datetime as _dt

    top_n = _validate_int(params.get("top_n"), name="top_n", default=10, minimum=1, maximum=50)
    sort_by = params.get("sort_by", "qty")
    if sort_by not in ("qty", "amount"):
        sort_by = "qty"

    # Resolve date range — default: last 12 months
    today = _dt.date.today()
    default_from = (today.replace(year=today.year - 1) if today.month != 2 or today.day != 29
                    else today.replace(year=today.year - 1, day=28)).isoformat()
    from_date = (params.get("from_date") or default_from).strip()
    to_date = (params.get("to_date") or today.isoformat()).strip()

    order_col = "total_qty" if sort_by == "qty" else "total_amount"

    rows = frappe.db.sql(
        f"""
        SELECT
            sii.item_code,
            sii.item_name,
            SUM(sii.qty)    AS total_qty,
            SUM(sii.amount) AS total_amount
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY sii.item_code, sii.item_name
        ORDER BY {order_col} DESC
        LIMIT %(top_n)s
        """,
        {"from_date": from_date, "to_date": to_date, "top_n": top_n},
        as_dict=True,
    )

    if not rows:
        return _error(
            f"No sales data found between {from_date} and {to_date}. "
            "Check that Sales Invoices have been submitted in that date range."
        )

    columns = ["Item Code", "Item Name", "Total Qty Sold", "Total Revenue"]
    safe_rows = [
        [_safe(r["item_code"]), _safe(r["item_name"]),
         _safe(r["total_qty"]), _safe(r["total_amount"])]
        for r in rows
    ]

    sort_label = "quantity" if sort_by == "qty" else "revenue"
    text = (
        f"Here are the top {len(rows)} selling items by {sort_label} "
        f"from {from_date} to {to_date}:"
    )
    citation = {
        "type": "query_result",
        "template": "top_selling_items",
        "columns": columns,
        "rows": safe_rows,
        "row_count": len(safe_rows),
    }
    return {"text": text, "citations": [citation], "tokens_used": 0}


def _execute_best_selling_pairs(params: dict, user: str) -> dict:
    """Find pairs of items most frequently bought together in the same Sales Invoice."""
    import datetime as _dt

    top_n = _validate_int(params.get("top_n"), name="top_n", default=10, minimum=1, maximum=50)

    today = _dt.date.today()
    default_from = (today.replace(year=today.year - 1) if today.month != 2 or today.day != 29
                    else today.replace(year=today.year - 1, day=28)).isoformat()
    from_date = (params.get("from_date") or default_from).strip()
    to_date = (params.get("to_date") or today.isoformat()).strip()

    # Self-join: a.item_code < b.item_code ensures each unordered pair counted once.
    # date filter on the parent SI guards query cost on large datasets.
    rows = frappe.db.sql(
        """
        SELECT
            a.item_code  AS item_a,
            a.item_name  AS item_a_name,
            b.item_code  AS item_b,
            b.item_name  AS item_b_name,
            COUNT(*)     AS times_sold_together
        FROM `tabSales Invoice Item` a
        JOIN `tabSales Invoice Item` b
          ON a.parent = b.parent AND a.item_code < b.item_code
        JOIN `tabSales Invoice` si ON si.name = a.parent
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY a.item_code, b.item_code
        ORDER BY times_sold_together DESC
        LIMIT %(top_n)s
        """,
        {"from_date": from_date, "to_date": to_date, "top_n": top_n},
        as_dict=True,
    )

    if not rows:
        return _error(
            f"No co-purchase data found between {from_date} and {to_date}. "
            "This may mean no Sales Invoice contained more than one item in that period."
        )

    columns = ["Item A", "Item A Name", "Item B", "Item B Name", "Times Sold Together"]
    safe_rows = [
        [_safe(r["item_a"]), _safe(r["item_a_name"]),
         _safe(r["item_b"]), _safe(r["item_b_name"]),
         _safe(r["times_sold_together"])]
        for r in rows
    ]

    text = (
        f"Here are the top {len(rows)} item pairs most frequently sold together "
        f"from {from_date} to {to_date}:"
    )
    citation = {
        "type": "query_result",
        "template": "best_selling_pairs",
        "columns": columns,
        "rows": safe_rows,
        "row_count": len(safe_rows),
    }
    return {"text": text, "citations": [citation], "tokens_used": 0}


# ---------------------------------------------------------------------------
# Template registry
# ---------------------------------------------------------------------------

# Each entry:
#   "description"          — fed to Gemini as the tool description
#   "parameters"           — Gemini function-parameter schema (type/description/required)
#   "execute"              — callable(args: dict, user: str) -> envelope dict
#   "permission_doctypes"  — list of DocTypes checked before execute; None = dynamic
#
# Additional templates added in subsequent commits (top_selling_items,
# best_selling_pairs, low_stock_recent_sales).

QUERY_TEMPLATES: dict = {
    "record_lookup": {
        "description": (
            "Look up the full details of a specific document by its DocType and "
            "name/ID. Use this when the user asks about a specific invoice, order, "
            "customer, item, or other named record (e.g. 'What is SINV-IR-00657?', "
            "'Show me customer C-00042', 'Tell me about item ITEM-001')."
        ),
        "parameters": {
            "doctype": {
                "type": "STRING",
                "description": (
                    "The Frappe DocType of the record, e.g. 'Sales Invoice', "
                    "'Customer', 'Item', 'Purchase Invoice', 'Sales Order', "
                    "'Purchase Order', 'Delivery Note', 'Purchase Receipt', "
                    "'Item Price', 'Stock Entry', 'Supplier'."
                ),
                "required": True,
            },
            "name": {
                "type": "STRING",
                "description": "The document name or ID, e.g. 'SINV-IR-00657' or 'C-00042'.",
                "required": True,
            },
        },
        "execute": _execute_record_lookup,
        "permission_doctypes": None,  # checked dynamically inside _execute_record_lookup
    },

    "top_selling_items": {
        "description": (
            "Return the top-selling items ranked by quantity sold or total revenue "
            "across submitted Sales Invoices. Use this when the user asks about "
            "best-selling products, top items by sales, or most popular items "
            "(e.g. 'What are the top 10 selling items?', 'Which items sell the most?')."
        ),
        "parameters": {
            "top_n": {
                "type": "NUMBER",
                "description": "How many items to return (default 10, max 50).",
                "required": False,
            },
            "sort_by": {
                "type": "STRING",
                "description": "Rank by 'qty' (quantity sold) or 'amount' (revenue). Default: 'qty'.",
                "required": False,
            },
            "from_date": {
                "type": "STRING",
                "description": "Start of date range (ISO date: YYYY-MM-DD). Default: 12 months ago.",
                "required": False,
            },
            "to_date": {
                "type": "STRING",
                "description": "End of date range (ISO date: YYYY-MM-DD). Default: today.",
                "required": False,
            },
        },
        "execute": _execute_top_selling_items,
        "permission_doctypes": ["Sales Invoice"],
    },

    "best_selling_pairs": {
        "description": (
            "Find pairs of items that are most frequently purchased together in the "
            "same Sales Invoice. Use this when the user asks about items bought "
            "together, product bundles, or co-purchase patterns "
            "(e.g. 'What are the top 2 items sold together?', "
            "'Which items are commonly bought together?')."
        ),
        "parameters": {
            "top_n": {
                "type": "NUMBER",
                "description": "How many item pairs to return (default 10, max 50).",
                "required": False,
            },
            "from_date": {
                "type": "STRING",
                "description": "Start of date range (ISO date: YYYY-MM-DD). Default: 12 months ago.",
                "required": False,
            },
            "to_date": {
                "type": "STRING",
                "description": "End of date range (ISO date: YYYY-MM-DD). Default: today.",
                "required": False,
            },
        },
        "execute": _execute_best_selling_pairs,
        "permission_doctypes": ["Sales Invoice"],
    },
}


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
