import frappe

TOP_K        = 5
MAX_DISTANCE = 1.0   # cosine distance ceiling; results above this are irrelevant


def search_candidates(text: str, api_key: str | None = None) -> list:
    """Embed a query and search the active-prefix LanceDB tables via the RAG sidecar.

    Single HTTP call to POST /search — the sidecar handles embedding (with the
    correct "query: " prefix) and vector search internally.
    Returns list of dicts: {doctype, name, text, _distance}, sorted by distance.
    Returns [] when no active-prefix tables exist or the sidecar returns no matches.

    Raises SidecarError on connection failure or HTTP error.
    """
    from frapperag.rag.sidecar_client import search, SidecarError

    return search(text, top_k=TOP_K, max_distance=MAX_DISTANCE, api_key=api_key)


def filter_by_permission(candidates: list, user: str) -> list:
    """Filter retrieval candidates through frappe.has_permission() for the calling user.

    Returns only records the user is authorised to read (Principle III).
    Called after search_candidates(), before build_messages().
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
