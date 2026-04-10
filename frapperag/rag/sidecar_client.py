"""Sidecar HTTP client for Frappe worker processes.

This is the ONLY file that may be imported in Frappe worker code to communicate
with the RAG sidecar. Workers MUST NOT import lancedb or sentence_transformers
directly (Constitution Principle IV / Sidecar HTTP only Development Workflow rule).

All functions use httpx with a 30-second timeout. They raise SidecarError on any
HTTP error, connection failure, or timeout so callers (sync_runner.py) can mark
the sync job as Failed without crashing the worker process.

Implementation rule: httpx is imported INSIDE each function (not at module level)
to preserve the per-function heavy-import isolation pattern used throughout the app.
"""


class SidecarError(Exception):
    """Raised when the sidecar returns a non-2xx response or is unreachable."""
    pass


def _get_port() -> int:
    """Read sidecar_port from AI Assistant Settings.

    frappe is available in worker context but NOT in sidecar context —
    this function must never be called from sidecar/main.py or sidecar/store.py.
    """
    import frappe
    try:
        port = frappe.get_doc("AI Assistant Settings").sidecar_port
        return int(port) if port else 8100
    except Exception:
        return 8100


def _base_url(port: int | None) -> str:
    resolved = port if port is not None else _get_port()
    return f"http://127.0.0.1:{resolved}"


def search(
    text: str,
    top_k: int = 5,
    max_distance: float = 1.0,
    port: int | None = None,
) -> list:
    """POST /search — embed query text via sidecar and search all v3_* tables.

    Returns a list of dicts: {doctype, name, text, _distance} sorted by distance.
    Raises SidecarError on HTTP error, connection failure, or timeout.
    """
    import httpx

    url = f"{_base_url(port)}/search"
    payload = {"text": text, "top_k": top_k, "max_distance": max_distance}
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        response.raise_for_status()
        return response.json()["results"]
    except httpx.ConnectError as exc:
        raise SidecarError(
            f"Cannot connect to RAG sidecar at {url}. "
            "Is the sidecar running? Check bench Procfile. "
            f"Original error: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise SidecarError(
            f"Sidecar /search returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SidecarError(f"Sidecar /search timed out after 30s: {exc}") from exc


def upsert_record(doctype: str, name: str, text: str, port: int | None = None) -> None:
    """POST /upsert — embed text via sidecar and store in the v3_ table.

    Raises SidecarError on HTTP error or connection failure.
    """
    import httpx

    url = f"{_base_url(port)}/upsert"
    payload = {"doctype": doctype, "name": name, "text": text}
    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        raise SidecarError(
            f"Cannot connect to RAG sidecar at {url}. "
            "Is the sidecar running? Check bench Procfile. "
            f"Original error: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise SidecarError(
            f"Sidecar /upsert returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SidecarError(f"Sidecar /upsert timed out after 30s: {exc}") from exc


def delete_record(doctype: str, name: str, port: int | None = None) -> None:
    """DELETE /record/{table}/{record_id} — remove one vector entry.

    Idempotent — no error if the record does not exist (sidecar returns 200).
    Raises SidecarError on HTTP error or connection failure.
    """
    import httpx

    table = "v3_" + doctype.lower().replace(" ", "_")
    record_id = f"{doctype}:{name}"
    # URL-encode the colon in the record_id component
    import urllib.parse
    encoded_id = urllib.parse.quote(record_id, safe="")
    url = f"{_base_url(port)}/record/{table}/{encoded_id}"
    try:
        response = httpx.delete(url, timeout=30.0)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        raise SidecarError(
            f"Cannot connect to RAG sidecar at {url}. "
            f"Original error: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise SidecarError(
            f"Sidecar /record delete returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SidecarError(f"Sidecar /record delete timed out after 30s: {exc}") from exc


def chat(
    messages: list,
    api_key: str,
    model: str = "gemini-2.5-flash",
    tools: list | None = None,
    port: int | None = None,
) -> dict:
    """POST /chat — call Gemini via the sidecar with a pre-assembled message list.

    The sidecar keeps the Gemini SDK and GenerativeModel initialized across calls,
    eliminating per-job cold-start overhead.

    `messages` is a list of {role, parts} dicts (the full conversation history
    including the final user turn).

    Accepts an optional tools list (list of function-declaration dicts) passed through to the sidecar.

    Returns {"text": str, "tokens_used": int}.
    Raises SidecarError on HTTP error, connection failure, or timeout (120s to
    allow for the sidecar's internal 60s rate-limit retry).
    """
    import httpx

    url = f"{_base_url(port)}/chat"
    payload = {"messages": messages, "api_key": api_key, "model": model}
    if tools:
        payload["tools"] = tools
    try:
        response = httpx.post(url, json=payload, timeout=120.0)
        response.raise_for_status()
        return response.json()
    except httpx.ConnectError as exc:
        raise SidecarError(
            f"Cannot connect to RAG sidecar at {url}. "
            "Is the sidecar running? Check bench Procfile. "
            f"Original error: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise SidecarError(
            f"Sidecar /chat returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SidecarError(f"Sidecar /chat timed out after 120s: {exc}") from exc


def drop_table(doctype: str, port: int | None = None) -> None:
    """DELETE /table/{table} — drop the entire v3_ table for a DocType.

    Used when a DocType is removed from the whitelist (purge job).
    Idempotent — no error if the table does not exist.
    Raises SidecarError on HTTP error or connection failure.
    """
    import httpx

    table = "v3_" + doctype.lower().replace(" ", "_")
    url = f"{_base_url(port)}/table/{table}"
    try:
        response = httpx.delete(url, timeout=30.0)
        response.raise_for_status()
    except httpx.ConnectError as exc:
        raise SidecarError(
            f"Cannot connect to RAG sidecar at {url}. "
            f"Original error: {exc}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise SidecarError(
            f"Sidecar /table delete returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:500]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise SidecarError(f"Sidecar /table delete timed out after 30s: {exc}") from exc
