"""Pluggable embedding provider abstraction for the RAG sidecar.

This module is imported ONLY inside the sidecar process.
Workers MUST NOT import this module — doing so would violate Constitution Principle IV.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str
    dim: int
    table_prefix: str

    def embed(self, texts: list[str], mode: str, api_key: str | None) -> list[list[float]]: ...
    def warmup(self) -> None: ...


class E5SmallProvider:
    name = "e5-small"
    dim = 384
    table_prefix = "v6_e5small_"

    def __init__(self):
        self._model = None

    def warmup(self) -> None:
        import logging
        from sentence_transformers import SentenceTransformer
        log = logging.getLogger("rag_sidecar")
        log.info("Startup: loading multilingual-e5-small (first run may download ~470 MB)")
        self._model = SentenceTransformer("intfloat/multilingual-e5-small")
        log.info("Startup: e5-small model loaded")

    def embed(self, texts: list[str], mode: str, api_key: str | None) -> list[list[float]]:
        from fastapi import HTTPException
        if self._model is None:
            raise HTTPException(status_code=503, detail="e5-small model not initialised")
        prefix = "query" if mode == "query" else "passage"
        prefixed = [f"{prefix}: {t}" for t in texts]
        vectors = self._model.encode(prefixed, normalize_embeddings=True)
        return [v.tolist() for v in vectors]


class GeminiProvider:
    name = "gemini"
    dim = 768
    table_prefix = "v5_gemini_"

    def warmup(self) -> None:
        import logging
        log = logging.getLogger("rag_sidecar")
        log.info("Startup: GeminiProvider ready (cloud, lazy — no model to load)")

    def embed(self, texts: list[str], mode: str, api_key: str | None) -> list[list[float]]:
        import httpx
        from fastapi import HTTPException
        if not api_key:
            raise HTTPException(status_code=503, detail="GOOGLE_API_KEY required for gemini provider")
        task_type = "RETRIEVAL_QUERY" if mode == "query" else "RETRIEVAL_DOCUMENT"
        vectors = []
        for text in texts:
            resp = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent",
                headers={"x-goog-api-key": api_key},
                json={
                    "model": "models/gemini-embedding-001",
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": 768,
                },
                timeout=30.0,
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=resp.status_code, detail=f"Gemini embed error: {resp.text[:300]}")
            vectors.append(resp.json()["embedding"]["values"])
        return vectors


def build_provider(name: str) -> EmbeddingProvider:
    if name == "e5-small":
        return E5SmallProvider()
    return GeminiProvider()
