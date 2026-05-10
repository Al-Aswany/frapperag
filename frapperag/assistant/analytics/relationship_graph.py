from __future__ import annotations

from copy import deepcopy
from typing import Any


KNOWN_RELATIONSHIPS: dict[str, dict[str, Any]] = {
    "sales_invoice_items": {
        "relationship_key": "sales_invoice_items",
        "label": "Sales Invoice -> Sales Invoice Item",
        "source_doctype": "Sales Invoice",
        "target_doctype": "Sales Invoice Item",
        "relationship_type": "child_table",
        "source_field": "items",
        "target_parent_field": "parent",
        "field_allowlists": {
            "dimension": ("item_code", "item_name", "warehouse"),
            "filter": ("item_code", "item_name", "warehouse"),
            "co_occurrence": ("item_code",),
        },
    },
    "sales_order_items": {
        "relationship_key": "sales_order_items",
        "label": "Sales Order -> Sales Order Item",
        "source_doctype": "Sales Order",
        "target_doctype": "Sales Order Item",
        "relationship_type": "child_table",
        "source_field": "items",
        "target_parent_field": "parent",
    },
    "purchase_invoice_items": {
        "relationship_key": "purchase_invoice_items",
        "label": "Purchase Invoice -> Purchase Invoice Item",
        "source_doctype": "Purchase Invoice",
        "target_doctype": "Purchase Invoice Item",
        "relationship_type": "child_table",
        "source_field": "items",
        "target_parent_field": "parent",
        "field_allowlists": {
            "dimension": ("item_code", "item_name", "warehouse"),
            "filter": ("item_code", "item_name", "warehouse"),
            "co_occurrence": ("item_code",),
        },
    },
    "purchase_order_items": {
        "relationship_key": "purchase_order_items",
        "label": "Purchase Order -> Purchase Order Item",
        "source_doctype": "Purchase Order",
        "target_doctype": "Purchase Order Item",
        "relationship_type": "child_table",
        "source_field": "items",
        "target_parent_field": "parent",
    },
    "customer_territory": {
        "relationship_key": "customer_territory",
        "label": "Customer -> Territory",
        "source_doctype": "Customer",
        "target_doctype": "Territory",
        "relationship_type": "link",
        "source_field": "territory",
        "target_field": "name",
    },
    "customer_customer_group": {
        "relationship_key": "customer_customer_group",
        "label": "Customer -> Customer Group",
        "source_doctype": "Customer",
        "target_doctype": "Customer Group",
        "relationship_type": "link",
        "source_field": "customer_group",
        "target_field": "name",
    },
    "item_item_group": {
        "relationship_key": "item_item_group",
        "label": "Item -> Item Group",
        "source_doctype": "Item",
        "target_doctype": "Item Group",
        "relationship_type": "link",
        "source_field": "item_group",
        "target_field": "name",
    },
    "payment_entry_party": {
        "relationship_key": "payment_entry_party",
        "label": "Payment Entry -> Party",
        "source_doctype": "Payment Entry",
        "target_doctype": "Party",
        "relationship_type": "dynamic_link",
        "source_field": "party",
        "source_type_field": "party_type",
        "target_field": "name",
    },
    "stock_ledger_entry_item": {
        "relationship_key": "stock_ledger_entry_item",
        "label": "Stock Ledger Entry -> Item",
        "source_doctype": "Stock Ledger Entry",
        "target_doctype": "Item",
        "relationship_type": "link",
        "source_field": "item_code",
        "target_field": "name",
    },
    "stock_ledger_entry_warehouse": {
        "relationship_key": "stock_ledger_entry_warehouse",
        "label": "Stock Ledger Entry -> Warehouse",
        "source_doctype": "Stock Ledger Entry",
        "target_doctype": "Warehouse",
        "relationship_type": "link",
        "source_field": "warehouse",
        "target_field": "name",
    },
}


def get_relationship(relationship_key: str) -> dict[str, Any] | None:
    relationship = KNOWN_RELATIONSHIPS.get((relationship_key or "").strip())
    if not relationship:
        return None
    return deepcopy(relationship)


def find_relationship(source_doctype: str, target_doctype: str) -> dict[str, Any] | None:
    source_doctype = (source_doctype or "").strip()
    target_doctype = (target_doctype or "").strip()
    for relationship in KNOWN_RELATIONSHIPS.values():
        if relationship["source_doctype"] == source_doctype and relationship["target_doctype"] == target_doctype:
            return deepcopy(relationship)
    return None


def list_relationships(
    *,
    source_doctype: str | None = None,
    target_doctype: str | None = None,
) -> list[dict[str, Any]]:
    source_doctype = (source_doctype or "").strip()
    target_doctype = (target_doctype or "").strip()
    relationships: list[dict[str, Any]] = []
    for relationship in KNOWN_RELATIONSHIPS.values():
        if source_doctype and relationship["source_doctype"] != source_doctype:
            continue
        if target_doctype and relationship["target_doctype"] != target_doctype:
            continue
        relationships.append(deepcopy(relationship))
    return relationships


def get_allowed_relationship_fields(relationship_key: str, *, purpose: str = "dimension") -> tuple[str, ...]:
    relationship = KNOWN_RELATIONSHIPS.get((relationship_key or "").strip()) or {}
    allowlists = relationship.get("field_allowlists") or {}
    values = allowlists.get((purpose or "dimension").strip(), ())
    return tuple(str(value).strip() for value in values if str(value).strip())


def debug_describe_relationship_graph(source_doctype: str | None = None) -> dict[str, Any]:
    return {
        "relationship_count": len(KNOWN_RELATIONSHIPS),
        "relationships": list_relationships(source_doctype=source_doctype),
    }
