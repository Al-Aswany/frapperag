"""
Document-to-text conversion module.

Each supported DocType has a dedicated Python template function that converts
a Frappe document dict into a human-readable natural language summary.
No LLM inference is performed here — summaries are generated deterministically
from field values (FR-022).
"""

SUPPORTED_DOCTYPES = {
    "Sales Invoice", "Customer", "Item",
    "Purchase Invoice", "Purchase Order", "Purchase Receipt", "Delivery Note",
    "Sales Order", "Item Price", "Stock Entry",
    "Supplier",
}


def to_text(doctype: str, doc: dict) -> str | None:
    """Convert a Frappe document dict to a human-readable text summary.

    Returns None for unsupported doctypes; caller counts the record as skipped.
    """
    converters = {
        "Sales Invoice": _sales_invoice_text,
        "Customer":      _customer_text,
        "Item":          _item_text,
        "Purchase Invoice": _purchase_invoice_text,
        "Purchase Order": _purchase_order_text,
        "Purchase Receipt": _purchase_receipt_text,
        "Delivery Note": _delivery_note_text,
        "Sales Order": _sales_order_text,
        "Item Price": _item_price_text,
        "Stock Entry": _stock_entry_text,
        "Supplier": _supplier_text,
    }
    fn = converters.get(doctype)
    return fn(doc) if fn else None


def _sales_invoice_text(d: dict) -> str:
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Sales Invoice {d['name']} issued on {d.get('posting_date')} "
        f"to customer {d.get('customer')} ({d.get('customer_name')}). "
        f"Grand total: {d.get('grand_total')} {d.get('currency')}. "
        f"Status: {d.get('status')}. Due date: {d.get('due_date')}. "
        f"Items: {items or 'none'}. "
        f"Outstanding amount: {d.get('outstanding_amount')}."
    )


def _customer_text(d: dict) -> str:
    return (
        f"Customer {d.get('customer_name')} (ID: {d['name']}). "
        f"Type: {d.get('customer_type')}. "
        f"Customer group: {d.get('customer_group')}. "
        f"Territory: {d.get('territory')}. "
        f"Primary contact: {d.get('email_id') or 'not set'}. "
        f"Outstanding amount: {d.get('outstanding_amount', 0)}."
    )


def _item_text(d: dict) -> str:
    return (
        f"Item {d.get('item_name')} (code: {d['name']}). "
        f"Item group: {d.get('item_group')}. "
        f"Stock unit: {d.get('stock_uom')}. "
        f"Standard selling rate: {d.get('standard_rate', 0)}. "
        f"Description: {(d.get('description') or '').strip()[:500]}. "
        f"Is stock item: {d.get('is_stock_item')}."
    )


def _purchase_invoice_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    date = d.get("bill_date") or d.get("posting_date")
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')} @ {r.get('rate')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Purchase Invoice {d['name']} dated {date} "
        f"from supplier {d.get('supplier_name')}. "
        f"Grand total: {d.get('grand_total')} {d.get('currency')}. "
        f"Outstanding: {d.get('outstanding_amount')}. "
        f"Status: {_DS.get(d.get('docstatus'), 'Unknown')}. "
        f"Items: {items or 'none'}."
    )


def _purchase_order_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')} @ {r.get('rate')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Purchase Order {d['name']} dated {d.get('transaction_date')} "
        f"from supplier {d.get('supplier_name')}. "
        f"Grand total: {d.get('grand_total')} {d.get('currency')}. "
        f"Status: {d.get('status')} ({_DS.get(d.get('docstatus'), 'Unknown')}). "
        f"Items: {items or 'none'}."
    )


def _purchase_receipt_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    items = "; ".join(
        f"{r.get('item_name')} ordered {r.get('qty')}, "
        f"received {r.get('received_qty')}, accepted {r.get('accepted_qty')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Purchase Receipt {d['name']} posted on {d.get('posting_date')} "
        f"from supplier {d.get('supplier_name')}. "
        f"Status: {_DS.get(d.get('docstatus'), 'Unknown')}. "
        f"Items: {items or 'none'}."
    )


def _delivery_note_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')} (stock qty: {r.get('stock_qty')})"
        for r in (d.get("items") or [])
    )
    return (
        f"Delivery Note {d['name']} posted on {d.get('posting_date')} "
        f"for customer {d.get('customer_name')}. "
        f"Status: {d.get('status')} ({_DS.get(d.get('docstatus'), 'Unknown')}). "
        f"Items: {items or 'none'}."
    )


def _sales_order_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')} @ {r.get('rate')}"
        for r in (d.get("items") or [])
    )
    return (
        f"Sales Order {d['name']} dated {d.get('transaction_date')} "
        f"for customer {d.get('customer_name')}. "
        f"Grand total: {d.get('grand_total')} {d.get('currency')}. "
        f"Status: {d.get('status')} ({_DS.get(d.get('docstatus'), 'Unknown')}). "
        f"Items: {items or 'none'}."
    )


def _item_price_text(d: dict) -> str:
    parts = [
        f"Item Price {d['name']} for item {d.get('item_code')} ({d.get('item_name')}).",
        f"Price list: {d.get('price_list')}.",
        f"Rate: {d.get('price_list_rate')} {d.get('currency')}.",
    ]
    if d.get("valid_from"):
        parts.append(f"Valid from: {d.get('valid_from')}.")
    if d.get("valid_upto"):
        parts.append(f"Valid until: {d.get('valid_upto')}.")
    return " ".join(parts)


def _stock_entry_text(d: dict) -> str:
    _DS = {0: "Draft", 1: "Submitted", 2: "Cancelled"}
    parts = [
        f"Stock Entry {d['name']} ({d.get('stock_entry_type')}) "
        f"posted on {d.get('posting_date')}.",
    ]
    if d.get("from_warehouse"):
        parts.append(f"From warehouse: {d.get('from_warehouse')}.")
    if d.get("to_warehouse"):
        parts.append(f"To warehouse: {d.get('to_warehouse')}.")
    parts.append(f"Status: {_DS.get(d.get('docstatus'), 'Unknown')}.")
    items = "; ".join(
        f"{r.get('item_name')} x{r.get('qty')}"
        + (f" from {r.get('s_warehouse')}" if r.get("s_warehouse") else "")
        + (f" to {r.get('t_warehouse')}" if r.get("t_warehouse") else "")
        for r in (d.get("items") or [])
    )
    parts.append(f"Items: {items or 'none'}.")
    return " ".join(parts)


def _supplier_text(d: dict) -> str:
    status = "inactive" if d.get("disabled") else "active"
    return (
        f"Supplier {d.get('supplier_name')} (ID: {d['name']}). "
        f"Type: {d.get('supplier_type')}. "
        f"Supplier group: {d.get('supplier_group')}. "
        f"Country: {d.get('country')}. "
        f"Primary email: {d.get('email_id') or 'not set'}. "
        f"Status: {status}."
    )
