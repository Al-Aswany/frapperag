import os
import importlib
from typing import Any

import frappe

from frapperag.rag.legacy_vector_policy import LEGACY_VECTOR_DOCTYPES


_SUPERVISOR_MARKER = "# --- frapperag rag_sidecar (managed by after_install/after_migrate) ---"
_DEFAULT_CHAT_MODEL = "gemini-2.5-flash"


def after_install():
    rag_path = os.path.join(frappe.utils.get_bench_path(), "rag")
    os.makedirs(rag_path, exist_ok=True)
    if _legacy_vector_dependencies_available():
        _ensure_existing_lancedb_indices(rag_path)
    _ensure_sidecar_procfile_entry()
    _ensure_sidecar_supervisor_entry()
    seed_all_settings()
    _refresh_schema_catalog_bootstrap("after_install")
    frappe.db.commit()


def after_migrate():
    """Re-assert process-manager entries on every migrate.

    `bench setup supervisor` regenerates config/supervisor.conf from a template
    and wipes third-party entries. Frappe Cloud runs migrate on every deploy,
    so re-appending here keeps the sidecar registered.
    """
    _ensure_sidecar_procfile_entry()
    _ensure_sidecar_supervisor_entry()
    seed_all_settings()
    _refresh_schema_catalog_bootstrap("after_migrate")


def _active_provider_name() -> str:
    """Read embedding_provider from settings, default to 'gemini'."""
    try:
        return frappe.get_cached_doc("AI Assistant Settings").embedding_provider or "gemini"
    except Exception:
        return "gemini"


def rewrite_sidecar_env(provider: str) -> None:
    """Re-render the Procfile and supervisor entries with the new EMBEDDING_PROVIDER.

    Idempotent — replaces the existing entry rather than appending.
    Called from AIAssistantSettings.on_update() when embedding_provider changes.
    """
    _rewrite_procfile_env(provider)
    _rewrite_supervisor_env(provider)


def _rewrite_procfile_env(provider: str) -> None:
    try:
        bench_path = frappe.utils.get_bench_path()
        procfile_path = os.path.join(bench_path, "Procfile")
        if not os.path.exists(procfile_path):
            return

        with open(procfile_path, "r") as f:
            content = f.read()

        if "rag_sidecar:" not in content:
            return  # not yet present; _ensure_sidecar_procfile_entry will add it

        app_path = os.path.join(bench_path, "apps", "frapperag", "frapperag")
        python_bin = os.path.join(bench_path, "env", "bin", "python")
        sidecar_script = os.path.join(app_path, "sidecar", "main.py")
        new_entry = f"rag_sidecar: env EMBEDDING_PROVIDER={provider} {python_bin} {sidecar_script} --port 8100"

        import re
        content = re.sub(r"rag_sidecar:.*", new_entry, content)
        with open(procfile_path, "w") as f:
            f.write(content)

        frappe.logger().info("frapperag: updated Procfile rag_sidecar EMBEDDING_PROVIDER=%s", provider)
    except Exception as exc:
        frappe.logger().warning("frapperag: could not rewrite Procfile: %s", exc)


def _rewrite_supervisor_env(provider: str) -> None:
    try:
        bench_path = frappe.utils.get_bench_path()
        conf_path = os.path.join(bench_path, "config", "supervisor.conf")
        if not os.path.exists(conf_path):
            return

        with open(conf_path, "r") as f:
            content = f.read()

        if _SUPERVISOR_MARKER not in content:
            return  # block not present yet

        import re
        content = re.sub(
            r"(environment=EMBEDDING_PROVIDER=)[^\n]*",
            f'\\1"{provider}"',
            content,
        )
        with open(conf_path, "w") as f:
            f.write(content)

        frappe.logger().info("frapperag: updated supervisor.conf EMBEDDING_PROVIDER=%s", provider)
    except Exception as exc:
        frappe.logger().warning("frapperag: could not rewrite supervisor.conf: %s", exc)


