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


def get_store(doctype: str):
    """Open (or create) the LanceDB table for a DocType. Returns (db, table).

    Table name uses the 'v1_' prefix for schema versioning.
    The table is NEVER dropped — re-indexing always upserts (FR-023).
    lancedb is imported here, not at module level, to prevent cross-site
    global state in multi-site bench workers (Principle II).
    """
    import lancedb
    rag_path = frappe.get_site_path("private", "files", "rag")
    db = lancedb.connect(rag_path)
    table_name = "v1_" + doctype.lower().replace(" ", "_")
    table = db.create_table(table_name, schema=_get_schema(), exist_ok=True)
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
