"""Unit tests for the embeddings client (Epic 3 S1).

Hermetic: the HTTP client is faked, so no network. Exercises configuration
gating, internal batching, and the swallow-and-default contract (any failure →
None, never an exception).
"""
import pytest

from app import embeddings


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """Async-context fake for httpx.AsyncClient. `handler(payload)->_FakeResp`."""
    def __init__(self, handler, **kwargs):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return self._handler(json)


def _install(monkeypatch, handler):
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", "http://embed.test")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "test-embed")
    monkeypatch.setattr(embeddings, "httpx",
                        type("m", (), {"AsyncClient": lambda **kw: _FakeClient(handler, **kw)}))


def _ok_handler(vec_for):
    def handler(payload):
        inputs = payload["input"]
        return _FakeResp({"data": [{"index": i, "embedding": vec_for(t)}
                                   for i, t in enumerate(inputs)]})
    return handler


async def test_not_configured_returns_none(monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_BASE_URL", raising=False)
    assert embeddings.configured() is False
    assert await embeddings.embed(["hello"]) is None
    assert await embeddings.embed_query("hi") is None


async def test_embed_returns_one_vector_per_input(monkeypatch):
    _install(monkeypatch, _ok_handler(lambda t: [float(len(t)), 1.0, 2.0]))
    out = await embeddings.embed(["a", "bb", "ccc"])
    assert out == [[1.0, 1.0, 2.0], [2.0, 1.0, 2.0], [3.0, 1.0, 2.0]]


async def test_embed_query_unwraps_single(monkeypatch):
    _install(monkeypatch, _ok_handler(lambda t: [9.0, 9.0]))
    assert await embeddings.embed_query("q") == [9.0, 9.0]


async def test_embed_batches_are_reassembled_in_order(monkeypatch):
    monkeypatch.setattr(embeddings, "BATCH_SIZE", 2)
    _install(monkeypatch, _ok_handler(lambda t: [float(ord(t[0]))]))
    out = await embeddings.embed(["a", "b", "c", "d", "e"])
    assert out == [[97.0], [98.0], [99.0], [100.0], [101.0]]


async def test_malformed_response_swallows_to_none(monkeypatch):
    def bad(payload):
        return _FakeResp({"data": [{"index": 0, "embedding": [1.0]}]})  # too few
    _install(monkeypatch, bad)
    assert await embeddings.embed(["a", "b"]) is None


async def test_http_error_swallows_to_none(monkeypatch):
    _install(monkeypatch, lambda payload: _FakeResp({}, status=500))
    assert await embeddings.embed(["a"]) is None


def test_dim_override(monkeypatch):
    monkeypatch.setenv("EMBEDDINGS_DIM", "384")
    assert embeddings.dim() == 384
    monkeypatch.setenv("EMBEDDINGS_DIM", "notanint")
    assert embeddings.dim() is None
    monkeypatch.delenv("EMBEDDINGS_DIM", raising=False)
    assert embeddings.dim() is None
