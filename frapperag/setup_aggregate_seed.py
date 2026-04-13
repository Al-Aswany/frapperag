"""
Seed aggregate configuration in AI Assistant Settings for the v1 test suite.

Adds date_field values to existing allowed_doctypes rows (and inserts any
missing allowed_doctypes rows) so the 'aggregate_doctype' template can execute
for test questions: AG-03..AG-06, ST-03, ST-04, EM-01, EM-02.

Run once after bench migrate:

    bench --site golive.site1 execute frapperag.setup_aggregate_seed.main

Safe to re-run — skips rows that already exist.
"""

import frappe


# ---------------------------------------------------------------------------
# Per-DocType seed data
# ---------------------------------------------------------------------------

# date_field: the fieldname used for from_date/to_date filters.
# aggregate_fields: list of (fieldname, allow_group_by, allow_aggregate) tuples.
SEED = {
    "Purchase Invoice": {
        "date_field": "posting_date",
        "aggregate_fields": [
            ("grand_total",  0, 1),   # SUM / AVG of invoice value
            ("supplier",     1, 0),   # GROUP BY supplier
            ("status",       1, 0),   # GROUP BY status
        ],
    },
    "Purchase Order": {
        "date_field": "transaction_date",
        "aggregate_fields": [
            ("grand_total",  0, 1),   # SUM / AVG
            ("supplier",     1, 0),   # GROUP BY supplier
            ("status",       1, 0),   # GROUP BY status
        ],
    },
    "Sales Invoice": {
        "date_field": "posting_date",
        "aggregate_fields": [
            ("grand_total",  0, 1),   # SUM / AVG
            ("customer",     1, 0),   # GROUP BY customer
            ("status",       1, 0),   # GROUP BY status
        ],
    },
    "Sales Order": {
        "date_field": "transaction_date",
        "aggregate_fields": [
            ("grand_total",  0, 1),   # SUM / AVG
            ("customer",     1, 0),   # GROUP BY customer
            ("status",       1, 0),   # GROUP BY status
        ],
    },
    "Stock Entry": {
        "date_field": "posting_date",
        "aggregate_fields": [
            ("stock_entry_type", 1, 0),  # GROUP BY entry type (Material Issue, etc.)
        ],
    },
}


def main():
    settings = frappe.get_single("AI Assistant Settings")

    # --- Step 1: ensure allowed_doctypes rows exist and date_field is set ----
    allowed_map = {row.doctype_name: row for row in (settings.allowed_doctypes or [])}

    for dt, cfg in SEED.items():
        if dt not in allowed_map:
            # Insert new row — validator requires it before aggregate_fields can reference it
            settings.append("allowed_doctypes", {
                "doctype_name": dt,
                "date_field":   cfg["date_field"],
            })
            allowed_map[dt] = settings.allowed_doctypes[-1]
            print(f"ADD   allowed_doctypes row: {dt} (date_field={cfg['date_field']!r})")
        else:
            row = allowed_map[dt]
            if row.date_field != cfg["date_field"]:
                row.date_field = cfg["date_field"]
                print(f"SET   {dt}.date_field = {cfg['date_field']!r}")
            else:
                print(f"OK    {dt}.date_field already '{cfg['date_field']}'")

    # --- Step 2: add aggregate_fields rows (skip duplicates) ---------------
    existing_agg = {
        (row.doctype_name, row.fieldname)
        for row in (settings.aggregate_fields or [])
    }

    added = []
    for dt, cfg in SEED.items():
        for fieldname, allow_group_by, allow_aggregate in cfg["aggregate_fields"]:
            key = (dt, fieldname)
            if key in existing_agg:
                print(f"SKIP  aggregate_fields row already exists: ({dt}, {fieldname})")
                continue
            settings.append("aggregate_fields", {
                "doctype_name":     dt,
                "fieldname":        fieldname,
                "allow_group_by":   allow_group_by,
                "allow_aggregate":  allow_aggregate,
            })
            existing_agg.add(key)
            added.append(f"({dt}, {fieldname}, group_by={allow_group_by}, aggregate={allow_aggregate})")

    if added:
        for msg in added:
            print(f"ADD   {msg}")
    else:
        print("OK    No new aggregate_fields rows needed.")

    # --- Step 3: save -------------------------------------------------------
    settings.save(ignore_permissions=True)
    frappe.db.commit()
    print("\nDone. AI Assistant Settings saved.")
    print(
        f"  allowed_doctypes rows : {len(settings.allowed_doctypes)}\n"
        f"  aggregate_fields rows : {len(settings.aggregate_fields)}"
    )
