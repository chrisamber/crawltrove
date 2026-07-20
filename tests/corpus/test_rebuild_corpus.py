"""Tests for the corpus rebuild command."""
import importlib
import json
from pathlib import Path

rebuild_mod = importlib.import_module("scripts.rebuild_corpus")


def _artifact(base: Path, name: str, source: str, results):
    crawls = base / "crawls"
    crawls.mkdir(parents=True, exist_ok=True)
    (crawls / f"{name}.json").write_text(
        json.dumps({"source": source, "status": "completed", "results": results}),
        encoding="utf-8")


def _page(url, title, ch, md):
    return {"url": url, "markdown": md,
            "metadata": {"title": title, "dedup": {"content_hash": ch},
                         "quality": {"failures": [], "passed": True}}}


def test_rebuild_swaps_corpus_and_appends_event(tmp_path):
    _artifact(tmp_path, "a", "swift-evolution", [
        _page("https://swift.org/blog/x", "X", "sha256:x",
              "# X\n\nclean body words here " * 30),
    ])
    summary = rebuild_mod.rebuild(tmp_path, now="2026-07-11T00:00:00Z")

    assert summary["artifacts"] == 1
    assert summary["written"].get("rag", 0) >= 1
    # swift-evolution -> swift-org-permissive -> {rag, dapt}, high tier -> dapt kept
    assert summary["written"].get("dapt", 0) >= 1

    # corpus tree is in place; staging dir consumed.
    assert (tmp_path / "corpus" / "rag").exists()
    assert not (tmp_path / "corpus.new").exists()

    # append-only rebuild ledger recorded exactly one event.
    ledger = (tmp_path / "metadata" / "rebuilds.jsonl").read_text().splitlines()
    assert len(ledger) == 1
    assert json.loads(ledger[0])["event"] == "rebuild"


def test_rebuild_keeps_previous_tree_as_bak(tmp_path):
    # First build a corpus the normal way so an old tree exists.
    (tmp_path / "corpus" / "rag").mkdir(parents=True)
    (tmp_path / "corpus" / "marker.txt").write_text("old", encoding="utf-8")
    _artifact(tmp_path, "a", "swift-evolution",
              [_page("https://swift.org/b", "B", "sha256:b", "# B\n\nbody " * 30)])

    rebuild_mod.rebuild(tmp_path, now="t")
    assert (tmp_path / "corpus.bak" / "marker.txt").read_text() == "old"
    assert (tmp_path / "corpus" / "rag").exists()


def test_rebuild_carries_sft_state_unless_cleared(tmp_path):
    (tmp_path / "corpus").mkdir(parents=True)
    (tmp_path / "corpus" / ".sft_state.json").write_text('["h1"]', encoding="utf-8")
    _artifact(tmp_path, "a", "swift-evolution",
              [_page("https://swift.org/c", "C", "sha256:c", "# C\n\nbody " * 30)])

    rebuild_mod.rebuild(tmp_path, now="t")
    assert (tmp_path / "corpus" / ".sft_state.json").read_text() == '["h1"]'


def test_rebuild_clear_sft_state_drops_it(tmp_path):
    (tmp_path / "corpus").mkdir(parents=True)
    (tmp_path / "corpus" / ".sft_state.json").write_text('["h1"]', encoding="utf-8")
    _artifact(tmp_path, "a", "swift-evolution",
              [_page("https://swift.org/d", "D", "sha256:d", "# D\n\nbody " * 30)])

    rebuild_mod.rebuild(tmp_path, now="t", clear_sft_state=True)
    assert not (tmp_path / "corpus" / ".sft_state.json").exists()


def test_rebuild_no_artifacts_raises(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        rebuild_mod.rebuild(tmp_path, now="t")


def test_rebuild_ledger_history_is_not_rewritten(tmp_path):
    # A rebuild must not append to content-hashes.jsonl (append-only history).
    _artifact(tmp_path, "a", "swift-evolution",
              [_page("https://swift.org/e", "E", "sha256:e", "# E\n\nbody " * 30)])
    rebuild_mod.rebuild(tmp_path, now="t")
    assert not (tmp_path / "metadata" / "content-hashes.jsonl").exists()