def _ensure_existing_lancedb_indices(rag_path: str) -> None:
    """Create ANN vector indices on any active-family LanceDB tables that already exist.

    On a fresh install this is a no-op (no tables yet).  On an upgrade where
    the DB already holds indexed data, this backfills the IVF_PQ indices so
    existing tables benefit from ANN search immediately.
    """
    import lancedb
    _log = frappe.logger("frapperag", allow_site=True)
    try:
        db = lancedb.connect(rag_path)
        for table_name in db.table_names():
            if not (table_name.startswith("v5_gemini_") or table_name.startswith("v6_e5small_")):
                continue
            try:
                table = db.open_table(table_name)
                # Check for existing vector index before creating one.
                try:
                    indices = table.list_indices()
                    for idx in indices:
                        cols = getattr(idx, "columns", None) or []
                        if "vector" in cols:
                            continue  # already indexed — skip
                except Exception as exc:
                    _log.warning(f"[INDEX] {table_name}: list_indices() failed ({exc}) — skipping")
                    continue

                row_count = table.count_rows()
                if row_count < 256:
                    continue  # flat scan is faster; IVF_PQ needs enough rows to train

                num_partitions = min(256, max(1, row_count // 10))
                _log.info(
                    f"[INDEX] {table_name}: building IVF_PQ index"
                    f" ({row_count} rows, {num_partitions} partitions)"
                )
                try:
                    table.create_index(
                        metric="cosine",
                        vector_column_name="vector",
                        num_partitions=num_partitions,
                        num_sub_vectors=96,
                        replace=False,
                    )
                    _log.info(f"[INDEX] {table_name}: IVF_PQ index ready")
                except Exception as exc:
                    _log.warning(
                        f"[INDEX] {table_name}: create_index failed ({exc}) — falling back to flat scan"
                    )
            except Exception:
                pass  # best-effort per table
    except Exception:
        pass  # if LanceDB isn't installed yet, skip gracefully


def _legacy_vector_dependencies_available() -> bool:
    try:
        return (
            importlib.util.find_spec("lancedb") is not None
            and importlib.util.find_spec("pyarrow") is not None
        )
    except Exception:
        return False


def _ensure_sidecar_procfile_entry() -> None:
    """Append the rag_sidecar Procfile line if not already present.

    Derives paths from frappe.utils.get_bench_path() so this works regardless
    of where the bench is installed.
    """
    try:
        bench_path = frappe.utils.get_bench_path()
        procfile_path = os.path.join(bench_path, "Procfile")

        if not os.path.exists(procfile_path):
            frappe.logger().warning(
                "frapperag after_install: Procfile not found at %s — skipping sidecar entry",
                procfile_path,
            )
            return

        with open(procfile_path, "r") as f:
            content = f.read()

        if "rag_sidecar:" in content:
            return  # already present — idempotent

        app_path = os.path.join(bench_path, "apps", "frapperag", "frapperag")
        python_bin = os.path.join(bench_path, "env", "bin", "python")
        sidecar_script = os.path.join(app_path, "sidecar", "main.py")
        provider = _active_provider_name()

        entry = f"\nrag_sidecar: env EMBEDDING_PROVIDER={provider} {python_bin} {sidecar_script} --port 8100\n"
        with open(procfile_path, "a") as f:
            f.write(entry)

        frappe.logger().info(
            "frapperag after_install: added rag_sidecar entry to Procfile. "
            "Run 'bench start' to launch the sidecar."
        )
    except Exception as exc:
        frappe.logger().warning(
            "frapperag after_install: could not update Procfile: %s", exc
        )


def _ensure_sidecar_supervisor_entry() -> None:
    """Append a `[program:rag_sidecar]` block to config/supervisor.conf.

    Production (Frappe Cloud, self-hosted with supervisor) uses supervisord, not
    the Procfile. Appending to config/supervisor.conf is idempotent via
    _SUPERVISOR_MARKER, and is re-asserted on every after_migrate since
    `bench setup supervisor` regenerates that file from a template.

    After install (or after `bench setup supervisor`) an admin must run:
        sudo supervisorctl reread && sudo supervisorctl update
    """
    try:
        import getpass

        bench_path = frappe.utils.get_bench_path()
        conf_path = os.path.join(bench_path, "config", "supervisor.conf")

        if not os.path.exists(conf_path):
            frappe.logger().warning(
                "frapperag: %s not found — skipping supervisor entry "
                "(bench may be in dev mode; Procfile entry will be used)",
                conf_path,
            )
            return

        with open(conf_path, "r") as f:
            content = f.read()

        if _SUPERVISOR_MARKER in content:
            return  # already present

        python_bin = os.path.join(bench_path, "env", "bin", "python")
        sidecar_script = os.path.join(
            bench_path, "apps", "frapperag", "frapperag", "sidecar", "main.py"
        )
        log_file = os.path.join(bench_path, "logs", "rag_sidecar.log")
        err_file = os.path.join(bench_path, "logs", "rag_sidecar.error.log")
        user = getpass.getuser()
        provider = _active_provider_name()

        block = (
            f"\n{_SUPERVISOR_MARKER}\n"
            "[program:rag_sidecar]\n"
            f"command={python_bin} {sidecar_script} --port 8100\n"
            "autostart=true\n"
            "autorestart=true\n"
            "startsecs=10\n"
            "stopwaitsecs=30\n"
            "stopasgroup=true\n"
            "killasgroup=true\n"
            f"stdout_logfile={log_file}\n"
            f"stderr_logfile={err_file}\n"
            f"user={user}\n"
            f"directory={bench_path}\n"
            f'environment=EMBEDDING_PROVIDER="{provider}"\n'
        )

        with open(conf_path, "a") as f:
            f.write(block)

        frappe.logger().info(
            "frapperag: appended rag_sidecar to %s. "
            "Run `sudo supervisorctl reread && sudo supervisorctl update` to start it.",
            conf_path,
        )
    except Exception as exc:
        frappe.logger().warning(
            "frapperag: could not update supervisor config: %s", exc
        )


_DEFAULT_DOCTYPES = list(LEGACY_VECTOR_DOCTYPES)

_DEFAULT_ALLOWED_DOCTYPE_DATE_FIELDS = {
    "Purchase Invoice": "posting_date",
    "Purchase Order": "transaction_date",
    "Sales Invoice": "posting_date",
    "Sales Order": "transaction_date",
    "Stock Entry": "posting_date",
}
_PHASE4F_ANALYTICS_POLICY_OVERRIDES = {
    "Purchase Invoice": {
        "default_date_field": "posting_date",
        "allow_query_builder": 1,
        "allow_child_tables": 1,
        "large_table_requires_date_filter": 0,
    },
    "Sales Invoice": {
        "default_date_field": "posting_date",
        "allow_query_builder": 1,
        "allow_child_tables": 1,
        "large_table_requires_date_filter": 1,
    },
}

_DEFAULT_ROLES = [
    "System Manager",
    "RAG Admin",
]

_DEFAULT_REPORTS = [
    {"report": "AI Supplier by Country", "description": "Supplier list"},
    {"report": "Customer by Territory"},
    {"report": "Item Active List"},
    {"report": "Sales Invoice Recent"},
]

_DEFAULT_AGGREGATE_FIELDS = [
    {"doctype_name": "Purchase Invoice", "fieldname": "grand_total", "allow_aggregate": 1},
    {"doctype_name": "Purchase Invoice", "fieldname": "supplier"},
    {"doctype_name": "Purchase Invoice", "fieldname": "status"},
    {"doctype_name": "Sales Order", "fieldname": "grand_total", "allow_aggregate": 1},
    {"doctype_name": "Sales Order", "fieldname": "customer"},
    {"doctype_name": "Sales Order", "fieldname": "status"},
    {"doctype_name": "Stock Entry", "fieldname": "stock_entry_type"},
    {"doctype_name": "Purchase Order", "fieldname": "grand_total", "allow_aggregate": 1},
    {"doctype_name": "Purchase Order", "fieldname": "supplier"},
    {"doctype_name": "Purchase Order", "fieldname": "status"},
    {"doctype_name": "Sales Invoice", "fieldname": "grand_total", "allow_aggregate": 1},
    {"doctype_name": "Sales Invoice", "fieldname": "customer"},
    {"doctype_name": "Sales Invoice", "fieldname": "status"},
]


def _default_allowed_doctype_policy(doctype_name: str, legacy_date_field: str | None = None) -> dict:
    date_field = legacy_date_field or _DEFAULT_ALLOWED_DOCTYPE_DATE_FIELDS.get(doctype_name)
    policy = {
        "enabled": 1,
        "date_field": date_field,
        "default_date_field": date_field,
        "default_title_field": "",
        "allow_get_list": 1,
        "allow_query_builder": 0,
        "allow_child_tables": 0,
        "default_sort": "modified desc",
        "default_limit": 20,
        "large_table_requires_date_filter": 0,
    }
    for fieldname, value in (_PHASE4F_ANALYTICS_POLICY_OVERRIDES.get(doctype_name) or {}).items():
        policy[fieldname] = value
    return policy


def seed_all_settings() -> None:
    """Idempotently configure AI Assistant Settings with production defaults.

    Seeds the API key, allowed doctypes, roles, reports, and aggregate fields
    so the app works immediately after install with no manual configuration.
    Called from after_install() and as the after_migrate hook.
    """
    if not frappe.db.exists("DocType", "AI Assistant Settings"):
        return

    settings = frappe.get_single("AI Assistant Settings")
    changed = False
    changed_single_values = False

    # --- Enable + sidecar port ---
    if not settings.is_enabled:
        settings.is_enabled = 1
        changed = True

    if not settings.sidecar_port:
        settings.sidecar_port = 8100
        changed = True

    if not getattr(settings, "chat_model", None):
        settings.chat_model = _DEFAULT_CHAT_MODEL
        changed = True

    if getattr(settings, "enable_chat_google_search", None) in (None, ""):
        settings.enable_chat_google_search = 0
        changed = True

    if getattr(settings, "enable_transactional_vector_sync", None) in (None, ""):
        settings.enable_transactional_vector_sync = 0
        changed = True

    if not frappe.db.get_single_value("AI Assistant Settings", "assistant_mode"):
        frappe.db.set_single_value(
            "AI Assistant Settings",
            "assistant_mode",
            "v1",
            update_modified=False,
        )
        changed_single_values = True

    # --- Allowed DocTypes ---
    existing_doctypes = {
        getattr(row, "doctype_name", None) or getattr(row, "document_type", None)
        for row in (settings.allowed_doctypes or [])
    }
    existing_doctypes.discard(None)
    for dt in _DEFAULT_DOCTYPES:
        if dt not in existing_doctypes:
            settings.append(
                "allowed_doctypes",
                {"doctype_name": dt, **_default_allowed_doctype_policy(dt)},
            )
            changed = True

    for row in (settings.allowed_doctypes or []):
        defaults = _default_allowed_doctype_policy(
            row.doctype_name,
            legacy_date_field=(getattr(row, "date_field", None) or None),
        )
        for fieldname, value in defaults.items():
            current_value = getattr(row, fieldname, None)
            if current_value not in (None, ""):
                continue
            setattr(row, fieldname, value)
            changed = True

    if _apply_phase4f_analytics_policy_defaults(settings):
        changed = True

    # --- Allowed Roles ---
    existing_roles = {row.role for row in (settings.allowed_roles or [])}
    for role in _DEFAULT_ROLES:
        if role not in existing_roles:
            settings.append("allowed_roles", {"role": role})
            changed = True

    # --- Allowed Reports (skip reports that don't exist on this site) ---
    existing_reports = {row.report for row in (settings.allowed_reports or [])}
    for entry in _DEFAULT_REPORTS:
        if entry["report"] not in existing_reports:
            if frappe.db.exists("Report", entry["report"]):
                settings.append("allowed_reports", entry)
                changed = True

    # --- Aggregate Fields ---
    existing_agg = {
        (row.doctype_name, row.fieldname)
        for row in (settings.aggregate_fields or [])
    }
    for entry in _DEFAULT_AGGREGATE_FIELDS:
        key = (entry["doctype_name"], entry["fieldname"])
        if key not in existing_agg:
            settings.append("aggregate_fields", entry)
            changed = True

    if changed:
        settings.flags.ignore_validate = True
        settings.flags.ignore_mandatory = True
        settings.flags.ignore_links = True
        settings.save(ignore_permissions=True)
        frappe.db.commit()
    elif changed_single_values:
        frappe.clear_document_cache("AI Assistant Settings", "AI Assistant Settings")
        frappe.db.commit()


# Keep backward-compatible alias for the after_migrate hook
seed_allowed_doctypes = seed_all_settings


def _apply_phase4f_analytics_policy_defaults(settings: Any) -> bool:
    changed = False
    for row in (settings.allowed_doctypes or []):
        overrides = _PHASE4F_ANALYTICS_POLICY_OVERRIDES.get(getattr(row, "doctype_name", None) or "")
        if not overrides:
            continue
        for fieldname, value in overrides.items():
            if getattr(row, fieldname, None) == value:
                continue
            setattr(row, fieldname, value)
            changed = True
        if getattr(row, "allow_get_list", None) != 1:
            row.allow_get_list = 1
            changed = True
    return changed


def _refresh_schema_catalog_bootstrap(reason: str) -> None:
    try:
        from frapperag.assistant.schema_refresh import enqueue_schema_catalog_refresh

        result = enqueue_schema_catalog_refresh(reason=reason, requested_by="System")
        message = "schema catalog bootstrap enqueue result: site=%s reason=%s queued=%s status=%s"
        args = (
            frappe.local.site,
            reason,
            result.get("queued"),
            result.get("status"),
        )
        frappe.logger("frapperag", allow_site=True).info(
            message,
            *args,
        )
        frappe.logger().info(
            "frapperag: " + message,
            *args,
        )
    except Exception as exc:
        message = "schema catalog bootstrap enqueue failed: site=%s reason=%s error=%s"
        args = (frappe.local.site, reason, exc)
        frappe.logger("frapperag", allow_site=True).warning(
            message,
            *args,
        )
        frappe.logger().warning(
            "frapperag: " + message,
            *args,
        )
