from app import retrieval


def _hit(ref, score=1.0, *, chunk=0):
    return {"kind": "scrape", "ref": ref, "url": f"https://{ref}",
            "chunkIndex": chunk, "snippet": ref, "score": score, "meta": {}}


def test_rrf_overlap_deduplicates_and_records_both_signals():
    hits = retrieval.reciprocal_rank_fusion(
        [_hit("shared"), _hit("semantic")],
        [_hit("shared"), _hit("keyword")], k=10)
    assert [hit["ref"] for hit in hits] == ["shared", "keyword", "semantic"]
    assert hits[0]["semanticRank"] == 1
    assert hits[0]["keywordRank"] == 1
    assert hits[0]["semanticScore"] == 1.0
    assert hits[0]["keywordScore"] == 1.0


def test_rrf_ties_use_stable_chunk_identity():
    hits = retrieval.reciprocal_rank_fusion([_hit("z")], [_hit("a")], k=10)
    assert [hit["ref"] for hit in hits] == ["a", "z"]


async def test_hybrid_embedding_failure_degrades_to_keyword(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: True)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)

    async def failed(_query):
        return None

    monkeypatch.setattr(retrieval.embeddings, "embed_query", failed)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [_hit("exact")])
    hits = await retrieval.search("exact", mode="hybrid")
    assert [hit["ref"] for hit in hits] == ["exact"]
    assert hits[0]["keywordRank"] == 1


async def test_postgres_empty_falls_back_to_file_keyword(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def empty(*args, **kwargs):
        return []

    monkeypatch.setattr(retrieval.repo, "search_pages", empty)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [_hit("file")])
    hits = await retrieval.search("term", mode="keyword")
    assert [hit["ref"] for hit in hits] == ["file"]


async def test_postgres_exception_falls_back_to_file_keyword(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def broken(*args, **kwargs):
        raise RuntimeError("db down")

    recorded: list[str] = []
    monkeypatch.setattr(
        retrieval.metrics, "record_retrieval_degradation",
        lambda signal: recorded.append(str(signal)),
    )
    monkeypatch.setattr(retrieval.repo, "search_pages", broken)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [_hit("file")])
    hits = await retrieval.search("term", mode="keyword")
    assert [hit["ref"] for hit in hits] == ["file"]
    # Depth expansion may re-enter keyword; every DB exception must be counted.
    assert recorded and set(recorded) == {"keyword_db"}


async def test_semantic_mode_unavailable(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    try:
        await retrieval.search("term", mode="semantic")
    except retrieval.RetrievalUnavailable:
        pass
    else:
        raise AssertionError("semantic mode should report unavailable")


async def test_keyword_mode_does_not_require_embeddings(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [_hit("keyword")])
    hits = await retrieval.search("term", mode="keyword")
    assert [hit["ref"] for hit in hits] == ["keyword"]


async def test_postgres_artifact_ref_bridges_to_semantic_identity(monkeypatch):
    semantic = [_hit("artifact", score=.9, chunk=2)]
    semantic[0]["url"] = "https://same"
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: True)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def embed(_query):
        return [1.0]

    async def rows(*args, **kwargs):
        return [{"id": 9, "url": "https://same", "rank": .8,
                 "raw_json_path": "data/scrapes/artifact.json",
                 "snippet": "db", "metadata": {}}]

    monkeypatch.setattr(retrieval.embeddings, "embed_query", embed)
    monkeypatch.setattr(retrieval.vecindex, "search",
                        lambda vector, kind=None, k=10: semantic)
    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", lambda *a, **kw: [])
    hits = await retrieval.search("same", mode="hybrid")
    assert len(hits) == 1
    assert hits[0]["ref"] == "artifact"
    assert hits[0]["chunkIndex"] == 2
    assert hits[0]["semanticRank"] == hits[0]["keywordRank"] == 1


async def test_postgres_and_file_keyword_candidates_are_combined(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def rows(*args, **kwargs):
        return [{"id": 7, "url": "https://db", "rank": .8,
                 "snippet": "db scrape", "metadata": {}}]

    corpus = {"kind": "corpus", "ref": "corpus-1", "url": "https://corpus",
              "chunkIndex": 0, "snippet": "file corpus", "score": .7, "meta": {}}
    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", lambda *a, **kw: [])
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [corpus])
    hits = await retrieval.search("exact", mode="keyword", k=10)
    assert [(hit["kind"], hit["ref"]) for hit in hits] == [
        ("scrape", "db:7"), ("corpus", "corpus-1")]


