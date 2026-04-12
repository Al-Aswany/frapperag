"""Report execution module for the FrappeRAG chat pipeline.

Performs all permission checks and executes a whitelisted Report Builder report
via frappe.get_doc("Report", name).get_data(). Never raises — all exceptions are
caught and converted to plain-language error dicts.

Called synchronously from chat_runner.run_chat_job() when the AI returns a
tool_call response. Runs inside the frappe.set_user(user) context already
established at job start (FR-023).
"""


def execute_report(
    args: dict,
    user: str,
    whitelist_snapshot: set,
) -> dict:
    """Perform permission checks and execute a Report Builder report.

    Args:
        args:               {"report_name": str, "filters": dict}
        user:               frappe.session.user value from the calling job
        whitelist_snapshot: set of report names loaded at job start (FR-008a)

    Returns one of:
        Success: {"text": "<narrative>", "citations": [{"type": "report_result", ...}],
                  "tokens_used": 0}
        Error:   {"text": "<plain-language error>", "citations": [], "tokens_used": 0}
    """
    import frappe

    report_name = (args or {}).get("report_name", "")
    filters = (args or {}).get("filters") or {}

    def _error(msg: str) -> dict:
        return {"text": msg, "citations": [], "tokens_used": 0}

    # Check 1 — whitelist membership (guards against hallucinated names, FR-008a)
    if report_name not in whitelist_snapshot:
        return _error(
            f"I tried to run a report called '{report_name}', but it is not in the "
            "approved list. Please contact your administrator."
        )

    try:
        # Check 2 — live report_type guard (guards post-whitelist misconfiguration, FR-008b)
        rtype = frappe.db.get_value("Report", report_name, "report_type")
        if rtype != "Report Builder":
            return _error(
                f"The report '{report_name}' cannot be run through this interface "
                "(only Report Builder reports are supported)."
            )

        # Check 3 — user permission (FR-008c, FR-009)
        if not frappe.has_permission("Report", doc=report_name, ptype="read", user=user):
            return _error(
                f"You do not have permission to view the '{report_name}' report. "
                "Please contact your administrator if you need access."
            )

        # Execute (FR-013) — limit=50 enforced here (FR-014)
        report_doc = frappe.get_doc("Report", report_name)
        columns, result = report_doc.get_data(filters=filters, limit=50, user=user)

        # Extract column header labels
        col_labels = [
            col.get("label") or col.get("fieldname", "")
            for col in (columns or [])
        ]

        # Normalise rows: Frappe may return list-of-dicts or list-of-lists (FR-013)
        # Cell values are coerced to JSON-safe types: date/datetime → ISO string,
        # Decimal → float, everything else → str if not already a JSON primitive.
        import datetime
        from decimal import Decimal

        _json_primitives = (type(None), bool, int, float, str)

        def _safe(v):
            if isinstance(v, _json_primitives):
                return v
            if isinstance(v, (datetime.datetime, datetime.date)):
                return v.isoformat()
            if isinstance(v, Decimal):
                return float(v)
            return str(v)

        fieldnames = [col.get("fieldname", "") for col in (columns or [])]
        rows = []
        for row in (result or []):
            if isinstance(row, dict):
                rows.append([_safe(row.get(fn)) for fn in fieldnames])
            else:
                rows.append([_safe(v) for v in row])

        row_count = len(rows)  # true count before cap (FR-014)
        rows = rows[:50]  # enforce 50-row display cap

        citation = {
            "type": "report_result",
            "report_name": report_name,
            "columns": col_labels,
            "rows": rows,
            "row_count": row_count,
        }

        if row_count == 0:
            text = (
                f"The {report_name} report returned no results for the given filters. "
                "No data matches those criteria."
            )
        else:
            text = f"Here are the results for the {report_name} report:"

        return {"text": text, "citations": [citation], "tokens_used": 0}

    except frappe.DoesNotExistError:
        return _error(
            f"The report '{report_name}' could not be found. It may have been deleted — "
            "please ask your administrator to check the whitelist."
        )
    except Exception as exc:
        short = str(exc)[:200]
        return _error(
            f"The report '{report_name}' could not be executed: {short}. "
            "Please try again or contact your administrator."
        )
