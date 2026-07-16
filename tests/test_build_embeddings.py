import json

from scripts import build_embeddings


def test_corpus_index_metadata_preserves_grouping_and_facets(tmp_path, monkeypatch):
    rag = tmp_path / "corpus" / "rag" / "swift-language"
    rag.mkdir(parents=True)
    record = {
        "id": "chunk-id", "url": "https://example.test/doc", "text": "body",
        "title": "Actors", "namespace": "swift-language", "framework": "",
        "license_bucket": "cc-by", "quality_tier": "high",
        "parent_hash": "parent-id", "chunk_index": 3,
        "heading_path": ["Concurrency", "Actors"],
    }
    (rag / "swift.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(build_embeddings.storage, "DATA_DIR", str(tmp_path))

    docs = list(build_embeddings._corpus_docs())

    assert len(docs) == 1
    assert docs[0]["meta"] == {
        "title": "Actors", "url": "https://example.test/doc",
        "namespace": "swift-language", "framework": "",
        "license_bucket": "cc-by", "quality_tier": "high",
        "parent_hash": "parent-id", "chunk_index": 3,
        "heading_path": ["Concurrency", "Actors"],
        "file": "corpus/rag/swift-language/swift.jsonl",
    }
