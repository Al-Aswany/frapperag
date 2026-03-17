import frappe

EMBEDDING_MODEL  = "models/gemini-embedding-001"
EMBEDDING_DIMS   = 768
TOP_K            = 5
MAX_DISTANCE     = 1.0   # cosine distance ceiling; results above this are irrelevant
RATE_LIMIT_SLEEP = 60.0
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 2.0


def embed_query(text: str, api_key: str) -> list:
    """
    Embed a single query string using gemini-embedding-001 with task_type=RETRIEVAL_QUERY.
    All imports inside function — no module-level state.
    ResourceExhausted → 60s sleep → retry. Other errors → exponential back-off → raise.
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    delay    = RETRY_BASE_DELAY
    last_exc = None

    for attempt in range(MAX_RETRIES):
        try:
            response = genai.embed_content(
                model=EMBEDDING_MODEL,
                content=text,
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=EMBEDDING_DIMS,
            )
            return response["embedding"]
        except ResourceExhausted as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(RATE_LIMIT_SLEEP)
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                time.sleep(delay)
                delay *= 2

    raise RuntimeError(
        f"Query embedding failed after {MAX_RETRIES} attempts: {last_exc}"
    ) from last_exc


def search_all_tables(query_vector: list) -> list:
    """
    Search all v1_* LanceDB tables for top-K results.
    Returns list of dicts: {doctype, name, text, _distance}.
    Returns [] if no v1_* tables exist (FR-011: friendly message path, not exception).
    All lancedb imports inside function — no module-level state.
    Path always via frappe.get_site_path() — never hardcoded.
    """
    import lancedb

    rag_path = frappe.get_site_path("private", "files", "rag")
    db       = lancedb.connect(rag_path)

    table_names = [t for t in db.table_names() if t.startswith("v1_")]
    if not table_names:
        return []

    results = []
    for table_name in table_names:
        table = db.open_table(table_name)
        rows  = (
            table.search(query_vector, vector_column_name="vector")
            .limit(TOP_K)
            .to_list()
        )
        for row in rows:
            dist = row.get("_distance", 0)
            if dist <= MAX_DISTANCE:
                results.append({
                    "doctype":   row["doctype"],
                    "name":      row["name"],
                    "text":      row["text"],
                    "_distance": dist,
                })

    results.sort(key=lambda r: r["_distance"])
    return results


def filter_by_permission(candidates: list, user: str) -> list:
    """
    Filter retrieval candidates through frappe.has_permission() for the calling user.
    Returns only records the user is authorised to read (Principle III).
    Called after search_all_tables(), before build_messages().
    """
    allowed = []
    for candidate in candidates:
        if frappe.has_permission(
            candidate["doctype"],
            doc=candidate["name"],
            ptype="read",
            user=user,
        ):
            allowed.append(candidate)
    return allowed