async def test_postgres_cannot_crowd_file_hits_out_of_keyword_top_k(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def rows(*args, **kwargs):
        return [
            {"id": i, "url": f"https://db/{i}", "rank": 1 / i,
             "snippet": f"db {i}", "metadata": {}}
            for i in range(1, 5)
        ]

    corpus = {"kind": "corpus", "ref": "corpus-1", "url": "https://corpus",
              "chunkIndex": 0, "snippet": "file corpus", "score": .7, "meta": {}}
    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", lambda *a, **kw: [])
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [corpus])
    hits = await retrieval.search("exact", mode="keyword", k=2)
    assert [(hit["kind"], hit["ref"]) for hit in hits] == [
        ("scrape", "db:1"), ("corpus", "corpus-1")]


def test_keyword_source_overlap_does_not_consume_file_turn():
    shared = _hit("shared")
    db_only = _hit("db-only")
    file_only = _hit("file-only")
    hits = retrieval._merge_keyword_sources(
        [shared, db_only], [shared, file_only], depth=2)
    assert [hit["ref"] for hit in hits] == ["shared", "file-only"]


async def test_db_artifact_bridge_only_adopts_scrape_identity(monkeypatch):
    corpus = {"kind": "corpus", "ref": "corpus-1", "url": "https://same",
              "chunkIndex": 0, "snippet": "corpus", "score": .9, "meta": {}}
    scrape = {"kind": "scrape", "ref": "scrape-1", "url": "https://same",
              "chunkIndex": 2, "snippet": "scrape", "score": 0.0, "meta": {}}
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: True)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def embed(_query):
        return [1.0]

    async def rows(*args, **kwargs):
        return [{"id": 8, "url": "https://same", "rank": .8,
                 "raw_json_path": "data/scrapes/scrape-1.json",
                 "snippet": "db", "metadata": {}}]

    def chunks_for_refs(refs, kind=None, k=100):
        assert refs == ["scrape-1"]
        assert kind == "scrape"
        return [scrape]

    monkeypatch.setattr(retrieval.embeddings, "embed_query", embed)
    monkeypatch.setattr(retrieval.vecindex, "search",
                        lambda vector, kind=None, k=10: [corpus])
    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", chunks_for_refs)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [])
    hits = await retrieval.search("same", mode="hybrid", k=10)
    by_ref = {hit["ref"]: hit for hit in hits}
    assert set(by_ref) == {"corpus-1", "scrape-1"}
    assert "keywordRank" not in by_ref["corpus-1"]
    assert by_ref["scrape-1"]["keywordRank"] == 1


async def test_db_artifact_bridge_does_not_boost_older_scrape_of_same_url(monkeypatch):
    old = _hit("old", score=.9)
    new = _hit("new", score=.8)
    old["url"] = new["url"] = "https://same"
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: True)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def embed(_query):
        return [1.0]

    async def rows(*args, **kwargs):
        return [{"id": 9, "url": "https://same", "rank": .8,
                 "raw_json_path": "data/scrapes/new.json",
                 "snippet": "db", "metadata": {}}]

    def chunks_for_refs(refs, kind=None, k=100):
        assert refs == ["new"]
        return [new]

    monkeypatch.setattr(retrieval.embeddings, "embed_query", embed)
    monkeypatch.setattr(retrieval.vecindex, "search",
                        lambda vector, kind=None, k=10: [old, new])
    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", chunks_for_refs)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search",
                        lambda query, kind=None, k=10: [])
    hits = await retrieval.search("same", mode="hybrid", k=10)
    by_ref = {hit["ref"]: hit for hit in hits}
    assert by_ref["new"]["keywordRank"] == 1
    assert "keywordRank" not in by_ref["old"]


async def test_db_only_row_has_explicit_identity(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: False)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def rows(*args, **kwargs):
        return [{"id": 42, "url": "https://db", "rank": .5,
                 "snippet": "db", "metadata": {}}]

    monkeypatch.setattr(retrieval.repo, "search_pages", rows)
    monkeypatch.setattr(retrieval.vecindex, "chunks_for_refs", lambda *a, **kw: [])
    hits = await retrieval.search("db", mode="keyword")
    assert hits[0]["ref"] == "db:42"
    assert hits[0]["kind"] == "scrape"


