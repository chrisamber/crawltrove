"""Tests for structure-aware Markdown chunking."""
from app.corpus import chunking


def test_empty_returns_no_chunks():
    assert chunking.chunk_markdown("") == []
    assert chunking.chunk_markdown("   \n\n  ") == []


def test_single_small_page_is_one_chunk():
    chunks = chunking.chunk_markdown("# View\n\nDiscussion of View.")
    assert len(chunks) == 1
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["heading_path"] == ["View"]
    assert "Discussion of View." in chunks[0]["text"]


def test_heading_path_is_nested_breadcrumb():
    md = "# Framework\n\n## Overview\n\n" + " ".join(["word"] * 300) + \
         "\n\n## Details\n\n" + " ".join(["thing"] * 300)
    chunks = chunking.chunk_markdown(md, target_tokens=200, max_tokens=400)
    paths = [c["heading_path"] for c in chunks]
    assert ["Framework", "Overview"] in paths
    assert ["Framework", "Details"] in paths


def test_code_fence_is_never_split():
    code = "```swift\n" + "\n".join(f"let x{i} = {i}" for i in range(200)) + "\n```"
    md = f"# Sample\n\nIntro.\n\n{code}\n\nAfter."
    chunks = chunking.chunk_markdown(md, target_tokens=50, max_tokens=100)
    # The whole fenced block lands in exactly one chunk, fences balanced.
    holders = [c for c in chunks if "```swift" in c["text"]]
    assert len(holders) == 1
    assert holders[0]["text"].count("```") == 2


def test_large_page_splits_into_multiple_windows():
    md = "# Big\n\n" + "\n\n".join(" ".join(["w"] * 100) for _ in range(20))
    chunks = chunking.chunk_markdown(md, target_tokens=200, max_tokens=300)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c["text"].split()) <= 300 + 40 + 5  # window + overlap slack


def test_chunk_indices_are_sequential():
    md = "# A\n\n" + "\n\n".join(" ".join(["x"] * 120) for _ in range(6))
    chunks = chunking.chunk_markdown(md, target_tokens=150, max_tokens=200)
    assert [c["chunk_index"] for c in chunks] == list(range(len(chunks)))


def test_deterministic():
    md = "# T\n\n" + " ".join(["word"] * 500)
    a = chunking.chunk_markdown(md)
    b = chunking.chunk_markdown(md)
    assert [c["text"] for c in a] == [c["text"] for c in b]
