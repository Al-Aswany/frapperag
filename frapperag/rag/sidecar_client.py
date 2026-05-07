"""Sidecar HTTP client for Frappe worker processes.

This is the ONLY file that may be imported in Frappe worker code to communicate
with the RAG sidecar. Workers MUST NOT import lancedb or sentence_transformers
directly (Constitution Principle IV / Sidecar HTTP only Development Workflow rule).

All public functions use httpx via _retry_call, which retries up to 3 times with
exponential back-off (1s → 2s) on transient errors before raising
SidecarUnavailableError. Permanent 4xx errors raise SidecarPermanentError
immediately without retrying. Both exception classes are defined here so callers
(indexer.py, chat_runner.py) can import and handle them explicitly.

Implementation rule: httpx is imported INSIDE each function (not at module level)
to preserve the per-function heavy-import isolation pattern used throughout the app.
"""


class SidecarError(Exception):
    """Raised when the sidecar returns a non-2xx response or is unreachable."""
    pass


class SidecarUnavailableError(SidecarError):
    """Raised when the sidecar is unreachable after all retry attempts.

    Covers: connection refused, timeout, and HTTP 429/502/503 exhaustion.
    """
    pass


class SidecarPermanentError(SidecarError):
    """Raised when the sidecar returns a 4xx (client error) that should not be retried.

    `status_code` is set to the HTTP status code when available.
    """
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _get_port() -> int:
    """Read sidecar_port from AI Assistant Settings.

    frappe is available in worker context but NOT in sidecar context —
    this function must never be called from sidecar/main.py or sidecar/store.py.
    """
    import frappe
    try:
        port = frappe.get_cached_doc("AI Assistant Settings").sidecar_port
        return int(port) if port else 8100
    except Exception:
        return 8100


def _base_url(port: int | None) -> str:
    resolved = port if port is not None else _get_port()
    return f"http://127.0.0.1:{resolved}"


def _active_table_prefix() -> str:
    """Return the LanceDB table prefix for the currently configured embedding provider."""
    import frappe
    try:
        provider = frappe.get_cached_doc("AI Assistant Settings").embedding_provider or "gemini"
    except Exception:
        provider = "gemini"
    if provider == "e5-small":
        return "v6_e5small_"
    return "v5_gemini_"


def _retry_call(fn, *args, **kwargs):
    """Call fn(*args, **kwargs) up to 3 times with exponential back-off.

    fn must be an httpx request function (post, get, delete, …) that returns
    an httpx.Response without raise_for_status called.

    Retry policy:
    - Transient (retried):  httpx.ConnectError, httpx.TimeoutException, HTTP 429/502/503
    - Permanent (not retried): HTTP 4xx (except 429) → SidecarPermanentError raised immediately
    - After 3 failed attempts: SidecarUnavailableError raised
    - Other HTTP errors (5xx except 502/503): SidecarError raised immediately

    Log format: [RETRY] attempt=N/3 delay=Xs error=<ExceptionType or HTTPNxx>
    """
    import time
    import httpx
    import frappe

    max_attempts = 3
    delay = 1
    multiplier = 2

    for attempt in range(1, max_attempts + 1):
        try:
            response = fn(*args, **kwargs)
            sc = response.status_code

            if sc in {429, 502, 503}:
                # Transient HTTP status — retry with back-off
                if attempt < max_attempts:
                    frappe.logger("frapperag").warning(
                        f"[RETRY] attempt={attempt}/{max_attempts} delay={delay}s error=HTTP{sc}"
                    )
                    time.sleep(delay)
                    delay *= multiplier
                    continue
                else:
                    target = args[0] if args else "unknown"
                    raise SidecarUnavailableError(
                        f"Sidecar unavailable after {max_attempts} retries"
                        f" ({target} → HTTP {sc})"
                        " — check rag_sidecar logs."
                    )
            elif 400 <= sc < 500:
                # Permanent client error — do not retry
                raise SidecarPermanentError(
                    f"Sidecar returned HTTP {sc}: {response.text[:200]}",
                    status_code=sc,
                )
            elif sc >= 300:
                # Non-transient server error (5xx other than 502/503)
                raise SidecarError(
                    f"Sidecar returned HTTP {sc}: {response.text[:500]}"
                )
            # 2xx — success
            return response

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            if attempt < max_attempts:
                frappe.logger("frapperag").warning(
                    f"[RETRY] attempt={attempt}/{max_attempts} delay={delay}s error={type(exc).__name__}"
                )
                time.sleep(delay)
                delay *= multiplier
            else:
                target = args[0] if args else "unknown"
                raise SidecarUnavailableError(
                    f"Sidecar unavailable after {max_attempts} retries ({target})"
                    " — is rag_sidecar running? Check `bench start` logs."
                ) from exc
        except (SidecarUnavailableError, SidecarPermanentError, SidecarError):
            raise


