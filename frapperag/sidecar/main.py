"""RAG Sidecar — FastAPI application.

Started by `bench start` via the Procfile entry added by after_install().
Binds to localhost only (Constitution Principle IV).

Phase 7A keeps chat startup lightweight by initialising only the Gemini chat
runtime during `sync_startup()`. Optional vector dependencies (LanceDB,
PyArrow, sentence-transformers, local model warmup) are loaded lazily only
when vector endpoints are used. The early port check still runs before
`uvicorn.run` so stale sidecars fail fast.

Usage:
    python main.py --port 8100
    uvicorn frapperag.sidecar.main:app --host 127.0.0.1 --port 8100
"""

import errno
import importlib
import logging
import os
import socket
import sys
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [sidecar] %(levelname)s: %(message)s",
)
log = logging.getLogger("rag_sidecar")

# ---------------------------------------------------------------------------
# Module-level state — set lazily at runtime, NOT at import time
# ---------------------------------------------------------------------------

_provider = None  # EmbeddingProvider instance (GeminiProvider or E5SmallProvider)
_startup_ready = False
_store_ready = False
_provider_warmed = False
_vector_init_lock = threading.Lock()

# Gemini SDK state — configured lazily on first /chat request, reused thereafter
_genai_api_key: str | None = None
_genai_client = None

# Install state — keyed by install_id
_install_state: dict[str, dict] = {}

_DEFAULT_CHAT_MODEL = "gemini-2.5-flash"
_FEATURE_UNAVAILABLE_ERROR_CODE = "feature_unavailable"
_GOOGLE_SEARCH_ALLOWED_INTENTS = frozenset({
    "erpnext_help",
    "out_of_scope",
    "web_current_info",
})


def _resolve_rag_dir() -> str:
    """Bench-level rag/ directory from RAG_DIR or a path relative to this file."""
    rag_dir = os.environ.get(
        "RAG_DIR",
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "rag"),
    )
    rag_dir = os.path.realpath(rag_dir)
    os.makedirs(rag_dir, exist_ok=True)
    return rag_dir


