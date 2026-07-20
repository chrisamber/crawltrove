import asyncio
import hashlib
import importlib
import json
from pathlib import Path

from app.corpus import chunking
from app.corpus import workflow as wf

drain = importlib.import_module("scripts.scrape_swift64_loop")


def _results_for(bid):
    return [
        {"url": f"https://x.test/{bid}/a",
         "markdown": f"# {bid} A\n\nAlpha body text, long enough to be a record.",
         "metadata": {"title": f"{bid} A", "dedup": {"content_hash": f"sha256:{bid}-a"}}},
        {"url": f"https://x.test/{bid}/b",
         "markdown": f"# {bid} B\n\nBeta body text, long enough to be a record.",
         "metadata": {"title": f"{bid} B", "dedup": {"content_hash": f"sha256:{bid}-b"}}},
    ]


def _make_scrape_fn(base):
    async def scrape(batch):
        art = Path(base) / "crawls" / f"{batch['id']}.json"
        art.parent.mkdir(parents=True, exist_ok=True)
        job = {"results": _results_for(batch["id"]), "source": "web", "status": "completed"}
        art.write_text(json.dumps(job), encoding="utf-8")
        return str(art)
    return scrape


def _web_batch(bid):
    # corpus_source 'swift-book' -> bucket cc-by-4.0 -> routes to {rag, dapt}
    return {"id": bid, "source": "web", "corpus_source": "swift-book",
            "version_hint": "x", "urls": [f"https://x.test/{bid}/a"]}


def _content_hashes(base):
    p = Path(base) / "metadata" / "content-hashes.jsonl"
    return {json.loads(l)["content_hash"] for l in p.read_text().splitlines() if l.strip()}


def _expected_hashes(bids):
    """Ledger hashes after chunking: DAPT keeps the whole-page hash, RAG records one
    hash per structure-aware chunk. swift-book routes to {rag, dapt}."""
    exp = set()
    for bid in bids:
        for res in _results_for(bid):
            exp.add(res["metadata"]["dedup"]["content_hash"])  # DAPT page hash
            for c in chunking.chunk_markdown(res["markdown"]):
                exp.add("sha256:" + hashlib.sha256(c["text"].encode("utf-8")).hexdigest())
    return exp


def test_run_drain_builds_ledger_and_rag(tmp_path):
    state = asyncio.run(drain.run_drain(
        [_web_batch("b1")], base=str(tmp_path), include_restricted=True,
        do_sft=False, concurrency=2, per_host=None,
        scrape_fn=_make_scrape_fn(tmp_path), progress=wf.Progress(),
    ))
    assert state["b1"]["status"] == "ok"
    assert _content_hashes(tmp_path) == _expected_hashes(["b1"])
    rag_files = list((tmp_path / "corpus" / "rag").rglob("*.jsonl"))
    assert rag_files, "expected at least one routed RAG file"


def test_run_drain_is_idempotent(tmp_path):
    fn = _make_scrape_fn(tmp_path)
    asyncio.run(drain.run_drain([_web_batch("b1")], base=str(tmp_path),
                include_restricted=True, do_sft=False, concurrency=1,
                per_host=None, scrape_fn=fn, progress=wf.Progress()))
    asyncio.run(drain.run_drain([_web_batch("b1")], base=str(tmp_path),
                include_restricted=True, do_sft=False, concurrency=1,
                per_host=None, scrape_fn=fn, progress=wf.Progress()))
    # second run records no new hashes
    assert _content_hashes(tmp_path) == _expected_hashes(["b1"])


def test_parallel_result_equals_serial_result(tmp_path):
    serial = tmp_path / "serial"
    parallel = tmp_path / "parallel"
    batches = [_web_batch("b1"), _web_batch("b2")]
    asyncio.run(drain.run_drain(batches, base=str(serial), include_restricted=True,
                do_sft=False, concurrency=1, per_host=None,
                scrape_fn=_make_scrape_fn(serial), progress=wf.Progress()))
    asyncio.run(drain.run_drain(batches, base=str(parallel), include_restricted=True,
                do_sft=False, concurrency=4, per_host=None,
                scrape_fn=_make_scrape_fn(parallel), progress=wf.Progress()))
    assert _content_hashes(serial) == _content_hashes(parallel)
    assert _content_hashes(serial) == _expected_hashes(["b1", "b2"])