def health_check(port: int | None = None) -> dict:
    """GET /health — non-raising liveness probe.

    Returns {"ok": bool, "url": str, "detail": str | None}.
    Never raises; always returns a dict so callers can surface the result directly.
    """
    import httpx

    url = f"{_base_url(port)}/health"
    try:
        r = httpx.get(url, timeout=5.0)
        if r.status_code == 200:
            return {"ok": True, "url": url, "detail": None}
        return {"ok": False, "url": url, "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return {"ok": False, "url": url, "detail": f"{type(exc).__name__} — is rag_sidecar running? Check `bench start` logs."}
    except Exception as exc:
        return {"ok": False, "url": url, "detail": str(exc)}


def search(
    text: str,
    top_k: int = 5,
    max_distance: float = 1.0,
    api_key: str | None = None,
    port: int | None = None,
) -> list:
    """POST /search — embed query text via sidecar and search all active-prefix tables.

    Returns a list of dicts: {doctype, name, text, _distance} sorted by distance.
    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx

    url = f"{_base_url(port)}/search"
    payload = {"text": text, "top_k": top_k, "max_distance": max_distance}
    if api_key:
        payload["api_key"] = api_key
    response = _retry_call(httpx.post, url, json=payload, timeout=30.0)
    return response.json()["results"]


def upsert_record(
    doctype: str,
    name: str,
    text: str,
    api_key: str | None = None,
    port: int | None = None,
) -> None:
    """POST /upsert — embed text via sidecar and store in the active-prefix table.

    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx

    url = f"{_base_url(port)}/upsert"
    payload = {"doctype": doctype, "name": name, "text": text}
    if api_key:
        payload["api_key"] = api_key
    _retry_call(httpx.post, url, json=payload, timeout=30.0)


def upsert_batch(
    records: list[dict],
    api_key: str | None = None,
    port: int | None = None,
) -> None:
    """POST /upsert_batch — embed and upsert multiple records in one sidecar call.

    `records` is a list of {doctype, name, text} dicts (same keys as upsert_record).
    The sidecar embeds all texts in a single batchEmbedContents call and writes
    one merge_insert per table, making bulk indexing ~WRITE_BATCH_SIZE× faster
    than calling upsert_record() per record.

    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx

    url = f"{_base_url(port)}/upsert_batch"
    payload = {"records": records}
    if api_key:
        payload["api_key"] = api_key
    _retry_call(httpx.post, url, json=payload, timeout=120.0)


def delete_record(doctype: str, name: str, port: int | None = None) -> None:
    """DELETE /record/{table}/{record_id} — remove one vector entry.

    Idempotent — no error if the record does not exist (sidecar returns 200).
    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx
    import urllib.parse

    table = _active_table_prefix() + doctype.lower().replace(" ", "_")
    record_id = f"{doctype}:{name}"
    # URL-encode the colon in the record_id component
    encoded_id = urllib.parse.quote(record_id, safe="")
    url = f"{_base_url(port)}/record/{table}/{encoded_id}"
    _retry_call(httpx.delete, url, timeout=30.0)


def chat(
    messages: list,
    api_key: str,
    model: str = "gemini-2.5-flash",
    tools: list | None = None,
    google_search: dict | None = None,
    port: int | None = None,
) -> dict:
    """POST /chat — call Gemini via the sidecar with a pre-assembled message list.

    The sidecar keeps the Gemini SDK and GenerativeModel initialized across calls,
    eliminating per-job cold-start overhead.

    `messages` is a list of {role, parts} dicts (the full conversation history
    including the final user turn).

    Accepts an optional tools list (list of function-declaration dicts) passed through to the sidecar.

    Returns {"text": str, "tokens_used": int}.
    Uses a 180s timeout to allow for the sidecar's internal 15s rate-limit retry plus a full Gemini round trip.
    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx

    url = f"{_base_url(port)}/chat"
    payload = {"messages": messages, "api_key": api_key, "model": model}
    if tools:
        payload["tools"] = tools
    if google_search:
        payload["google_search"] = google_search
    response = _retry_call(httpx.post, url, json=payload, timeout=180.0)
    return response.json()


def drop_table(doctype: str, port: int | None = None) -> None:
    """DELETE /table/{table} — drop the active-prefix table for a DocType.

    Used when a DocType is removed from the whitelist (purge job).
    Idempotent — no error if the table does not exist.
    Raises SidecarUnavailableError after 3 failed connection/timeout/transient attempts.
    Raises SidecarPermanentError on 4xx client errors.
    Raises SidecarError on other non-2xx responses.
    """
    import httpx

    table = _active_table_prefix() + doctype.lower().replace(" ", "_")
    url = f"{_base_url(port)}/table/{table}"
    _retry_call(httpx.delete, url, timeout=30.0)


def tables_populated(prefix: str, port: int | None = None) -> dict:
    """GET /tables/populated — check if any tables exist under a prefix.

    Returns {"populated": bool, "tables": [...], "prefix": str}.
    Used by the dashboard banner and get_active_prefix_status().
    """
    import httpx

    url = f"{_base_url(port)}/tables/populated"
    response = _retry_call(httpx.get, url, params={"prefix": prefix}, timeout=10.0)
    return response.json()


def install_local_model(hf_token: str | None = None, port: int | None = None) -> dict:
    """POST /install_local_model — start a background download of e5-small in the sidecar.

    Returns {"install_id": str}.
    """
    import httpx

    url = f"{_base_url(port)}/install_local_model"
    payload = {"hf_token": hf_token}
    response = _retry_call(httpx.post, url, json=payload, timeout=30.0)
    return response.json()


def install_local_model_status(install_id: str, port: int | None = None) -> dict:
    """GET /install_local_model/status/{install_id} — poll install progress."""
    import httpx

    url = f"{_base_url(port)}/install_local_model/status/{install_id}"
    response = _retry_call(httpx.get, url, timeout=10.0)
    return response.json()
