"""Text embedding client for the semantic-search layer (Epic 3 S1).

Mirrors the ``extract_llm`` backend contract: a single local OpenAI-compatible
``/v1/embeddings`` endpoint, selected via ``EMBEDDINGS_BASE_URL`` +
``EMBEDDINGS_MODEL`` (llama.cpp / Ollama / LM Studio serving e.g.
``nomic-embed-text`` or ``bge-small``). No torch in the image.

With ``EMBEDDINGS_BASE_URL`` unset the backend is *not configured*: the caller
(``vecindex``, the ``/api/search/semantic`` route) treats that as a 501, exactly
like ``extract_llm`` without a backend. Every call here is swallow-and-default —
``embed()`` returns ``None`` on any failure so an embedding hiccup can never
change a scrape response (the flag-never-filter / resilient-signal invariant).
"""
import logging
import os
from typing import List, Optional

import httpx

logger = logging.getLogger("embeddings")

# Requests are chunked into batches so a large backfill never sends one giant
# payload the server rejects. Overridable for slower/faster backends.
BATCH_SIZE = int(os.environ.get("EMBEDDINGS_BATCH", "64"))
TIMEOUT = float(os.environ.get("EMBEDDINGS_TIMEOUT", "120"))


def base_url() -> str:
    return os.environ.get("EMBEDDINGS_BASE_URL", "").rstrip("/")


def model() -> str:
    return os.environ.get("EMBEDDINGS_MODEL") or "embedding"


def _api_key() -> str:
    """Bearer token for the endpoint. Empty for keyless local servers
    (Ollama/llama.cpp); set for hosted gateways."""
    return (os.environ.get("EMBEDDINGS_API_KEY")
            or os.environ.get("LOCAL_LLM_API_KEY")
            or os.environ.get("AI_GATEWAY_API_KEY") or "")


def configured() -> bool:
    """True when an embedding backend is available (EMBEDDINGS_BASE_URL set)."""
    return bool(base_url())


def dim() -> Optional[int]:
    """Optional dimension override (EMBEDDINGS_DIM). Otherwise the index learns
    the dimension from the first vector returned by the backend."""
    d = os.environ.get("EMBEDDINGS_DIM")
    try:
        return int(d) if d else None
    except ValueError:
        return None


async def _embed_batch(http: httpx.AsyncClient, texts: List[str]) -> Optional[List[List[float]]]:
    payload = {"model": model(), "input": list(texts)}
    resp = await http.post(f"{base_url()}/v1/embeddings", json=payload)
    resp.raise_for_status()
    body = resp.json()
    data = body.get("data") or []
    # OpenAI returns objects with an "index"; sort defensively before stripping.
    data = sorted(data, key=lambda d: d.get("index", 0))
    vecs = [d.get("embedding") for d in data]
    if len(vecs) != len(texts) or any(not v for v in vecs):
        raise ValueError(
            f"embedding backend returned {len(vecs)} vectors for {len(texts)} inputs")
    return vecs


async def embed(texts: List[str]) -> Optional[List[List[float]]]:
    """Embed a list of texts. Returns one vector per input, or ``None`` on any
    failure (backend unset, network error, malformed response). Batched
    internally; a single failed batch fails the whole call (``None``)."""
    if not texts or not configured():
        return None
    headers = {}
    key = _api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    out: List[List[float]] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=headers) as http:
            for i in range(0, len(texts), BATCH_SIZE):
                batch = texts[i:i + BATCH_SIZE]
                vecs = await _embed_batch(http, batch)
                if vecs is None:
                    return None
                out.extend(vecs)
        return out
    except Exception as e:
        logger.warning("embed failed (%d texts): %s", len(texts), e)
        return None


async def embed_query(text: str) -> Optional[List[float]]:
    """Embed a single query string. Returns the vector or ``None``."""
    vecs = await embed([text])
    return vecs[0] if vecs else None