def test_collapse_keeps_best_chunk_and_surfaces_later_parents():
    hits = [
        _hit("long", score=.9, chunk=0),
        _hit("long", score=.8, chunk=1),
        _hit("long", score=.7, chunk=2),
        _hit("other", score=.6, chunk=0),
    ]
    collapsed = retrieval.collapse_results(hits, 2)
    assert [hit["ref"] for hit in collapsed] == ["long", "other"]
    assert collapsed[0]["chunkIndex"] == 0
    assert collapsed[0]["matchedChunkCount"] == 3
    assert collapsed[0]["parentId"] == "scrape:ref:long"


def test_collapse_uses_corpus_parent_hash_but_namespaces_kind():
    one = _hit("chunk-1", chunk=0)
    two = _hit("chunk-2", chunk=0)
    one.update(kind="corpus", meta={"parent_hash": "parent"})
    two.update(kind="corpus", meta={"parent_hash": "parent"})
    scrape = _hit("scrape", chunk=0)
    scrape["meta"] = {"parent_hash": "parent"}
    collapsed = retrieval.collapse_results([one, two, scrape], 10)
    assert [(hit["kind"], hit["matchedChunkCount"]) for hit in collapsed] == [
        ("corpus", 2), ("scrape", 1)]


def test_facet_counts_use_unique_parents_and_normalize_untiered():
    corpus = _hit("chunk", chunk=0)
    corpus.update(kind="corpus", meta={
        "parent_hash": "p", "namespace": "swift-language",
        "license_bucket": "cc-by", "framework": "",
    })
    scrape = _hit("scrape", chunk=0)
    hits = retrieval.collapse_results([corpus, corpus, scrape], 10)
    facets = retrieval.facet_counts(hits)
    assert facets["kind"] == {"corpus": 1, "scrape": 1}
    assert facets["namespace"] == {"swift-language": 1}
    assert facets["bucket"] == {"cc-by": 1}
    assert facets["tier"] == {"untiered": 1}
    assert facets["framework"] == {}


async def test_filters_forward_to_both_file_signals_and_skip_postgres(monkeypatch):
    calls = []
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: True)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: True)

    async def embed(_query):
        return [1.0]

    async def unexpected_db(*args, **kwargs):
        raise AssertionError("metadata-filtered search must not query Postgres")

    def semantic(vector, **kwargs):
        calls.append(("semantic", kwargs))
        return []

    def keyword(query, **kwargs):
        calls.append(("keyword", kwargs))
        return []

    monkeypatch.setattr(retrieval.embeddings, "embed_query", embed)
    monkeypatch.setattr(retrieval.repo, "search_pages", unexpected_db)
    monkeypatch.setattr(retrieval.vecindex, "search", semantic)
    monkeypatch.setattr(retrieval.vecindex, "keyword_search", keyword)
    await retrieval.search(
        "actors", mode="hybrid", filters={"namespace": "swift-language"})
    assert [name for name, _ in calls] == [
        "semantic", "keyword", "semantic", "keyword", "semantic", "keyword"]
    assert [kwargs["k"] for _, kwargs in calls] == [50, 50, 100, 100, 200, 200]
    assert all(kwargs["filters"] == {"namespace": "swift-language"}
               for _, kwargs in calls)


async def test_search_expands_until_requested_unique_parents_surface(monkeypatch):
    monkeypatch.setattr(retrieval.embeddings, "configured", lambda: False)
    monkeypatch.setattr(retrieval.vecindex, "available", lambda: True)
    monkeypatch.setattr(retrieval.pool, "enabled", lambda: False)
    candidates = [_hit("long", chunk=i) for i in range(60)] + [_hit("other")]
    depths = []

    def keyword(query, kind=None, k=10):
        depths.append(k)
        return candidates[:k]

    monkeypatch.setattr(retrieval.vecindex, "keyword_search", keyword)
    hits = await retrieval.search("term", mode="keyword", k=2)
    assert [hit["ref"] for hit in hits] == ["long", "other"]
    assert depths == [50, 100]
