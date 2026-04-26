"""RAG Sidecar — FastAPI application.

Started by `bench start` via the Procfile entry added by after_install().
Binds to localhost only (Constitution Principle IV).

Heavy work (LanceDB + embedding model) runs in `sync_startup()`, invoked
from `__main__` *before* `uvicorn.run`. Uvicorn runs ASGI lifespan startup
*before* it binds the listening socket; doing model load only in lifespan
meant a ~minute-long load followed by bind failure if the port was already
taken (e.g. a stale sidecar). Pre-startup init plus an early port check avoids
that and fails fast with a clear error.

Usage:
    python main.py --port 8100
    uvicorn frapperag.sidecar.main:app --host 127.0.0.1 --port 8100
"""

import errno
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
# Module-level state — set during lifespan startup, NOT at import time
# ---------------------------------------------------------------------------

_provider = None  # EmbeddingProvider instance (GeminiProvider or E5SmallProvider)

# Gemini SDK state — configured lazily on first /chat request, reused thereafter
_genai_api_key: str | None    = None   # last key passed to genai.configure()
_genai_model_name: str | None = None   # last model name used
_genai_model_instance         = None   # cached genai.GenerativeModel instance

# Install state — keyed by install_id
_install_state: dict[str, dict] = {}


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
    """Initialise LanceDB and the embedding provider once (not at import time)."""
    global _provider
    if _provider is not None:
        return

    rag_dir = _resolve_rag_dir()
    log.info("Startup: initialising LanceDB at %s", rag_dir)
    from frapperag.sidecar.store import init_store
    init_store(rag_dir)
    log.info("Startup: LanceDB connection ready")

    provider_name = os.environ.get("EMBEDDING_PROVIDER", "gemini")
    from frapperag.sidecar.providers import build_provider
    _provider = build_provider(provider_name)
    _provider.warmup()

    from frapperag.sidecar.store import configure_provider
    configure_provider(_provider.dim, _provider.table_prefix)
    log.info("Startup: provider=%s dim=%d prefix=%s", _provider.name, _provider.dim, _provider.table_prefix)

    log.info("Startup: pre-importing google.generativeai (warms module cache)")
    import google.generativeai  # noqa: F401
    log.info("Startup: google.generativeai imported")


# ---------------------------------------------------------------------------
# Lifespan: optional init when the app is started without __main__ (e.g. uvicorn CLI)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — full init if `sync_startup` was not run earlier."""
    global _provider

    if _provider is None:
        sync_startup()

    yield

    log.info("Shutdown: sidecar stopping")
    _provider = None


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


class ChatMessage(BaseModel):
    role: str
    parts: list[str]


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    api_key: str
    model: str = "gemini-2.5-flash"
    tools: list[dict] | None = None  # per-report function declarations


