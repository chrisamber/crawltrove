"""save_research + artifact listing for the research kind (tmp dirs only)."""
import json
import os
import time

from app import storage


def _tmp_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "SCRAPES_DIR", str(tmp_path / "scrapes"))
    monkeypatch.setattr(storage, "CRAWLS_DIR", str(tmp_path / "crawls"))
    monkeypatch.setattr(storage, "RESEARCH_DIR", str(tmp_path / "research"))
    monkeypatch.setattr(storage, "RESEARCH_CHECKPOINTS_DIR",
                        str(tmp_path / "research" / "checkpoints"))


def _job():
    return {"query": "what is x?", "status": "completed",
            "report": "# Research\n\nAnswer [1].",
            "sources": [{"index": 1, "url": "https://a.example", "title": "A",
                         "artifact": "data/scrapes/s.json", "quality_score": 0.9,
                         "relevant": True, "notes": "n", "key_facts": []}]}


def test_save_research_writes_pair(monkeypatch, tmp_path):
    _tmp_dirs(monkeypatch, tmp_path)
    stem = storage.save_research(_job())
    saved = json.loads((tmp_path / "research" / f"{stem}.json").read_text())
    assert saved["query"] == "what is x?"
    assert saved["sources"][0]["artifact"] == "data/scrapes/s.json"
    md = (tmp_path / "research" / f"{stem}.md").read_text()
    assert "Answer [1]." in md
    assert "what is x?" in md


def test_list_artifacts_includes_research(monkeypatch, tmp_path):
    _tmp_dirs(monkeypatch, tmp_path)
    storage.save_research(_job())
    research_items = [i for i in storage.list_artifacts()
                      if i["kind"] == "research"]
    assert len(research_items) == 1
    assert research_items[0]["pages"] == 1
    assert research_items[0]["title"] == "what is x?"
    assert research_items[0]["json"].startswith("/data/research/")


def test_checkpoint_roundtrip(monkeypatch, tmp_path):
    _tmp_dirs(monkeypatch, tmp_path)
    payload = {"version": 1, "job": {"job_id": "j1", "status": "reading"},
               "loop": {"round_no": 1}, "updated_at": "2026-07-10T00:00:00+00:00"}
    storage.save_research_checkpoint("j1", payload)
    loaded = storage.load_research_checkpoints()
    assert loaded == [payload]
    # No stray .tmp left behind by the atomic write.
    ckdir = tmp_path / "research" / "checkpoints"
    assert sorted(os.listdir(ckdir)) == ["j1.json"]

    storage.delete_research_checkpoint("j1")
    assert storage.load_research_checkpoints() == []
    storage.delete_research_checkpoint("j1")   # idempotent


def test_prune_covers_research_and_checkpoints(monkeypatch, tmp_path):
    _tmp_dirs(monkeypatch, tmp_path)
    stem = storage.save_research(_job())
    storage.save_research_checkpoint("stale", {"version": 1, "job": {}, "loop": {}})

    # Age everything past the cutoff.
    old = time.time() - 90 * 86400
    for p in [tmp_path / "research" / f"{stem}.json",
              tmp_path / "research" / f"{stem}.md",
              tmp_path / "research" / "checkpoints" / "stale.json"]:
        os.utime(p, (old, old))

    out = storage.prune(max_age_days=30, keep_runs=0)
    assert out["removed"] == 2      # the research pair (1 stem) + the checkpoint
    assert not (tmp_path / "research" / f"{stem}.json").exists()
    assert not (tmp_path / "research" / "checkpoints" / "stale.json").exists()
