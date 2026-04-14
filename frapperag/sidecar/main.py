"""RAG Sidecar — FastAPI application.

Started by `bench start` via the Procfile entry added by after_install().
Binds to localhost only (Constitution Principle IV).

Heavy work (LanceDB + sentence-transformers) runs in `sync_startup()`, invoked
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

_model = None                 # sentence_transformers.SentenceTransformer instance

# Gemini SDK state — configured lazily on first /chat request, reused thereafter
_genai_api_key: str | None    = None   # last key passed to genai.configure()
_genai_model_name: str | None = None   # last model name used
_genai_model_instance         = None   # cached genai.GenerativeModel instance


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
    """Initialise LanceDB and the embedding model once (not at import time)."""
    global _model
    if _model is not None:
        return

    rag_dir = _resolve_rag_dir()
    log.info("Startup: initialising LanceDB at %s", rag_dir)
    from frapperag.sidecar.store import init_store

    init_store(rag_dir)
    log.info("Startup: LanceDB connection ready")

    log.info("Startup: loading multilingual-e5-base (first run may download ~280 MB)")
    from sentence_transformers import SentenceTransformer

    _model = SentenceTransformer("intfloat/multilingual-e5-base")
    log.info("Startup: model loaded — sidecar ready")

    log.info("Startup: pre-importing google.generativeai (warms module cache)")
    import google.generativeai  # noqa: F401
    log.info("Startup: google.generativeai imported")


# ---------------------------------------------------------------------------
# Lifespan: optional init when the app is started without __main__ (e.g. uvicorn CLI)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan — full init if `sync_startup` was not run earlier."""
    global _model

    if _model is None:
        sync_startup()

    yield

    log.info("Shutdown: sidecar stopping")
    _model = None


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


class SearchRequest(BaseModel):
    text: str
    top_k: int = 5
    max_distance: float = 1.0


class UpsertRequest(BaseModel):
    doctype: str
    name: str
    text: str


class ChatMessage(BaseModel):
    role: str
    parts: list[str]


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    api_key: str
    model: str = "gemini-2.5-flash"
    tools: list[dict] | None = None  # per-report function declarations


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness check. Returns 200 when the sidecar is ready."""
    return {"status": "ok", "model": "multilingual-e5-base"}


@app.post("/embed")
def embed(req: EmbedRequest):
    """Embed a list of texts using multilingual-e5-base.

    Returns 768-dim float vectors in the same order as the input texts.
    """
    if not req.texts:
        raise HTTPException(status_code=422, detail="texts must be a non-empty list")
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not initialised yet")

    try:
        # multilingual-e5-base expects "query: " or "passage: " prefix for best results.
        # Callers specify mode="query" for retrieval, mode="passage" for indexing (default).
        prefix = "query" if req.mode == "query" else "passage"
        prefixed = [f"{prefix}: {t}" for t in req.texts]
        vectors = _model.encode(prefixed, normalize_embeddings=True)
        return {"vectors": [v.tolist() for v in vectors]}
    except Exception as exc:
        log.exception("embed failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Model inference failed: {exc}")


@app.post("/search")
def search(req: SearchRequest):
    """Embed a query text and search all v3_* LanceDB tables.

    Uses "query: " prefix (multilingual-e5-base retrieval convention).
    Returns candidates sorted by ascending cosine distance, filtered by max_distance.
    """
    if not req.text:
        raise HTTPException(status_code=422, detail="text must be non-empty")
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not initialised yet")

    from frapperag.sidecar.store import search_all_v3_tables
    import time as _time

    try:
        t0 = _time.monotonic()
        vector = _model.encode([f"query: {req.text}"], normalize_embeddings=True)[0].tolist()
        embed_ms = _time.monotonic() - t0
        log.info("[TIMING][/search] embed %.3fs", embed_ms)

        t0 = _time.monotonic()
        results = search_all_v3_tables(vector, top_k=req.top_k, max_distance=req.max_distance)
        log.info("[TIMING][/search] vector_search %.3fs → %d results", _time.monotonic() - t0, len(results))

        return {"results": results}
    except Exception as exc:
        log.exception("search failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}")


@app.post("/upsert")
def upsert(req: UpsertRequest):
    """Embed one record's text and upsert its vector into the v3_ LanceDB table.

    Creates the table if it does not exist.
    """
    if not req.doctype or not req.name or not req.text:
        raise HTTPException(status_code=422, detail="doctype, name, and text are required")
    if _model is None:
        raise HTTPException(status_code=503, detail="Model not initialised yet")

    from frapperag.sidecar.store import table_name_for, record_id_for, upsert_rows

    try:
        prefixed = f"passage: {req.text}"
        vector = _model.encode([prefixed], normalize_embeddings=True)[0]

        table_name = table_name_for(req.doctype)
        row = {
            "id":            record_id_for(req.doctype, req.name),
            "doctype":       req.doctype,
            "name":          req.name,
            "text":          req.text,
            "vector":        vector.tolist(),
            "last_modified": "",
        }
        upsert_rows(table_name, [row])
        return {"ok": True}
    except Exception as exc:
        log.exception("upsert failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Upsert failed: {exc}")


_CHAT_RATE_LIMIT_SLEEP = 60.0  # seconds — mirrors FR-015 retry pattern


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

    # Import the package module so `_model` / `app` are the same objects uvicorn will use.
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