def test_scrape_failure_isolates_batch(tmp_path):
    async def scrape(batch):
        if batch["id"] == "bad":
            raise RuntimeError("scrape exploded")
        return await _make_scrape_fn(tmp_path)(batch)

    state = asyncio.run(drain.run_drain(
        [_web_batch("good"), {"id": "bad", "source": "web", "corpus_source": "swift-book",
                              "version_hint": "x", "urls": ["https://x.test/bad/a"]}],
        base=str(tmp_path), include_restricted=True, do_sft=False, concurrency=2,
        per_host=None, scrape_fn=scrape, progress=wf.Progress(),
    ))
    assert state["good"]["status"] == "ok"
    assert state["bad"]["status"] == "failed"
    assert "scrape exploded" in state["bad"]["error"]


def test_sft_runs_once_globally(tmp_path, monkeypatch):
    calls = {"n": 0}

    async def fake_run(rag_dir, out_dir, **kw):
        calls["n"] += 1
        return {"records": 0, "pairs": 0}

    monkeypatch.setattr(drain.generate_sft, "run", fake_run)
    asyncio.run(drain.run_drain(
        [_web_batch("b1"), _web_batch("b2")], base=str(tmp_path),
        include_restricted=True, do_sft=True, concurrency=2, per_host=None,
        scrape_fn=_make_scrape_fn(tmp_path), progress=wf.Progress(),
    ))
    assert calls["n"] == 1  # one global pass, NOT per-batch


def test_select_todo_modes():
    allb = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    pend = [{"id": "b"}, {"id": "c"}]
    assert [x["id"] for x in drain.select_todo(allb, pend, batch="a", batches="", run_all=False)] == ["a"]
    assert [x["id"] for x in drain.select_todo(allb, pend, batch="", batches="b,c", run_all=False)] == ["b", "c"]
    assert [x["id"] for x in drain.select_todo(allb, pend, batch="", batches="", run_all=True)] == ["b", "c"]
    assert [x["id"] for x in drain.select_todo(allb, pend, batch="", batches="", run_all=False)] == ["b"]


def test_scraper_argv_apple_has_no_out_flag():
    argv = drain.scraper_argv({"source": "appledocs-docc", "root": "https://developer.apple.com/documentation/swiftui"})
    assert "--out" not in argv  # dry-run output stays clean; --out is added at exec time


def test_artifact_for_batch_apple_passes_out(tmp_path, monkeypatch):
    import app.storage as storage_mod
    monkeypatch.setattr(storage_mod, "CRAWLS_DIR", str(tmp_path))
    seen = {}

    async def fake_run_async(argv):
        seen["argv"] = argv
        # emulate the scraper writing the artifact to the --out path
        out = argv[argv.index("--out") + 1]
        Path(out).write_text("{}", encoding="utf-8")

    monkeypatch.setattr(drain, "_run_async", fake_run_async)
    batch = {"id": "apple-swiftui", "source": "appledocs-docc",
             "root": "https://developer.apple.com/documentation/swiftui"}
    path = asyncio.run(drain._artifact_for_batch_async(batch))
    assert path.startswith(str(tmp_path))
    assert path.endswith(".json") and "apple-swiftui" in path
    assert seen["argv"][seen["argv"].index("--out") + 1] == path


def test_artifact_for_batch_web_uses_scrape_web(tmp_path, monkeypatch):
    async def fake_web(batch):
        return "WEBPATH.json"
    monkeypatch.setattr(drain, "_scrape_web", fake_web)
    batch = {"id": "w", "source": "web", "urls": ["https://swift.org/blog/"]}
    path = asyncio.run(drain._artifact_for_batch_async(batch))
    assert path == "WEBPATH.json"
