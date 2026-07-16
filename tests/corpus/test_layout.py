from app.corpus import layout


def test_ensure_layout_creates_dirs_and_returns_paths(tmp_corpus):
    paths = layout.ensure_layout(tmp_corpus)

    assert (tmp_corpus / "corpus" / "rag").is_dir()
    assert (tmp_corpus / "corpus" / "sft").is_dir()
    assert (tmp_corpus / "corpus" / "dapt").is_dir()
    assert (tmp_corpus / "metadata").is_dir()

    assert paths["rag"] == tmp_corpus / "corpus" / "rag"
    assert paths["sources"] == tmp_corpus / "metadata" / "sources.jsonl"
    assert paths["content_hashes"] == tmp_corpus / "metadata" / "content-hashes.jsonl"
    # files are NOT pre-created, only their parent dir
    assert not paths["sources"].exists()


def test_layout_constants():
    assert (layout.RAG, layout.SFT, layout.DAPT) == ("rag", "sft", "dapt")
