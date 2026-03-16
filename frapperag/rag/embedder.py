"""
Gemini text-embedding-004 caller.

All heavy imports (google.generativeai, google.api_core) are inside embed_texts()
to prevent module-level global state in multi-site bench workers (Principle II).
"""

EMBEDDING_MODEL  = "models/gemini-embedding-001"
EMBEDDING_DIMS   = 768   # request 768-dim output for LanceDB schema compatibility
BATCH_SIZE       = 20     # documents per Gemini API call
MAX_RETRIES      = 3
RETRY_BASE_DELAY = 2.0    # seconds; doubled on each retry for generic errors
RATE_LIMIT_SLEEP = 60.0   # seconds to wait on ResourceExhausted before retry


class EmbeddingError(Exception):
    """Raised when embedding generation fails after all retries."""


def embed_texts(texts: list, api_key: str) -> list:
    """Embed a list of texts using Gemini text-embedding-004.

    Returns a list of 768-dim float vectors in the same order as input.

    Rate-limit handling:
    - ResourceExhausted → flat 60-second sleep before retry (no exponential back-off)
    - All other exceptions → exponential back-off: 2s, 4s, 8s

    Raises EmbeddingError after MAX_RETRIES exhausted on any batch.
    """
    import time
    import google.generativeai as genai
    from google.api_core.exceptions import ResourceExhausted

    genai.configure(api_key=api_key)
    results = []

    for batch_start in range(0, len(texts), BATCH_SIZE):
        batch = texts[batch_start : batch_start + BATCH_SIZE]
        delay = RETRY_BASE_DELAY
        last_exc = None

        for attempt in range(MAX_RETRIES):
            try:
                response = genai.embed_content(
                    model=EMBEDDING_MODEL,
                    content=batch,
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=EMBEDDING_DIMS,
                )
                results.extend(response["embedding"])
                last_exc = None
                break
            except ResourceExhausted as exc:
                # Rate limit hit — wait flat 60 seconds before retrying this batch.
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RATE_LIMIT_SLEEP)
            except Exception as exc:
                last_exc = exc
                if attempt < MAX_RETRIES - 1:
                    time.sleep(delay)
                    delay *= 2

        if last_exc:
            raise EmbeddingError(
                f"Embedding failed after {MAX_RETRIES} attempts: {last_exc}"
            ) from last_exc

    return results
