import frappe

EMBEDDING_DIM = 768  # text-embedding-004 output dimensions


def _get_schema():
    import pyarrow as pa
    return pa.schema([
        pa.field("id",            pa.string()),
        pa.field("doctype",       pa.string()),
        pa.field("name",          pa.string()),
        pa.field("text",          pa.string()),
        pa.field("vector",        pa.list_(pa.float32(), EMBEDDING_DIM)),
        pa.field("last_modified", pa.string()),
    ])


def _ensure_vector_index(table) -> None:
    """Create an ANN vector index on the vector column if one does not already exist.

    Uses IVF_PQ for ANN search (orders-of-magnitude faster than brute-force scan).
    No-op when an index already covers the vector column, or the table has fewer
    than 256 rows (flat scan is faster at that scale; IVF_PQ needs data to train on).
    Index creation is best-effort — a full scan remains the safe fallback.
    """
    _log = frappe.logger("frapperag", allow_site=True)
    table_name = getattr(table, "name", "unknown")

    try:
        indices = table.list_indices()
        for idx in indices:
            cols = getattr(idx, "columns", None) or []
            if "vector" in cols:
                return  # already indexed — nothing to do
    except Exception as exc:
        _log.warning(f"[INDEX] {table_name}: list_indices() failed ({exc}) — skipping")
        return  # list_indices unsupported in this build — skip safely

    row_count = table.count_rows()
    if row_count < 256:
        return  # flat scan is faster; IVF_PQ needs enough rows to train partitions

    num_partitions = min(256, max(1, row_count // 10))
    _log.info(f"[INDEX] {table_name}: building IVF_PQ index ({row_count} rows, {num_partitions} partitions)")
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
        _log.warning(f"[INDEX] {table_name}: create_index failed ({exc}) — falling back to flat scan")


def get_store(doctype: str):
    """Open (or create) the LanceDB table for a DocType. Returns (db, table).

    Table name uses the 'v1_' prefix for schema versioning.
    The table is NEVER dropped — re-indexing always upserts (FR-023).
    lancedb is imported here, not at module level, to prevent cross-site
    global state in multi-site bench workers (Principle II).

    An IVF_PQ vector index is created automatically if one does not already
    exist — this eliminates the brute-force scan that caused the 5.7s bottleneck.
    """
    import lancedb
    rag_path = frappe.get_site_path("private", "files", "rag")
    db = lancedb.connect(rag_path)
    table_name = "v1_" + doctype.lower().replace(" ", "_")
    table = db.create_table(table_name, schema=_get_schema(), exist_ok=True)
    _ensure_vector_index(table)
    return db, table


def upsert_vectors(doctype: str, rows: list) -> None:
    """Upsert a batch of row dicts into the LanceDB table for doctype.

    Uses merge_insert("id") so existing entries are updated in place and
    new entries are inserted. The table is never rebuilt from scratch (FR-023).
    """
    _, table = get_store(doctype)
    (
        table.merge_insert("id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(rows)
    )
