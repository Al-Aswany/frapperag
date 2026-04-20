"""LanceDB store wrapper for the RAG sidecar.

This module is imported ONLY inside the sidecar process (FastAPI/uvicorn).
It MUST NOT be imported from Frappe worker processes — doing so would violate
Constitution Principle IV (workers must talk to LanceDB via HTTP, not directly).

The LanceDB connection is initialised once during the FastAPI lifespan startup
event (not at Python module import time) to prevent accidental connection
attempts before the bench path is configured.
"""

import pyarrow as pa

EMBEDDING_DIM = 384  # multilingual-e5-small output dimensions
TABLE_PREFIX = "v4_"

# Schema mirrors lancedb_store.py (v1_) but uses v4_ prefix and
# is accessed exclusively via the sidecar HTTP API.
_SCHEMA = pa.schema([
    pa.field("id",            pa.string()),
    pa.field("doctype",       pa.string()),
    pa.field("name",          pa.string()),
    pa.field("text",          pa.string()),
    pa.field("vector",        pa.list_(pa.float32(), EMBEDDING_DIM)),
    pa.field("last_modified", pa.string()),
])

# Module-level connection reference — set by init_store() during lifespan startup.
_db = None


def init_store(rag_dir: str) -> None:
    """Initialise the LanceDB connection.

    Called ONCE from the FastAPI lifespan startup event, not at import time.
    `rag_dir` is the absolute path to the bench-level rag/ directory.
    """
    import lancedb

    global _db
    _db = lancedb.connect(rag_dir)


def _table_name(doctype: str) -> str:
    """Compute the v4_ table name for a given DocType."""
    return TABLE_PREFIX + doctype.lower().replace(" ", "_")


def _record_id(doctype: str, name: str) -> str:
    """Compute the composite record ID used as the primary key."""
    return f"{doctype}:{name}"


def get_or_create_table(table_name: str):
    """Open or create a LanceDB table with the standard v4_ schema."""
    if _db is None:
        raise RuntimeError("store not initialised — call init_store() first")
    return _db.create_table(table_name, schema=_SCHEMA, exist_ok=True)


def upsert_rows(table_name: str, rows: list[dict]) -> None:
    """Upsert a list of row dicts into a v4_ LanceDB table.

    Uses merge_insert("id") so existing entries are updated in-place
    and new entries are inserted without rebuilding the table.
    """
    table = get_or_create_table(table_name)
    (
        table.merge_insert("id")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute(rows)
    )


def delete_row(table_name: str, record_id: str) -> bool:
    """Delete a single row from a v4_ table by composite ID.

    Returns True if the row existed and was deleted, False if it was not found.
    No-op (returns False) if the table does not exist.
    """
    if _db is None:
        raise RuntimeError("store not initialised — call init_store() first")
    try:
        table = _db.open_table(table_name)
    except Exception:
        return False  # table does not exist — idempotent

    # Check row count before and after to determine if anything was deleted.
    before = table.count_rows(filter=f"id = '{record_id}'")
    if before == 0:
        return False
    table.delete(f"id = '{record_id}'")
    return True


def drop_table(table_name: str) -> bool:
    """Drop an entire v4_ LanceDB table.

    Returns True if the table existed and was dropped, False if not found.
    Idempotent — no error if the table does not exist.
    """
    if _db is None:
        raise RuntimeError("store not initialised — call init_store() first")
    existing = _db.table_names()
    if table_name not in existing:
        return False
    _db.drop_table(table_name)
    return True


def _search_one_table(
    table_name: str, query_vector: list, top_k: int, max_distance: float
) -> tuple[str, list, float]:
    """Search a single v4_* table. Returns (table_name, rows, elapsed_seconds).

    Called from a ThreadPoolExecutor worker inside search_all_v4_tables.
    """
    import time as _time
    t0 = _time.monotonic()
    try:
        table = _db.open_table(table_name)
        rows = (
            table.search(query_vector, vector_column_name="vector")
            .limit(top_k)
            .to_list()
        )
    except Exception:
        return table_name, [], _time.monotonic() - t0
    return table_name, rows, _time.monotonic() - t0


def search_all_v4_tables(query_vector: list, top_k: int = 5, max_distance: float = 1.0) -> list:
    """Search all v4_* tables with a pre-computed query vector.

    Searches tables in parallel (ThreadPoolExecutor) when more than one table exists.
    Returns a list of dicts: {doctype, name, text, _distance}, sorted by distance.
    Tables that cannot be opened or searched are skipped silently.
    Returns [] if no v4_* tables exist.
    """
    import logging
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if _db is None:
        raise RuntimeError("store not initialised — call init_store() first")

    _log = logging.getLogger("rag_sidecar")
    table_names = [t for t in _db.table_names() if t.startswith(TABLE_PREFIX)]
    if not table_names:
        return []

    results = []
    max_workers = min(len(table_names), 8)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_search_one_table, t, query_vector, top_k, max_distance): t
            for t in table_names
        }
        for future in as_completed(futures):
            table_name, rows, elapsed = future.result()
            _log.info("[TIMING][store] search %s %.3fs → %d rows", table_name, elapsed, len(rows))
            for row in rows:
                dist = row.get("_distance", 0)
                if dist <= max_distance:
                    results.append({
                        "doctype":   row["doctype"],
                        "name":      row["name"],
                        "text":      row["text"],
                        "_distance": dist,
                    })

    results.sort(key=lambda r: r["_distance"])
    return results


# Convenience helpers used by main.py endpoints

def table_name_for(doctype: str) -> str:
    return _table_name(doctype)


def record_id_for(doctype: str, name: str) -> str:
    return _record_id(doctype, name)
