import frappe
from frappe import _
from frappe.model.document import Document


class AIAssistantSettings(Document):
    def validate(self):
        # T022: strict blocking check — must run before any early return
        if self.is_enabled and not self.gemini_api_key:
            frappe.throw(_("Gemini API key is required when the assistant is enabled."))

        if not self.is_enabled:
            return

        # T023: non-blocking warning — save proceeds normally
        if not self.allowed_doctypes:
            frappe.msgprint(
                _("No DocTypes are configured for indexing — chat will return no results."),
                indicator="orange",
                alert=True,
            )

        # T024: non-blocking port range warning
        if (
            hasattr(self, "sidecar_port")
            and self.sidecar_port
            and (self.sidecar_port < 1 or self.sidecar_port > 65535)
        ):
            frappe.msgprint(
                _("Sidecar port must be between 1 and 65535."),
                indicator="orange",
                alert=True,
            )

        if not self.allowed_roles:
            frappe.throw(
                "At least one Allowed Role is required when the AI Assistant is enabled.",
                frappe.ValidationError,
            )

        # Block A — Report type validation (FR-002)
        for i, row in enumerate(self.allowed_reports or []):
            if not row.report:
                continue
            rtype = frappe.db.get_value("Report", row.report, "report_type")
            if rtype != "Report Builder":
                frappe.throw(
                    f"Allowed Reports row {i + 1}: '{row.report}' is a {rtype}. "
                    "Only Report Builder reports are permitted.",
                    frappe.ValidationError,
                )

        # Block B — Default filters JSON validation (FR-001)
        import json
        import re

        for i, row in enumerate(self.allowed_reports or []):
            if not row.default_filters:
                continue
            try:
                parsed = json.loads(row.default_filters)
            except json.JSONDecodeError as exc:
                frappe.throw(
                    f"Allowed Reports row {i + 1} ({row.report}): "
                    f"Default Filters is not valid JSON — {exc}",
                    frappe.ValidationError,
                )
            if not isinstance(parsed, dict):
                frappe.throw(
                    f"Allowed Reports row {i + 1} ({row.report}): "
                    "Default Filters must be a JSON object {}, not an array or scalar.",
                    frappe.ValidationError,
                )

        # Block C — Aggregate field allowlist validation (AF-001 to AF-004)
        _FIELDNAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
        _NUMERIC_TYPES = {"Currency", "Float", "Int", "Percent"}
        _allowed_doctype_names = {r.doctype_name for r in (self.allowed_doctypes or [])}
        _seen_agg = set()

        for i, row in enumerate(self.aggregate_fields or []):
            label = f"Aggregate Fields row {i + 1}"

            # AF-001: doctype must be in allowed_doctypes
            if row.doctype_name not in _allowed_doctype_names:
                frappe.throw(
                    f"{label}: DocType '{row.doctype_name}' is not in the Allowed Document Types list.",
                    frappe.ValidationError,
                )

            # AF-002: fieldname must match safe identifier pattern
            if not _FIELDNAME_RE.match(row.fieldname or ""):
                frappe.throw(
                    f"{label}: fieldname '{row.fieldname}' is invalid. "
                    "Must start with a lowercase letter and contain only lowercase letters, digits, and underscores (max 64 chars).",
                    frappe.ValidationError,
                )

            # AF-003: no duplicate (doctype_name, fieldname) rows
            key = (row.doctype_name, row.fieldname)
            if key in _seen_agg:
                frappe.throw(
                    f"{label}: duplicate entry for ({row.doctype_name}, {row.fieldname}).",
                    frappe.ValidationError,
                )
            _seen_agg.add(key)

            # AF-004: if allow_aggregate is set, field must be a numeric type
            if row.allow_aggregate:
                meta_field = frappe.get_meta(row.doctype_name).get_field(row.fieldname)
                if not meta_field:
                    frappe.throw(
                        f"{label}: field '{row.fieldname}' does not exist on DocType '{row.doctype_name}'.",
                        frappe.ValidationError,
                    )
                if meta_field.fieldtype not in _NUMERIC_TYPES:
                    frappe.throw(
                        f"{label}: field '{row.fieldname}' is of type '{meta_field.fieldtype}'. "
                        f"Only numeric fields ({', '.join(sorted(_NUMERIC_TYPES))}) may have Allow Aggregate enabled.",
                        frappe.ValidationError,
                    )

    def on_update(self):
        """Detect removed DocTypes and enqueue a purge job for each one (US3 / FR-005).
        Also handles embedding provider changes."""
        old = self.get_doc_before_save()

        # Embedding provider change — rewrite process-manager config and prompt restart
        old_provider = (getattr(old, "embedding_provider", None) if old else None) or "gemini"
        new_provider = self.embedding_provider or "gemini"
        if old_provider != new_provider:
            from frapperag.setup.install import rewrite_sidecar_env
            rewrite_sidecar_env(new_provider)
            target_prefix = "v6_e5small_" if new_provider == "e5-small" else "v5_gemini_"
            frappe.msgprint(_(
                "Embedding provider changed to {0}. Two follow-ups required:<br>"
                "1. Restart the sidecar (<code>bench restart</code> in production, or kill the "
                "rag_sidecar process under <code>bench start</code>) for the new model to load.<br>"
                "2. After restart, click <b>Index All</b> to populate <code>{1}*</code> "
                "tables. Until then, chat will return responses without citations."
            ).format(new_provider, target_prefix), indicator="orange")

        old_allowed = {r.doctype_name for r in old.allowed_doctypes} if old else set()
        new_allowed = {r.doctype_name for r in self.allowed_doctypes}

        removed = old_allowed - new_allowed
        if not removed:
            return

        for dt in removed:
            log = frappe.get_doc({
                "doctype": "Sync Event Log",
                "doctype_name": dt,
                "record_name": "*",
                "trigger_type": "Purge",
                "outcome": "Queued",
            })
            log.insert(ignore_permissions=True)
            frappe.db.commit()

            frappe.enqueue(
                "frapperag.rag.sync_runner.run_purge_job",
                queue="short",
                timeout=120,
                job_name=f"rag_purge_{dt.lower().replace(' ', '_')}",
                site=frappe.local.site,
                sync_log_id=log.name,
                doctype=dt,
                user=frappe.session.user,
            )
