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

from frapperag.rag.text_converter import to_text, to_brief_summary

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
# Curated field sets for record_detail citations
# ---------------------------------------------------------------------------

# Per-DocType config: which header fields to expose and how to extract items.
# Field types Code / Text Editor / HTML / Long Text are never included.
_CURATED: dict = {
    "Purchase Order": {
        "header": ["supplier", "supplier_name", "transaction_date", "schedule_date",
                   "status", "grand_total", "currency", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Sales Invoice": {
        "header": ["customer", "customer_name", "posting_date", "due_date",
                   "status", "grand_total", "currency", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Sales Order": {
        "header": ["customer", "customer_name", "transaction_date", "delivery_date",
                   "status", "grand_total", "currency", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Purchase Invoice": {
        "header": ["supplier", "supplier_name", "posting_date", "due_date",
                   "status", "grand_total", "currency", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Delivery Note": {
        "header": ["customer", "customer_name", "posting_date", "status", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Purchase Receipt": {
        "header": ["supplier", "supplier_name", "posting_date", "status", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "rate", "amount"],
    },
    "Stock Entry": {
        "header": ["stock_entry_type", "posting_date", "from_warehouse",
                   "to_warehouse", "company"],
        "items_field": "items",
        "item_fields": ["item_code", "item_name", "qty", "basic_rate", "amount",
                        "s_warehouse", "t_warehouse"],
    },
    "Customer": {
        "header": ["customer_name", "customer_group", "territory",
                   "customer_type", "default_currency"],
        "items_field": None,
    },
    "Supplier": {
        "header": ["supplier_name", "supplier_group", "supplier_type", "country"],
        "items_field": None,
    },
    "Item": {
        "header": ["item_name", "item_group", "stock_uom", "item_type",
                   "valuation_rate", "standard_rate"],
        "items_field": None,
    },
    "Item Price": {
        "header": ["item_code", "item_name", "price_list", "price_list_rate",
                   "currency", "valid_from", "valid_upto"],
        "items_field": None,
    },
}


def _curate_fields(doctype: str, doc_dict: dict) -> dict:
    """Return a curated, JSON-safe field set for the citation.

    Header fields come from the per-DocType allowlist in _CURATED.
    The items child table (if present) is extracted as a structured array
    [{item_code, item_name, qty, rate, amount}] — never a raw string.
    Falls back to a safe dump of scalar primitives when the DocType is not
    in the curated map.
    """
    config = _CURATED.get(doctype)
    if not config:
        # Fallback: expose only JSON primitives, skip child tables and privates.
        return {
            k: _safe(v)
            for k, v in doc_dict.items()
            if not k.startswith("_") and isinstance(v, _json_primitives)
        }

    result: dict = {}

    # Header fields
    for field in config["header"]:
        val = doc_dict.get(field)
        if val is None or val == "":
            continue
        result[field] = _safe(val)

    # Items child table → structured array
    items_field = config.get("items_field")
    if items_field:
        raw_items = doc_dict.get(items_field) or []
        item_fields = config.get("item_fields", [])
        curated_items = []
        for row in raw_items:
            if not isinstance(row, dict):
                continue
            entry = {}
            for f in item_fields:
                v = row.get(f)
                if v is not None and v != "":
                    entry[f] = _safe(v)
            if entry:
                curated_items.append(entry)
        if curated_items:
            result["items"] = curated_items

    return result


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

    # Use a brief header-only summary as the tool-result narrative so the LLM
    # response stays concise.  Full item details are in the structured citation.
    # to_text() (verbose, with all items) is used only by the vector indexer.
    narrative = to_brief_summary(doctype, doc_dict)
    if not narrative:
        narrative = f"Here are the details for {doctype} '{name}':"

    citation = {
        "type": "record_detail",
        "doctype": doctype,
        "name": name,
        "fields": _curate_fields(doctype, doc_dict),
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


def _execute_low_stock_recent_sales(params: dict, user: str) -> dict:
    """Items sold recently that are now low or out of stock."""
    import datetime as _dt

    months = _validate_int(params.get("months"), name="months", default=6, minimum=1, maximum=12)
    top_n  = _validate_int(params.get("top_n"),  name="top_n",  default=10, minimum=1, maximum=50)

    today     = _dt.date.today()
    from_date = (today - _dt.timedelta(days=months * 30)).isoformat()
    to_date   = today.isoformat()

    # LEFT JOIN tabBin so items with no bin row (never stocked) also appear.
    # COALESCE handles NULL actual_qty from missing bin rows.
    # SUM(sii.qty) is the total sold in the window — used for urgency ordering.
    rows = frappe.db.sql(
        """
        SELECT
            sii.item_code,
            MAX(sii.item_name)             AS item_name,
            SUM(sii.qty)                   AS qty_sold,
            COALESCE(SUM(b.actual_qty), 0) AS current_stock
        FROM `tabSales Invoice Item` sii
        JOIN `tabSales Invoice` si ON si.name = sii.parent
        LEFT JOIN `tabBin` b ON b.item_code = sii.item_code
        WHERE si.docstatus = 1
          AND si.posting_date BETWEEN %(from_date)s AND %(to_date)s
        GROUP BY sii.item_code
        HAVING COALESCE(SUM(b.actual_qty), 0) < SUM(sii.qty)
        ORDER BY (SUM(sii.qty) - COALESCE(SUM(b.actual_qty), 0)) DESC
        LIMIT %(top_n)s
        """,
        {"from_date": from_date, "to_date": to_date, "top_n": top_n},
        as_dict=True,
    )

    if not rows:
        return _error(
            f"No low-stock items found for the last {months} month(s). "
            "Either stock levels are sufficient or no Sales Invoices exist in that period."
        )

    columns = ["Item Code", "Item Name", "Qty Sold (Period)", "Current Stock", "Shortfall"]
    safe_rows = [
        [
            _safe(r["item_code"]),
            _safe(r["item_name"]),
            _safe(r["qty_sold"]),
            _safe(r["current_stock"]),
            _safe(float(r["qty_sold"] or 0) - float(r["current_stock"] or 0)),
        ]
        for r in rows
    ]

    text = (
        f"Here are the top {len(rows)} items sold in the last {months} month(s) "
        f"that are now low or out of stock (sorted by shortfall):"
    )
    citation = {
        "type": "query_result",
        "template": "low_stock_recent_sales",
        "columns": columns,
        "rows": safe_rows,
        "row_count": len(safe_rows),
    }
    return {"text": text, "citations": [citation], "tokens_used": 0}


# ---------------------------------------------------------------------------
# Aggregate DocType helpers
# ---------------------------------------------------------------------------

_ALLOWED_AGG_FNS = frozenset(["COUNT", "SUM", "AVG", "MIN", "MAX"])


def _load_aggregate_allowlists() -> dict:
    """Load per-DocType aggregate allowlists from AI Assistant Settings.

    Returns a dict keyed by doctype_name::

        {
            "Purchase Invoice": {
                "date_field":       "posting_date",   # or None → date filters rejected
                "group_by_fields":  frozenset({"status", "supplier"}),
                "aggregate_fields": frozenset({"grand_total"}),
            },
            ...
        }

    Returns an empty dict when settings are not accessible (fail-closed).
    """
    try:
        settings = frappe.get_cached_doc("AI Assistant Settings")
    except Exception:
        return {}

    date_field_map: dict[str, str | None] = {
        row.doctype_name: (row.date_field or None)
        for row in (settings.allowed_doctypes or [])
    }

    result: dict = {}
    for row in (settings.aggregate_fields or []):
        dt = row.doctype_name
        if dt not in result:
            result[dt] = {
                "date_field": date_field_map.get(dt),
                "group_by_fields": set(),
                "aggregate_fields": set(),
            }
        if row.allow_group_by:
            result[dt]["group_by_fields"].add(row.fieldname)
        if row.allow_aggregate:
            result[dt]["aggregate_fields"].add(row.fieldname)

    for dt in result:
        result[dt]["group_by_fields"] = frozenset(result[dt]["group_by_fields"])
        result[dt]["aggregate_fields"] = frozenset(result[dt]["aggregate_fields"])

    return result


def _execute_aggregate_doctype(params: dict, user: str) -> dict:
    """Parametric aggregate query against an admin-allowlisted DocType.

    Identifiers (table name, field names) are taken exclusively from the
    admin-configured allowlist and inserted via f-string into the SQL.
    User-supplied filter *values* use %(name)s parameterized placeholders.
    """
    import datetime as _dt

    doctype         = (params.get("doctype")         or "").strip()
    group_by        = (params.get("group_by")        or "").strip()
    aggregate_field = (params.get("aggregate_field") or "").strip()
    aggregate_fn    = (params.get("aggregate_fn")    or "COUNT").strip().upper()
    from_date       = (params.get("from_date")       or "").strip()
    to_date         = (params.get("to_date")         or "").strip()
    status          = (params.get("status")          or "").strip()
    filter_field    = (params.get("filter_field")    or "").strip()
    filter_value    = (params.get("filter_value")    or "").strip()
    order_dir       = (params.get("order_by")        or "desc").strip().lower()
    limit           = _validate_int(params.get("limit"), name="limit", default=10, minimum=1, maximum=50)

    if not doctype:
        return _error("Please specify a DocType to query (e.g. 'Purchase Invoice').")

    # --- allowlist gate --------------------------------------------------
    allowlists = _load_aggregate_allowlists()
    dt_config = allowlists.get(doctype)
    if dt_config is None:
        return _error(
            f"Aggregate queries on '{doctype}' are not configured. "
            "Ask your administrator to add it to AI Assistant Settings → Aggregate Fields."
        )

    # --- DocType-level permission ----------------------------------------
    if not frappe.has_permission(doctype, ptype="read", user=user):
        return _error(
            f"You do not have permission to access {doctype} data. "
            "Please contact your administrator if you need access."
        )

    # --- validate aggregate_fn ------------------------------------------
    if aggregate_fn not in _ALLOWED_AGG_FNS:
        aggregate_fn = "COUNT"

    # --- validate group_by ----------------------------------------------
    if group_by and group_by not in dt_config["group_by_fields"]:
        allowed = ", ".join(sorted(dt_config["group_by_fields"])) or "none configured"
        return _error(
            f"Grouping by '{group_by}' is not allowed for {doctype}. "
            f"Allowed group-by fields: {allowed}."
        )

    # --- validate filter_field / filter_value ---------------------------
    if filter_field and not filter_value:
        return _error("filter_value is required when filter_field is specified.")
    if filter_value and not filter_field:
        return _error("filter_field is required when filter_value is specified.")
    if filter_field and filter_field not in dt_config["group_by_fields"]:
        allowed = ", ".join(sorted(dt_config["group_by_fields"])) or "none configured"
        return _error(
            f"Filtering by '{filter_field}' is not allowed for {doctype}. "
            f"Allowed filter fields: {allowed}."
        )

    # --- validate aggregate_field ---------------------------------------
    if aggregate_fn != "COUNT" or aggregate_field:
        # Non-COUNT fns require an aggregate_field
        if aggregate_fn != "COUNT" and not aggregate_field:
            return _error(
                f"aggregate_field is required when aggregate_fn is '{aggregate_fn}'."
            )
        # aggregate_field, when supplied, must be on the allowlist
        if aggregate_field and aggregate_field not in dt_config["aggregate_fields"]:
            allowed = ", ".join(sorted(dt_config["aggregate_fields"])) or "none configured"
            return _error(
                f"Aggregating '{aggregate_field}' is not allowed for {doctype}. "
                f"Allowed aggregate fields: {allowed}."
            )

    # --- validate date filters ------------------------------------------
    date_field = dt_config.get("date_field")
    if (from_date or to_date) and not date_field:
        return _error(
            f"Date filtering is not configured for {doctype}. "
            "Ask your administrator to set a Date Field in AI Assistant Settings → Allowed Document Types."
        )

    # --- validate order direction ----------------------------------------
    if order_dir not in ("asc", "desc"):
        order_dir = "desc"

    # --- build SQL -------------------------------------------------------
    # Identifiers: validated from allowlist → safe for f-string interpolation.
    # Values:      always %(name)s placeholders.
    tab = f"`tab{doctype}`"

    if aggregate_field:
        agg_expr = f"{aggregate_fn}(`{aggregate_field}`)"
        agg_label = f"{aggregate_fn}({aggregate_field})"
    else:
        agg_expr = "COUNT(*)"
        agg_label = "COUNT(*)"

    if group_by:
        select_sql = f"`{group_by}`, {agg_expr} AS agg_value"
    else:
        select_sql = f"{agg_expr} AS agg_value"

    sql_params: dict = {"limit": limit}
    where_clauses: list[str] = []

    if from_date and date_field:
        where_clauses.append(f"`{date_field}` >= %(from_date)s")
        sql_params["from_date"] = from_date
    if to_date and date_field:
        where_clauses.append(f"`{date_field}` <= %(to_date)s")
        sql_params["to_date"] = to_date
    if status:
        meta = frappe.get_meta(doctype)
        if meta.get_field("status"):
            where_clauses.append("`status` = %(status)s")
            sql_params["status"] = status
    if filter_field and filter_value:
        # filter_field already validated against group_by_fields allowlist
        where_clauses.append(f"`{filter_field}` = %(filter_value)s")
        sql_params["filter_value"] = filter_value

    # Submitted-docs-only filter for submittable DocTypes
    meta = frappe.get_meta(doctype)
    if meta.is_submittable:
        where_clauses.append("`docstatus` = 1")

    where_sql  = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    group_sql  = f"GROUP BY `{group_by}`" if group_by else ""
    order_sql  = f"ORDER BY agg_value {order_dir.upper()}"

    full_sql = (
        f"SELECT {select_sql} FROM {tab} "
        f"{where_sql} {group_sql} {order_sql} LIMIT %(limit)s"
    )

    rows = frappe.db.sql(full_sql, sql_params, as_dict=True)

    if not rows:
        filter_parts = []
        if from_date: filter_parts.append(f"from {from_date}")
        if to_date:   filter_parts.append(f"to {to_date}")
        if status:    filter_parts.append(f"with status '{status}'")
        if filter_field and filter_value:
            filter_parts.append(f"with {filter_field} = '{filter_value}'")
        filter_desc = " ".join(filter_parts) or "with the given filters"
        return _error(f"No {doctype} records found {filter_desc}.")

    # --- build citation -------------------------------------------------
    if group_by:
        columns   = [group_by, agg_label]
        safe_rows = [[_safe(r.get(group_by)), _safe(r["agg_value"])] for r in rows]
        text = (
            f"Here are the {doctype} records grouped by '{group_by}' "
            f"({agg_label}):"
        )
    else:
        columns   = [agg_label]
        safe_rows = [[_safe(r["agg_value"])] for r in rows]
        val = _safe(rows[0]["agg_value"])
        text = f"The {agg_label} for {doctype} is {val}."
        filter_parts = []
        if from_date: filter_parts.append(f"from {from_date}")
        if to_date:   filter_parts.append(f"to {to_date}")
        if status:    filter_parts.append(f"status = '{status}'")
        if filter_parts:
            text += f" (Filters: {', '.join(filter_parts)})"

    citation = {
        "type": "query_result",
        "template": "aggregate_doctype",
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
            "customer, supplier, item, or other named record — including questions "
            "about a specific attribute of a named entity such as email address, "
            "phone number, contact details, address, status, or total. "
            "ALWAYS call this tool for named-entity attribute questions instead of "
            "answering from context. "
            "Examples: 'What is SINV-IR-00657?', "
            "'Show me customer C-00042', 'Tell me about item ITEM-001', "
            "'What is PUR-ORD-2026-00001?', 'Show me purchase order PUR-ORD-2024-00077', "
            "'Look up delivery note DN-00123', "
            "'What is the email address of supplier X?', "
            "'What is the phone number of customer Y?', "
            "'What is the contact information for supplier Z?', "
            "'What is the address of customer Rana Alotom?'."
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
            "best-selling products, top items by sales, most popular items, or items "
            "ranked by revenue or amount "
            "(e.g. 'What are the top 10 selling items?', 'Which items sell the most?', "
            "'Show me the top 10 items ranked by revenue this year', "
            "'What are the highest-revenue products?')."
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
            "same Sales Invoice. Use this when the user asks about items bought or "
            "purchased together, item pairs, product bundles, or co-purchase patterns "
            "(e.g. 'What are the top 2 items sold together?', "
            "'Which items are commonly bought together?', "
            "'Which item pairs are most frequently purchased together?', "
            "'What products do customers usually buy in the same order?')."
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

    "low_stock_recent_sales": {
        "description": (
            "Find items that were sold recently but are now low or out of stock — "
            "ranked by shortfall (qty sold minus current stock). Use this when the "
            "user asks about missing stock, items running out, reorder urgency, or "
            "products that need restocking "
            "(e.g. 'What are the top 10 missing items from the last 6 months of sales?', "
            "'Which items are out of stock but still selling?')."
        ),
        "parameters": {
            "months": {
                "type": "NUMBER",
                "description": "How many months of sales history to look back (default 6, max 12).",
                "required": False,
            },
            "top_n": {
                "type": "NUMBER",
                "description": "How many items to return (default 10, max 50).",
                "required": False,
            },
        },
        "execute": _execute_low_stock_recent_sales,
        "permission_doctypes": ["Sales Invoice", "Stock Entry"],
    },

    "aggregate_doctype": {
        "description": (
            "Run a flexible COUNT / SUM / AVG / MIN / MAX aggregate query on any "
            "admin-configured DocType. Use this for questions that ask how many records "
            "exist, totals, averages, breakdowns by a field, or to check if records exist "
            "for a given filter — when no more specific template applies. "
            "Resolve relative date expressions (e.g. 'last month', 'this year') into "
            "concrete YYYY-MM-DD from_date / to_date values using today's date. "
            "You can also filter by any allowlisted field using filter_field + filter_value "
            "(e.g. filter_field='customer', filter_value='Acme Corp'). "
            "IMPORTANT: Do NOT set group_by='name' — 'name' is never an allowed group-by "
            "field. Questions like 'what X were created' or 'list X' mean COUNT with no "
            "group_by (return the total count), not a per-record breakdown. "
            "Examples: "
            "'How many purchase invoices were submitted last month?', "
            "'What is the total grand total of submitted sales orders this year?', "
            "'What stock entries were created in the last 7 days?' → COUNT Stock Entry with date filter, no group_by, "
            "'How many stock entries were created this month?' → COUNT Stock Entry with date filter, "
            "'Show the count of purchase invoices grouped by supplier last quarter', "
            "'What is the average invoice value for submitted purchase invoices in March?', "
            "'How many sales orders have status Completed?', "
            "'Show me all sales invoices for customer XYZ' (use COUNT with filter_field='customer')."
        ),
        "parameters": {
            "doctype": {
                "type": "STRING",
                "description": (
                    "The Frappe DocType to query, e.g. 'Purchase Invoice', "
                    "'Sales Order', 'Stock Entry'."
                ),
                "required": True,
            },
            "aggregate_fn": {
                "type": "STRING",
                "description": (
                    "Aggregate function: COUNT (default), SUM, AVG, MIN, MAX. "
                    "Use COUNT when asking 'how many'. Use SUM/AVG for totals/averages."
                ),
                "required": False,
            },
            "aggregate_field": {
                "type": "STRING",
                "description": (
                    "Field to aggregate (required for SUM/AVG/MIN/MAX; optional for COUNT). "
                    "Must be an admin-allowlisted numeric field, e.g. 'grand_total'."
                ),
                "required": False,
            },
            "group_by": {
                "type": "STRING",
                "description": (
                    "Field to group results by, e.g. 'status' or 'supplier'. "
                    "Must be an admin-allowlisted field. Omit for a single aggregate value."
                ),
                "required": False,
            },
            "from_date": {
                "type": "STRING",
                "description": "Start of date range (ISO date: YYYY-MM-DD). Requires date_field to be configured.",
                "required": False,
            },
            "to_date": {
                "type": "STRING",
                "description": "End of date range (ISO date: YYYY-MM-DD). Requires date_field to be configured.",
                "required": False,
            },
            "status": {
                "type": "STRING",
                "description": "Filter by status field value, e.g. 'Submitted', 'Cancelled'.",
                "required": False,
            },
            "filter_field": {
                "type": "STRING",
                "description": (
                    "An additional field to filter on (must be an admin-allowlisted "
                    "group-by field), e.g. 'supplier', 'customer'. "
                    "For Stock Entry, the entry-type field is 'stock_entry_type' "
                    "(not 'purpose') — use filter_field='stock_entry_type' with "
                    "filter_value='Material Transfer', 'Material Receipt', etc. "
                    "Use with filter_value."
                ),
                "required": False,
            },
            "filter_value": {
                "type": "STRING",
                "description": (
                    "The value to match for filter_field, e.g. a customer name or supplier name. "
                    "Required when filter_field is set."
                ),
                "required": False,
            },
            "order_by": {
                "type": "STRING",
                "description": "Sort direction for the aggregate value: 'desc' (default) or 'asc'.",
                "required": False,
            },
            "limit": {
                "type": "NUMBER",
                "description": "Maximum rows to return (default 10, max 50).",
                "required": False,
            },
        },
        "execute": _execute_aggregate_doctype,
        "permission_doctypes": None,  # checked dynamically against allowlist + frappe.has_permission
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