def _linux_pids_listening_on_tcp_port(port: int, host: str) -> list[int]:
    """Find PIDs listening on host:port using only /proc (no ss/lsof/fuser).

    Best-effort: may miss PIDs if /proc/pid/fd is unreadable. Linux only.
    """
    if sys.platform != "linux" or not os.path.isfile("/proc/net/tcp"):
        return []

    want_ips = {host}
    if host == "127.0.0.1":
        want_ips.add("0.0.0.0")

    inodes: set[str] = set()
    try:
        with open("/proc/net/tcp") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) < 10 or parts[3] != "0A":  # LISTEN
                    continue
                lip_hex, lport_hex = parts[1].split(":")
                if int(lport_hex, 16) != port:
                    continue
                lip = socket.inet_ntoa(bytes.fromhex(lip_hex)[::-1])
                if lip not in want_ips:
                    continue
                ino = parts[9]
                if ino.isdigit():
                    inodes.add(ino)
    except OSError:
        return []

    if not inodes:
        return []

    found: set[int] = set()
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            fd_dir = os.path.join("/proc", name, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    link = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if link.startswith("socket:[") and link.endswith("]"):
                    if link[8:-1] in inodes:
                        found.add(pid)
                        break
    except OSError:
        return []

    return sorted(found)


def assert_port_free(host: str, port: int) -> None:
    """Fail fast if nothing can bind to host:port (e.g. stale sidecar).

    Uvicorn binds only after lifespan; without this check, a taken port is
    reported only after a long model load.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            pids = _linux_pids_listening_on_tcp_port(port, host)
            pid_hint = (
                f" Likely process(es): PID {', '.join(map(str, pids))} — run "
                f"`kill {' '.join(map(str, pids))}` (or `kill -9 …` if needed)."
                if pids
                else ""
            )
            tools_hint = (
                " If you have them: `ss -tlnp | grep :%s` or `fuser -k %s/tcp`."
                % (port, port)
            )
            log.error(
                "Cannot bind %s:%s — %s.%s%s Stop any other `bench start` "
                "or old sidecar using this port.",
                host,
                port,
                exc.strerror.lower() if exc.strerror else exc,
                pid_hint,
                tools_hint,
            )
            raise SystemExit(1) from exc
        raise
    finally:
        s.close()


def sync_startup() -> None:
    """Prepare chat/runtime imports only.

    Phase 7A keeps vector dependencies optional, so startup must not require
    LanceDB, PyArrow, sentence-transformers, or a local model. Those are
    initialised lazily when vector endpoints are used.
    """
    global _startup_ready
    if _startup_ready:
        return

    log.info("Startup: pre-importing google.genai (warms module cache)")
    import google.genai  # noqa: F401
    log.info("Startup: google.genai imported")
    _startup_ready = True


def _provider_name() -> str:
    return (os.environ.get("EMBEDDING_PROVIDER", "gemini") or "gemini").strip() or "gemini"


def _provider_prefix(provider_name: str) -> str:
    return "v6_e5small_" if provider_name == "e5-small" else "v5_gemini_"


def _provider_dim(provider_name: str) -> int:
    return 384 if provider_name == "e5-small" else 768


def _check_optional_import(module_name: str) -> tuple[bool, str | None]:
    try:
        if importlib.util.find_spec(module_name) is None:
            return False, f"{module_name} unavailable: module not installed"
        return True, None
    except Exception as exc:
        return False, f"{module_name} unavailable: {exc}"


def _vector_capability_snapshot() -> dict:
    provider_name = _provider_name()
    prefix = _provider_prefix(provider_name)
    dim = _provider_dim(provider_name)

    has_lancedb, lancedb_reason = _check_optional_import("lancedb")
    has_pyarrow, pyarrow_reason = _check_optional_import("pyarrow")
    has_sentence_transformers, st_reason = _check_optional_import("sentence_transformers")
    has_huggingface_hub, hf_reason = _check_optional_import("huggingface_hub")

    local_embeddings_available = has_sentence_transformers and has_huggingface_hub
    vector_available = has_lancedb and has_pyarrow
    vector_reason_parts: list[str] = []

    if not has_lancedb:
        vector_reason_parts.append(lancedb_reason or "lancedb unavailable")
    if not has_pyarrow:
        vector_reason_parts.append(pyarrow_reason or "pyarrow unavailable")

    if provider_name == "e5-small" and not local_embeddings_available:
        vector_available = False
        if not has_sentence_transformers:
            vector_reason_parts.append(st_reason or "sentence_transformers unavailable")
        if not has_huggingface_hub:
            vector_reason_parts.append(hf_reason or "huggingface_hub unavailable")

    return {
        "provider": provider_name,
        "dim": dim,
        "table_prefix": prefix,
        "chat_available": True,
        "vector_available": bool(vector_available),
        "vector_reason": "; ".join(vector_reason_parts),
        "local_embeddings_available": bool(local_embeddings_available),
        "can_install_local_model": bool(local_embeddings_available),
    }


def _feature_unavailable_response(reason: str, **payload) -> JSONResponse:
    body = {
        "detail": reason,
        "error_code": _FEATURE_UNAVAILABLE_ERROR_CODE,
    }
    body.update(payload)
    return JSONResponse(status_code=409, content=body)


def _health_payload() -> dict:
    payload = _vector_capability_snapshot()
    payload.update({
        "status": "ok" if _startup_ready else "starting",
        "startup_ready": _startup_ready,
        "vector_initialized": _store_ready,
        "model_loaded": _provider_warmed,
    })
    return payload


def _ensure_vector_backend(*, need_embeddings: bool) -> tuple[object | None, dict]:
    global _provider, _store_ready, _provider_warmed

    capability = _vector_capability_snapshot()
    if not capability["vector_available"]:
        return None, capability

    with _vector_init_lock:
        capability = _vector_capability_snapshot()
        if not capability["vector_available"]:
            return None, capability

        if _provider is None:
            from frapperag.sidecar.providers import build_provider

            _provider = build_provider(capability["provider"])

        if not _store_ready:
            rag_dir = _resolve_rag_dir()
            from frapperag.sidecar.store import configure_provider, init_store

            configure_provider(_provider.dim, _provider.table_prefix)
            init_store(rag_dir)
            _store_ready = True
            log.info(
                "Startup: vector store ready provider=%s dim=%d prefix=%s",
                _provider.name,
                _provider.dim,
                _provider.table_prefix,
            )

        if need_embeddings and not _provider_warmed:
            _provider.warmup()
            _provider_warmed = True

    return _provider, capability


# ---------------------------------------------------------------------------
# Lifespan: optional init when the app is started without __main__ (e.g. uvicorn CLI)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — full init if `sync_startup` was not run earlier."""
    global _genai_api_key, _genai_client, _provider
    global _provider_warmed, _startup_ready, _store_ready

    if not _startup_ready:
        sync_startup()

    yield

    log.info("Shutdown: sidecar stopping")
    if _genai_client is not None:
        _close_genai_client(_genai_client)
    _genai_api_key = None
    _genai_client = None
    _provider = None
    _store_ready = False
    _provider_warmed = False
    _startup_ready = False


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="FrappeRAG Sidecar", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class EmbedRequest(BaseModel):
    texts: list[str]
    mode: str = "passage"  # "passage" for indexing, "query" for retrieval
    api_key: str | None = None


class SearchRequest(BaseModel):
    text: str
    top_k: int = 5
    max_distance: float = 1.0
    api_key: str | None = None


class UpsertRequest(BaseModel):
    doctype: str
    name: str
    text: str
    api_key: str | None = None


class UpsertBatchItem(BaseModel):
    doctype: str
    name: str
    text: str


class UpsertBatchRequest(BaseModel):
    records: list[UpsertBatchItem]
    api_key: str | None = None


class ChatMessage(BaseModel):
    role: str
    parts: list[str]


class ChatGoogleSearch(BaseModel):
    enabled: bool = False
    intent: str | None = None


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    api_key: str
    model: str = _DEFAULT_CHAT_MODEL
    tools: list[dict] | None = None  # per-report function declarations
    google_search: ChatGoogleSearch | None = None


class InstallReq(BaseModel):
    hf_token: str | None = None


def _close_genai_client(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception as exc:
            log.warning("Shutdown: closing google.genai client failed: %s", exc)


def _get_genai_client(api_key: str):
    global _genai_api_key, _genai_client

    from google import genai

    if _genai_client is None or api_key != _genai_api_key:
        if _genai_client is not None:
            _close_genai_client(_genai_client)
        _genai_client = genai.Client(api_key=api_key)
        _genai_api_key = api_key

    return _genai_client


def _normalize_schema(value):
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "type" and isinstance(item, str):
                normalized[key] = item.lower()
            else:
                normalized[key] = _normalize_schema(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_schema(item) for item in value]
    return value


def _build_function_tools(raw_tools: list[dict], types_mod) -> list:
    tool_objects = []

    for tool_block in raw_tools or []:
        declarations = []
        for fd in tool_block.get("function_declarations", []):
            schema = fd.get("parameters_json_schema") or fd.get("parameters") or {
                "type": "object",
                "properties": {},
            }
            declarations.append(
                types_mod.FunctionDeclaration(
                    name=fd["name"],
                    description=fd.get("description", ""),
                    parameters_json_schema=_normalize_schema(schema),
                )
            )

        if declarations:
            tool_objects.append(types_mod.Tool(function_declarations=declarations))

    return tool_objects


def _messages_to_contents(messages: list[ChatMessage]) -> list[dict]:
    return [
        {
            "role": message.role,
            "parts": [{"text": part} for part in message.parts],
        }
        for message in messages
    ]


def _messages_to_interaction_input(messages: list[ChatMessage]) -> str:
    chunks = []
    for message in messages:
        text = "\n".join(part for part in message.parts if part)
        if not text:
            continue
        role = "Assistant" if message.role == "model" else "User"
        chunks.append(f"{role}: {text}")
    return "\n\n".join(chunks)


def _request_has_erp_context(messages: list[ChatMessage]) -> bool:
    return any(
        "Context from ERP data:" in part
        for message in messages
        for part in message.parts
    )


def _extract_tokens_used(response) -> int:
    usage = getattr(response, "usage_metadata", None)
    if not usage:
        return 0

    for attr in ("total_token_count", "total_tokens"):
        value = getattr(usage, attr, None)
        if isinstance(value, int):
            return value

    total = 0
    for attr in (
        "prompt_token_count",
        "candidates_token_count",
        "tool_use_prompt_token_count",
        "thoughts_token_count",
    ):
        value = getattr(usage, attr, 0) or 0
        if isinstance(value, int):
            total += value
    return total


def _extract_tool_call(response) -> dict | None:
    for function_call in getattr(response, "function_calls", None) or []:
        nested = getattr(function_call, "function_call", None)
        name = getattr(function_call, "name", None) or getattr(nested, "name", None)
        args = getattr(function_call, "args", None)
        if args is None and nested is not None:
            args = getattr(nested, "args", None)
        if name:
            return {"name": name, "args": dict(args or {})}

    parts = (
        response.candidates[0].content.parts
        if getattr(response, "candidates", None)
        and response.candidates[0].content
        and response.candidates[0].content.parts
        else []
    )
    for part in parts:
        function_call = getattr(part, "function_call", None)
        if function_call and function_call.name:
            return {"name": function_call.name, "args": dict(function_call.args or {})}

    return None


def _extract_text(response) -> str:
    try:
        text = getattr(response, "text", None)
    except Exception:
        text = None

    if text:
        return text

    parts = (
        response.candidates[0].content.parts
        if getattr(response, "candidates", None)
        and response.candidates[0].content
        and response.candidates[0].content.parts
        else []
    )
    return "".join(getattr(part, "text", "") for part in parts if getattr(part, "text", ""))


def _extract_interaction_text(interaction) -> str:
    for output in getattr(interaction, "outputs", None) or []:
        if getattr(output, "type", None) == "text" and getattr(output, "text", None):
            return output.text
    return ""


def _validate_google_search_request(req: ChatRequest) -> None:
    if not req.google_search or not req.google_search.enabled:
        return

    if not req.google_search.intent:
        raise HTTPException(status_code=422, detail="google_search.intent is required when Google Search is enabled")

    if req.google_search.intent not in _GOOGLE_SEARCH_ALLOWED_INTENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                "Google Search is restricted to future routed intents: "
                f"{', '.join(sorted(_GOOGLE_SEARCH_ALLOWED_INTENTS))}"
            ),
        )

    if req.tools:
        raise HTTPException(
            status_code=422,
            detail="Google Search grounding is not supported together with function tools on /chat",
        )

    if _request_has_erp_context(req.messages):
        raise HTTPException(
            status_code=422,
            detail="Google Search grounding cannot be combined with ERP context",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness check. Returns 200 when the sidecar is ready."""
    return _health_payload()


@app.post("/embed")
def embed(req: EmbedRequest):
    """Embed a list of texts using the active embedding provider."""
    if not req.texts:
        raise HTTPException(status_code=422, detail="texts must be a non-empty list")

    provider, capability = _ensure_vector_backend(need_embeddings=True)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    try:
        vectors = provider.embed(req.texts, req.mode, req.api_key)
        return {"vectors": vectors}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Embed failed: {exc}")


@app.post("/search")
def search(req: SearchRequest):
    """Embed a query text and search all active-prefix LanceDB tables.

    Returns candidates sorted by ascending cosine distance, filtered by max_distance.
    """
    if not req.text:
        raise HTTPException(status_code=422, detail="text must be non-empty")

    provider, capability = _ensure_vector_backend(need_embeddings=True)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    from frapperag.sidecar.store import search_all_active_tables
    import time as _time

    try:
        t0 = _time.monotonic()
        vector = provider.embed([req.text], "query", req.api_key)[0]
        log.info("[TIMING][/search] embed %.3fs", _time.monotonic() - t0)

        t0 = _time.monotonic()
        results = search_all_active_tables(vector, top_k=req.top_k, max_distance=req.max_distance)
        log.info("[TIMING][/search] vector_search %.3fs → %d results", _time.monotonic() - t0, len(results))

        return {"results": results}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("search failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}")


@app.post("/upsert")
def upsert(req: UpsertRequest):
    """Embed one record's text and upsert its vector into the active-prefix LanceDB table.

    Creates the table if it does not exist.
    """
    if not req.doctype or not req.name or not req.text:
        raise HTTPException(status_code=422, detail="doctype, name, and text are required")

    provider, capability = _ensure_vector_backend(need_embeddings=True)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    from frapperag.sidecar.store import table_name_for, record_id_for, upsert_rows

    try:
        vector = provider.embed([req.text], "passage", req.api_key)[0]

        table_name = table_name_for(req.doctype)
        row = {
            "id":            record_id_for(req.doctype, req.name),
            "doctype":       req.doctype,
            "name":          req.name,
            "text":          req.text,
            "vector":        vector,
            "last_modified": "",
        }
        upsert_rows(table_name, [row])
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upsert failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Upsert failed: {exc}")


@app.post("/upsert_batch")
def upsert_batch(req: UpsertBatchRequest):
    """Embed a batch of records in one Gemini call and upsert all into LanceDB.

    Groups rows by table so a mixed-doctype batch (e.g. Customer + Supplier)
    issues one merge_insert per table, not one per record.
    """
    if not req.records:
        raise HTTPException(status_code=422, detail="records must be non-empty")

    provider, capability = _ensure_vector_backend(need_embeddings=True)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    from frapperag.sidecar.store import table_name_for, record_id_for, upsert_rows
    import time as _time

    try:
        t0 = _time.monotonic()
        texts = [r.text for r in req.records]
        vectors = provider.embed(texts, "passage", req.api_key)
        log.info("[TIMING][/upsert_batch] embed %d texts %.3fs", len(texts), _time.monotonic() - t0)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upsert_batch embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Embed failed: {exc}")

    rows_by_table: dict[str, list] = {}
    for rec, vector in zip(req.records, vectors):
        table_name = table_name_for(rec.doctype)
        rows_by_table.setdefault(table_name, []).append({
            "id":            record_id_for(rec.doctype, rec.name),
            "doctype":       rec.doctype,
            "name":          rec.name,
            "text":          rec.text,
            "vector":        vector,
            "last_modified": "",
        })

    try:
        t0 = _time.monotonic()
        for table_name, rows in rows_by_table.items():
            upsert_rows(table_name, rows)
        log.info("[TIMING][/upsert_batch] lancedb_write %.3fs", _time.monotonic() - t0)
    except Exception as exc:
        log.exception("upsert_batch lancedb write failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"LanceDB write failed: {exc}")

    return {"ok": True, "count": len(req.records)}


_CHAT_RATE_LIMIT_SLEEP = 15.0  # seconds — kept well below the 180s httpx timeout in sidecar_client


@app.post("/chat")
def chat(req: ChatRequest):
    """Call Gemini with the assembled conversation history.

    The request/response contract is kept compatible with the existing sidecar
    `/chat` flow. Google Search grounding is optional, disabled by default in
    app settings, and must be explicitly requested by future routed callers.
    """
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must be non-empty")

    import time
    from google.genai import errors, types

    _validate_google_search_request(req)
    client = _get_genai_client(req.api_key)
    response = None

    for attempt in range(2):
        try:
            t0 = time.monotonic()
            if req.google_search and req.google_search.enabled:
                response = client.interactions.create(
                    model=req.model or _DEFAULT_CHAT_MODEL,
                    input=_messages_to_interaction_input(req.messages),
                    tools=[{"type": "google_search"}],
                )
                log.info("[TIMING][/chat] interactions.create %.3fs", time.monotonic() - t0)
            else:
                config = None
                if req.tools:
                    config = types.GenerateContentConfig(
                        tools=_build_function_tools(req.tools, types),
                        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    )

                response = client.models.generate_content(
                    model=req.model or _DEFAULT_CHAT_MODEL,
                    contents=_messages_to_contents(req.messages),
                    config=config,
                )
                log.info("[TIMING][/chat] generate_content %.3fs", time.monotonic() - t0)
            break
        except errors.APIError as exc:
            if getattr(exc, "code", None) == 429 and attempt == 0:
                log.warning("/chat: Gemini rate-limited — sleeping %.0fs before retry", _CHAT_RATE_LIMIT_SLEEP)
                time.sleep(_CHAT_RATE_LIMIT_SLEEP)
                continue
            if getattr(exc, "code", None) == 429:
                raise HTTPException(status_code=429, detail="Gemini API rate limit exceeded after retry")
            log.exception("/chat: Gemini API call failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")
        except Exception as exc:
            log.exception("/chat: Gemini call failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    tokens_used = _extract_tokens_used(response)

    if not (req.google_search and req.google_search.enabled):
        tool_call = _extract_tool_call(response)
        if tool_call:
            return {"tool_call": tool_call, "tokens_used": tokens_used}

        text = _extract_text(response)
        if not text:
            finish_reason = (
                response.candidates[0].finish_reason
                if getattr(response, "candidates", None)
                else "unknown"
            )
            log.warning("/chat: Gemini returned no content parts (finish_reason=%s)", finish_reason)
            return {"text": "", "tokens_used": tokens_used}

        return {"text": text, "tokens_used": tokens_used}

    text = _extract_interaction_text(response)
    if not text:
        log.warning("/chat: Google Search grounding returned no text output")
    return {"text": text, "tokens_used": tokens_used}


@app.delete("/record/{table}/{record_id:path}")
def delete_record(table: str, record_id: str):
    """Remove one vector entry from a LanceDB table by composite ID.

    Idempotent — no error if the record does not exist.
    """
    provider, capability = _ensure_vector_backend(need_embeddings=False)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    from frapperag.sidecar.store import delete_row

    try:
        found = delete_row(table, record_id)
        return {"ok": True, "found": found}
    except Exception as exc:
        log.exception("delete_record failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Delete failed: {exc}")


@app.delete("/table/{table}")
def delete_table(table: str):
    """Drop an entire LanceDB table.

    Idempotent — no error if the table does not exist.
    """
    provider, capability = _ensure_vector_backend(need_embeddings=False)
    if provider is None:
        return _feature_unavailable_response(
            capability["vector_reason"] or "Vector backend is unavailable.",
            **capability,
        )

    from frapperag.sidecar.store import drop_table

    try:
        existed = drop_table(table)
        return {"ok": True, "existed": existed}
    except Exception as exc:
        log.exception("delete_table failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Drop table failed: {exc}")


@app.get("/tables/populated")
def tables_populated(prefix: str = ""):
    """Return populated tables under the given prefix.

    Used by worker-side helpers to check if the active prefix has data.
    """
    provider, capability = _ensure_vector_backend(need_embeddings=False)
    if provider is None:
        active_prefix = prefix if prefix else capability["table_prefix"]
        return {
            "populated": False,
            "tables": [],
            "prefix": active_prefix,
            "available": False,
            "reason": capability["vector_reason"] or "Vector backend is unavailable.",
            "provider": capability["provider"],
            "local_embeddings_available": capability["local_embeddings_available"],
        }

    from frapperag.sidecar.store import list_populated_tables
    try:
        active_prefix = prefix if prefix else provider.table_prefix
        tables = list_populated_tables(active_prefix)
        return {
            "populated": len(tables) > 0,
            "tables": tables,
            "prefix": active_prefix,
            "available": True,
            "reason": "",
            "provider": capability["provider"],
            "local_embeddings_available": capability["local_embeddings_available"],
        }
    except Exception as exc:
        log.exception("tables_populated failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"tables_populated failed: {exc}")


@app.post("/install_local_model")
def install_local_model(req: InstallReq):
    """Kick off a background download and test of multilingual-e5-small.

    Returns an install_id immediately; poll /install_local_model/status/{install_id}
    for progress. The endpoint never changes embedding_provider — that is the
    worker/admin's responsibility after install succeeds.
    """
    capability = _vector_capability_snapshot()
    if not capability["local_embeddings_available"]:
        return _feature_unavailable_response(
            "Local embedding dependencies are not installed.",
            **capability,
        )

    install_id = uuid.uuid4().hex
    _install_state[install_id] = {
        "phase": "queued", "percent": 0,
        "terminal": False, "ok": False, "message": "",
    }
    threading.Thread(target=_do_install, args=(install_id, req.hf_token), daemon=True).start()
    return {"install_id": install_id}


@app.get("/install_local_model/status/{install_id}")
def install_local_model_status(install_id: str):
    """Poll install progress."""
    s = _install_state.get(install_id)
    if not s:
        raise HTTPException(404, "unknown install_id")
    return s


def _do_install(install_id: str, hf_token: str | None) -> None:
    state = _install_state[install_id]
    try:
        cache_dir = os.path.expanduser(
            "~/.cache/huggingface/hub/models--intfloat--multilingual-e5-small"
        )
        if os.path.isdir(cache_dir) and os.listdir(cache_dir):
            state.update(phase="cached", percent=80, message="Using existing snapshot")
        else:
            state.update(phase="download", percent=5, message="Starting download…")
            from huggingface_hub import snapshot_download
            snapshot_download("intfloat/multilingual-e5-small", token=hf_token or None)
            state.update(phase="download", percent=70, message="Download complete")

        state.update(phase="load", percent=85, message="Loading model into memory…")
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("intfloat/multilingual-e5-small")

        state.update(phase="test_embed", percent=95, message="Running test embedding…")
        v = m.encode(["test"], normalize_embeddings=True)
        assert len(v[0]) == 384

        state.update(phase="done", percent=100, terminal=True, ok=True,
                     dim=384, message="Local model ready")
    except MemoryError as exc:
        state.update(phase="failed", terminal=True, ok=False,
                     message=f"OOM during install: {exc}")
    except Exception as exc:
        state.update(phase="failed", terminal=True, ok=False, message=str(exc))


# ---------------------------------------------------------------------------
# Direct invocation entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="FrappeRAG sidecar process")
    parser.add_argument("--port", type=int, default=8100, help="Port to listen on (localhost only)")
    parser.add_argument("--rag-dir", type=str, default="", help="Path to bench-level rag/ directory")
    args = parser.parse_args()

    if args.rag_dir:
        os.environ["RAG_DIR"] = args.rag_dir

    # Import the package module so `_provider` / `app` are the same objects uvicorn will use.
    import frapperag.sidecar.main as sidecar_mod

    host = "127.0.0.1"
    sidecar_mod.assert_port_free(host, args.port)
    sidecar_mod.sync_startup()

    uvicorn.run(
        sidecar_mod.app,
        host=host,
        port=args.port,
        log_level="info",
    )
