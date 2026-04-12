import os
import frappe


def after_install():
    rag_path = os.path.join(frappe.utils.get_bench_path(), "rag")
    os.makedirs(rag_path, exist_ok=True)
    _ensure_existing_lancedb_indices(rag_path)
    _ensure_sidecar_procfile_entry()
    seed_allowed_doctypes()
    frappe.db.commit()


def _ensure_existing_lancedb_indices(rag_path: str) -> None:
    """Create ANN vector indices on any v1_* LanceDB tables that already exist.

    On a fresh install this is a no-op (no tables yet).  On an upgrade where
    the DB already holds indexed data, this backfills the IVF_PQ indices so
    existing tables benefit from ANN search immediately.
    """
    import lancedb
    _log = frappe.logger("frapperag", allow_site=True)
    try:
        db = lancedb.connect(rag_path)
        for table_name in db.table_names():
            if not table_name.startswith("v1_"):
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

        entry = f"\nrag_sidecar: {python_bin} {sidecar_script} --port 8100\n"
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


_DEFAULT_DOCTYPES = [
    "Sales Invoice", "Customer", "Item",
    "Purchase Invoice", "Purchase Order", "Sales Order",
    "Delivery Note", "Purchase Receipt", "Supplier",
    "Item Price", "Stock Entry",
]


def seed_allowed_doctypes() -> None:
    """Idempotently append missing DocTypes to the AI Assistant Settings whitelist.

    Called from after_install() (fresh install) and as the after_migrate hook
    (upgrades). Safe to run multiple times - existing rows are never duplicated.
    """
    if not frappe.db.exists("DocType", "AI Assistant Settings"):
        return
    settings = frappe.get_single("AI Assistant Settings")
    existing = {
        # `doctype_name` is the current child-table field; keep fallback for
        # older rows created before the field rename.
        getattr(row, "doctype_name", None) or getattr(row, "document_type", None)
        for row in (settings.allowed_doctypes or [])
    }
    existing.discard(None)
    changed = False
    for dt in _DEFAULT_DOCTYPES:
        if dt not in existing:
            settings.append("allowed_doctypes", {"doctype_name": dt})
            changed = True
    if changed:
        settings.flags.ignore_validate = True
        settings.flags.ignore_mandatory = True
        settings.save(ignore_permissions=True)
        frappe.db.commit()
