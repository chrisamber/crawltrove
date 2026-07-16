from app.corpus import layout, provenance


def test_record_content_hash_is_idempotent(tmp_corpus):
    paths = layout.ensure_layout(tmp_corpus)
    meta = paths["metadata"]

    first = provenance.record_content_hash(meta, "sha256:aaa", {"url": "u1"})
    second = provenance.record_content_hash(meta, "sha256:aaa", {"url": "u1"})

    assert first is True
    assert second is False

    loaded = provenance.load_content_hashes(meta)
    assert set(loaded) == {"sha256:aaa"}
    assert loaded["sha256:aaa"]["url"] == "u1"


def test_record_source_appends(tmp_corpus):
    paths = layout.ensure_layout(tmp_corpus)
    meta = paths["metadata"]
    provenance.record_source(meta, {"id": "s1", "url": "u1"})
    provenance.record_source(meta, {"id": "s2", "url": "u2"})
    lines = (meta / "sources.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
