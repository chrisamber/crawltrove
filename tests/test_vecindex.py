"""Tests for the sqlite-vec semantic index (Epic 3 S1).

Uses a real sqlite-vec database in a tmp dir (the extension is a hard dep). The
chunker is a pure function. index_document is exercised with a fake embedding
backend so no network is touched.
"""
import sqlite3

import pytest

from app import embeddings, vecindex

@pytest.fixture
def tmp_index(tmp_path, monkeypatch):
    """Point the module-global index at a fresh tmp DB for each test."""
    if not vecindex.available():
        pytest.skip("sqlite-vec extension not loadable")
    monkeypatch.setattr(vecindex, "INDEX_DIR", str(tmp_path / "index"))
    monkeypatch.setattr(vecindex, "DB_PATH", str(tmp_path / "index" / "vectors.db"))
    vecindex._reset_for_tests()
    yield
    vecindex._reset_for_tests()


@pytest.fixture
def keyword_index(monkeypatch):
    """Minimal sqlite connection; keyword behavior does not need sqlite-vec."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,"
        "ref TEXT NOT NULL,url TEXT,chunk_index INTEGER NOT NULL,content_hash TEXT NOT NULL,"
        "snippet TEXT,meta TEXT,text TEXT)")
    conn.execute("CREATE TABLE vec_chunks(rowid INTEGER PRIMARY KEY,embedding BLOB)")
    conn.execute("CREATE TABLE vec_meta(key TEXT PRIMARY KEY,value TEXT)")
    vecindex._conn = conn
    vecindex._available = True
    vecindex._fts_available = None

    class FakeVec:
        @staticmethod
        def serialize_float32(value):
            return repr(value).encode()

    vecindex._sqlite_vec = FakeVec()
    monkeypatch.setattr(vecindex, "_ensure_vec_table", lambda *args: True)
    vecindex._setup_fts(conn)
    conn.commit()
    yield conn
    vecindex._reset_for_tests()


def test_keyword_search_reads_full_text_beyond_snippet(keyword_index):
    text = "x" * 400 + " exact_tail_symbol"
    assert vecindex.upsert("scrape", "long", "https://long", [text], [[1.0]]) == 1
    hits = vecindex.keyword_search("exact_tail_symbol")
    assert [hit["ref"] for hit in hits] == ["long"]


def test_keyword_kind_and_stale_replace(keyword_index):
    vecindex.upsert("scrape", "same", "https://s", ["oldtoken"], [[1.0]])
    vecindex.upsert("scrape", "same", "https://s", ["newtoken"], [[1.0]])
    vecindex.upsert("research", "r", "https://r", ["newtoken"], [[1.0]])
    assert vecindex.keyword_search("oldtoken") == []
    hits = vecindex.keyword_search("newtoken", kind="research")
    assert [hit["ref"] for hit in hits] == ["r"]


def test_keyword_filters_before_limit_and_combines_facets(keyword_index):
    vecindex.upsert("corpus", "wrong", None, ["shared token"], [[1.0]],
                    meta={"namespace": "other", "quality_tier": "high"})
    vecindex.upsert("corpus", "right", None, ["shared token"], [[1.0]],
                    meta={"namespace": "swift-language", "quality_tier": "high"})
    hits = vecindex.keyword_search(
        "shared token", k=1,
        filters={"namespace": "swift-language", "tier": "high"})
    assert [hit["ref"] for hit in hits] == ["right"]


def test_untiered_filter_is_corpus_only(keyword_index):
    vecindex.upsert("scrape", "scrape", None, ["shared token"], [[1.0]], meta={})
    vecindex.upsert("corpus", "corpus", None, ["shared token"], [[1.0]], meta={})
    hits = vecindex.keyword_search(
        "shared token", filters={"tier": "untiered"})
    assert [(hit["kind"], hit["ref"]) for hit in hits] == [("corpus", "corpus")]


def test_filter_sql_pushes_metadata_filters_into_corpus_candidates():
    sql, params = vecindex._filter_sql(
        "c", None, {"bucket": "cc-by", "framework": "swiftui"})
    assert "c.kind=?" in sql
    assert "json_extract(c.meta, '$.license_bucket')=?" in sql
    assert "json_extract(c.meta, '$.framework')=?" in sql
    assert params == ["corpus", "cc-by", "swiftui"]


def test_semantic_filter_matching_is_corpus_scoped_and_handles_untiered():
    assert vecindex._matches_filters(
        "corpus", {"namespace": "swift-language"}, None,
        {"namespace": "swift-language", "tier": "untiered"})
    assert not vecindex._matches_filters(
        "scrape", {}, None, {"tier": "untiered"})
    assert not vecindex._matches_filters(
        "corpus", {"quality_tier": "high"}, None, {"tier": "untiered"})


def test_keyword_punctuation_is_literal_safe(keyword_index):
    vecindex.upsert("scrape", "punct", None, ["Swift Array append error"], [[1.0]])
    assert vecindex.keyword_search('Swift: (Array) "append" -error')


def test_keyword_like_fallback_keeps_index_available(keyword_index, monkeypatch):
    vecindex.upsert("scrape", "fallback", None, ["fallback token"], [[1.0]])
    monkeypatch.setattr(vecindex, "_fts_available", False)
    assert vecindex.available() is True
    assert [hit["ref"] for hit in vecindex.keyword_search("fallback token")] == ["fallback"]


def test_fts_backfill_runs_only_when_table_is_created(keyword_index):
    statements = []
    keyword_index.set_trace_callback(statements.append)
    assert vecindex._setup_fts(keyword_index) is True
    assert not any("rebuild" in statement.lower() for statement in statements)


# --- chunker (pure) --------------------------------------------------------
def test_chunk_empty_returns_empty():
    assert vecindex.chunk_text("") == []
    assert vecindex.chunk_text("   \n  ") == []


def test_chunk_packs_paragraphs():
    text = "para one.\n\npara two.\n\npara three."
    chunks = vecindex.chunk_text(text, max_chars=1000)
    assert chunks == ["para one.\n\npara two.\n\npara three."]


def test_chunk_splits_on_size():
    text = "\n\n".join(["x" * 200 for _ in range(10)])
    chunks = vecindex.chunk_text(text, max_chars=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_chunk_hard_splits_oversized_paragraph():
    chunks = vecindex.chunk_text("y" * 5000, max_chars=1000, overlap=100)
    assert len(chunks) >= 5
    assert all(len(c) <= 1000 for c in chunks)


# --- upsert / search roundtrip --------------------------------------------
def test_upsert_and_search_roundtrip(tmp_index):
    vecindex.upsert("scrape", "stem-a", "http://a", ["hello world"],
                    [[1.0, 0.0, 0.0]], meta={"title": "A"})
    vecindex.upsert("scrape", "stem-b", "http://b", ["different"],
                    [[0.0, 1.0, 0.0]], meta={"title": "B"})
    hits = vecindex.search([1.0, 0.0, 0.0], k=2)
    assert hits[0]["ref"] == "stem-a"
    assert hits[0]["url"] == "http://a"
    assert hits[0]["meta"]["title"] == "A"
    assert hits[0]["score"] >= hits[1]["score"]


def test_search_filters_by_kind(tmp_index):
    vecindex.upsert("scrape", "s1", "http://s", ["t"], [[1.0, 0.0]])
    vecindex.upsert("research", "r1", None, ["t"], [[1.0, 0.0]])
    hits = vecindex.search([1.0, 0.0], kind="research", k=5)
    assert hits and all(h["kind"] == "research" for h in hits)


def test_upsert_replaces_prior_chunks(tmp_index):
    vecindex.upsert("scrape", "same", "http://x", ["a", "b"],
                    [[1.0, 0.0], [0.9, 0.1]])
    assert vecindex.stats()["total"] == 2
    vecindex.upsert("scrape", "same", "http://x", ["only one"], [[1.0, 0.0]])
    assert vecindex.stats()["total"] == 1


def test_dimension_mismatch_is_refused(tmp_index):
    assert vecindex.upsert("scrape", "a", None, ["t"], [[1.0, 0.0, 0.0]]) == 1
    # A 2-dim vector into a 3-dim index is skipped, not crashed.
    assert vecindex.upsert("scrape", "b", None, ["t"], [[1.0, 0.0]]) == 0
    assert vecindex.stats()["dim"] == 3


def test_stats_and_ref_indexed(tmp_index):
    assert vecindex.ref_indexed("scrape", "x") is False
    vecindex.upsert("scrape", "x", None, ["t"], [[1.0, 0.0]], model="m")
    assert vecindex.ref_indexed("scrape", "x") is True
    st = vecindex.stats()
    assert st["available"] is True
    assert st["byKind"]["scrape"] == 1
    assert st["model"] == "m"


def test_content_hash_indexed(tmp_index):
    import hashlib
    text = "some indexed content"
    ch = hashlib.sha256(text.encode()).hexdigest()
    assert vecindex.content_hash_indexed(ch) is False
    vecindex.upsert("scrape", "x", None, [text], [[1.0, 0.0]])
    assert vecindex.content_hash_indexed(ch) is True


def test_identity_inventory_exposes_url_parent_and_ref_aliases(keyword_index):
    vecindex.upsert(
        "corpus", "chunk", "HTTPS://Example.Test/doc/", ["text"], [[1.0]],
        meta={"parent_hash": "parent", "namespace": "swift-language"})
    assert vecindex.identity_inventory() == {
        "corpus:ref:chunk", "corpus:hash:parent",
        "corpus:url:https://example.test/doc",
    }
    assert vecindex.identity_inventory({
        "kind": "corpus", "namespace": "swift-language"})
    assert vecindex.identity_inventory({"namespace": "apple-framework"}) == set()


# --- index_document (chunk → embed → upsert) ------------------------------
async def test_index_document_no_backend_is_noop(tmp_index, monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: False)
    n = await vecindex.index_document("scrape", "s", "http://s", "hello world body")
    assert n == 0
    assert vecindex.stats()["total"] == 0


async def test_index_document_indexes_chunks(tmp_index, monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)

    async def fake_embed(texts):
        return [[float(len(t)), 1.0] for t in texts]

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    n = await vecindex.index_document("scrape", "s1", "http://s",
                                      "para one.\n\npara two.")
    assert n >= 1
    assert vecindex.ref_indexed("scrape", "s1") is True


async def test_index_document_embed_failure_is_noop(tmp_index, monkeypatch):
    monkeypatch.setattr(embeddings, "configured", lambda: True)

    async def fake_embed(texts):
        return None  # backend down

    monkeypatch.setattr(embeddings, "embed", fake_embed)
    assert await vecindex.index_document("scrape", "s", "http://s", "body text") == 0
    assert vecindex.stats()["total"] == 0