class InstallReq(BaseModel):
    hf_token: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness check. Returns 200 when the sidecar is ready."""
    if _provider is None:
        return {"status": "starting"}
    return {
        "status": "ok",
        "provider": _provider.name,
        "dim": _provider.dim,
        "table_prefix": _provider.table_prefix,
    }


@app.post("/embed")
def embed(req: EmbedRequest):
    """Embed a list of texts using the active embedding provider."""
    if not req.texts:
        raise HTTPException(status_code=422, detail="texts must be a non-empty list")
    if _provider is None:
        raise HTTPException(status_code=503, detail="Provider not initialised yet")

    try:
        vectors = _provider.embed(req.texts, req.mode, req.api_key)
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
    if _provider is None:
        raise HTTPException(status_code=503, detail="Provider not initialised yet")

    from frapperag.sidecar.store import search_all_active_tables
    import time as _time

    try:
        t0 = _time.monotonic()
        vector = _provider.embed([req.text], "query", req.api_key)[0]
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
    if _provider is None:
        raise HTTPException(status_code=503, detail="Provider not initialised yet")

    from frapperag.sidecar.store import table_name_for, record_id_for, upsert_rows

    try:
        vector = _provider.embed([req.text], "passage", req.api_key)[0]

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


_CHAT_RATE_LIMIT_SLEEP = 15.0  # seconds — kept well below the 180s httpx timeout in sidecar_client


@app.post("/chat")
def chat(req: ChatRequest):
    """Call Gemini with the assembled conversation history.

    Reuses a cached GenerativeModel across requests — genai.configure() and
    GenerativeModel() run only when the api_key or model name changes.
    start_chat() is per-request because history differs each time.

    Rate-limit handling: ResourceExhausted → 60s sleep → one retry.
    Returns {"text": str, "tokens_used": int}.
    """
    global _genai_api_key, _genai_model_name, _genai_model_instance

    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must be non-empty")

    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    # Re-configure only when the api_key changes (avoid redundant SDK setup)
    if req.api_key != _genai_api_key:
        genai.configure(api_key=req.api_key)
        _genai_api_key = req.api_key
        _genai_model_instance = None  # force model recreate on key change

    if req.tools:
        # Per-request model instance when tools are present — bypass cache
        tool_objects = [
            genai.types.Tool(function_declarations=[
                genai.types.FunctionDeclaration(**fd)
                for fd in tool_block.get("function_declarations", [])
            ])
            for tool_block in req.tools
        ]
        model_instance = genai.GenerativeModel(req.model, tools=tool_objects)
    else:
        if _genai_model_instance is None or req.model != _genai_model_name:
            _genai_model_instance = genai.GenerativeModel(req.model)
            _genai_model_name = req.model
        model_instance = _genai_model_instance

    history      = [{"role": m.role, "parts": m.parts} for m in req.messages[:-1]]
    last_message = req.messages[-1].parts[0]
    chat_session = model_instance.start_chat(history=history)

    response = None
    for attempt in range(2):
        try:
            t0 = time.monotonic()
            response = chat_session.send_message(last_message)
            log.info("[TIMING][/chat] send_message %.3fs", time.monotonic() - t0)
            break
        except ResourceExhausted:
            if attempt == 0:
                log.warning("/chat: Gemini rate-limited — sleeping %.0fs before retry", _CHAT_RATE_LIMIT_SLEEP)
                time.sleep(_CHAT_RATE_LIMIT_SLEEP)
                continue
            raise HTTPException(status_code=429, detail="Gemini API rate limit exceeded after retry")
        except Exception as exc:
            log.exception("/chat: Gemini call failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"Gemini API error: {exc}")

    tokens_used = 0
    if hasattr(response, "usage_metadata") and response.usage_metadata:
        tokens_used = getattr(response.usage_metadata, "total_token_count", 0)

    # Detect tool_call response — check before accessing response.text
    parts = (
        response.candidates[0].content.parts
        if response.candidates and response.candidates[0].content.parts
        else []
    )

    for part in parts:
        if hasattr(part, "function_call") and part.function_call and part.function_call.name:
            fc = part.function_call
            return {
                "tool_call": {"name": fc.name, "args": dict(fc.args)},
                "tokens_used": tokens_used,
            }

    # Empty-parts response (e.g. finish_reason=STOP with no content) — return
    # empty text rather than letting response.text raise ValueError.
    if not parts:
        finish_reason = (
            response.candidates[0].finish_reason
            if response.candidates else "unknown"
        )
        log.warning("/chat: Gemini returned no content parts (finish_reason=%s)", finish_reason)
        return {"text": "", "tokens_used": tokens_used}

    return {"text": response.text, "tokens_used": tokens_used}


@app.delete("/record/{table}/{record_id:path}")
def delete_record(table: str, record_id: str):
    """Remove one vector entry from a LanceDB table by composite ID.

    Idempotent — no error if the record does not exist.
    """
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
    from frapperag.sidecar.store import list_populated_tables
    try:
        active_prefix = prefix if prefix else (_provider.table_prefix if _provider else "")
        tables = list_populated_tables(active_prefix)
        return {"populated": len(tables) > 0, "tables": tables, "prefix": active_prefix}
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
