"""
Document-to-text conversion module.

Each supported DocType has a dedicated Python template function that converts
a Frappe document dict into a human-readable natural language summary.
No LLM inference is performed here — summaries are generated deterministically
from field values (FR-022).
"""

SUPPORTED_DOCTYPES = {"Sales Invoice", "Customer", "Item"}


def to_text(doctype: str, doc: dict) -> str | None:
    """Convert a Frappe document dict to a human-readable text summary.

    Returns None for unsupported doctypes; caller counts the record as skipped.
    """
    converters = {
        "Sales Invoice": _sales_invoice_text,
        "Customer":      _customer_text,
        "Item":          _item_text,
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
