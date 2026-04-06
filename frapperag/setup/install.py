import os
import frappe


def after_install():
    rag_path = frappe.get_site_path("private", "files", "rag")
    os.makedirs(rag_path, exist_ok=True)
    _ensure_existing_lancedb_indices(rag_path)
    _ensure_sidecar_procfile_entry()
    frappe.db.commit()


def _ensure_existing_lancedb_indices(rag_path: str) -> None:
    """Create ANN vector indices on any v1_* LanceDB tables that already exist.

    On a fresh install this is a no-op (no tables yet).  On an upgrade where
    the DB already holds indexed data, this backfills the IVF_PQ indices so
    existing tables benefit from ANN search immediately.
    """
    import lancedb
    from frapperag.rag.lancedb_store import _ensure_vector_index
    try:
        db = lancedb.connect(rag_path)
        for table_name in db.table_names():
            if not table_name.startswith("v1_"):
                continue
            try:
                _ensure_vector_index(db.open_table(table_name))
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
