"""Self-paced drain loop: harvest Swift 6.4 / iOS 27 content per the manifest
and route it into RAG + synthetic-SFT datasets.

Per batch:  scrape  ->  normalize to a crawl artifact  ->  build_corpus
            ->  generate_sft  ->  mark done in the state file.

Resumable: re-running skips batches already in data/corpus/.loop_state.json.
Standalone usage:
    .venv/bin/python scripts/scrape_swift64_loop.py --list
    .venv/bin/python scripts/scrape_swift64_loop.py --dry-run
    .venv/bin/python scripts/scrape_swift64_loop.py --batch apple-swiftui
    .venv/bin/python scripts/scrape_swift64_loop.py --all
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from app.corpus import crawl_shape, manifest as mf  # noqa: E402
from app.corpus import workflow  # noqa: E402
from scripts import build_corpus, generate_sft  # noqa: E402

_PY = sys.executable
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(_REPO, "data"))


def load_state(path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"done": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_state(path, state: Dict[str, Any]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pending(manifest_dict: Dict[str, Any], state: Dict[str, Any]) -> List[Dict[str, Any]]:
    done = state.get("done", {})
    return [b for b in mf.batches(manifest_dict) if b.get("id") not in done]


def scraper_argv(batch: Dict[str, Any]) -> Optional[List[str]]:
    src = batch.get("source")
    if src == "appledocs-docc":
        argv = [os.path.join("scripts", "scrape_apple_docs.py"),
                "--root", batch["root"]]
        if batch.get("max_pages"):
            argv += ["--max-pages", str(batch["max_pages"])]
        if batch.get("scope"):
            argv += ["--scope", batch["scope"]]
        return argv
    if src == "swift-evolution":
        return [os.path.join("scripts", "scrape_swift_evolution.py")]
    if src == "wwdc":
        argv = [os.path.join("scripts", "scrape_wwdc_transcripts.py")]
        if batch.get("limit"):
            argv += ["--limit", str(batch["limit"])]
        if batch.get("keywords"):
            argv += ["--keywords", batch["keywords"]]
        if batch.get("framework"):
            argv += ["--default-framework", batch["framework"]]
        # Distinct --out per batch id: multiple wwdc-sourced batches (e.g. the
        # default audio sweep plus a topic-specific one) must not clobber each
        # other's .jsonl/.fetch.json — see _flat_jsonl_for.
        argv += ["--out", _flat_jsonl_for(batch)]
        return argv
    return None  # web handled in-process


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")


async def _run_async(argv: List[str]) -> None:
    print(f"  $ {' '.join(argv)}", flush=True)
    proc = await asyncio.create_subprocess_exec(_PY, *argv, cwd=_REPO)
    rc = await proc.wait()
    if rc != 0:
        raise subprocess.CalledProcessError(rc, [_PY, *argv])


def _flat_jsonl_for(batch: Dict[str, Any]) -> Optional[str]:
    """Path of the flat .jsonl a scraper writes for this batch.

    Keyed by batch id (not just source) for "wwdc": multiple wwdc-sourced
    batches (e.g. the default audio sweep plus a topic-specific one like
    apple-mapkit's WWDC sessions) each need their own file, or a concurrent
    run would clobber one another's output.
    """
    base = os.path.join(DATA_DIR, "dapt", "extra-sources")
    src = batch.get("source")
    if src == "swift-evolution":
        return os.path.join(base, "swift-evolution-proposals.jsonl")
    if src == "wwdc":
        return os.path.join(base, f"{batch.get('id', 'wwdc')}.jsonl")
    return None


async def _scrape_web(batch: Dict[str, Any]) -> str:
    """Scrape each URL via the generic WebScraper, save a normalized crawl
    artifact, and return its path."""
    from app import scraper, storage
    ws = scraper.WebScraper()
    results = []
    for url in batch["urls"]:
        try:
            res = await ws.scrape(url)
            results.append({"url": res.get("url", url), "markdown": res.get("markdown", ""),
                            "metadata": res.get("metadata", {})})
        except Exception as exc:  # one bad URL never kills the batch
            print(f"  ! web scrape failed {url}: {exc}", flush=True)
    job = crawl_shape.job_from_results(results, base_url=batch["urls"][0],
                                       source=mf.corpus_source(batch), now=_now())
    return os.path.join(storage.CRAWLS_DIR, storage.save_crawl(job) + ".json")


async def _artifact_for_batch_async(batch: Dict[str, Any]) -> str:
    """Scrape one batch and return its crawl-artifact path. Artifacts always
    live under storage.CRAWLS_DIR (the single source for crawl paths). Apple
    DocC scrapes write to a deterministic per-batch --out path so concurrent
    scrapes never race on artifact detection."""
    from app import storage
    src = batch["source"]
    crawls = storage.CRAWLS_DIR
    if src == "appledocs-docc":
        out = os.path.join(crawls, f"{batch['id']}-{_stamp()}.json")
        await _run_async(scraper_argv(batch) + ["--out", out])
        return out
    if src == "web":
        return await _scrape_web(batch)
    # swift-evolution / wwdc -> run, then normalize their flat jsonl in-process
    await _run_async(scraper_argv(batch))
    flat = _flat_jsonl_for(batch)
    results = crawl_shape.read_flat_jsonl(flat, mf.corpus_source(batch))
    base_url = results[0]["url"] if results else f"about:{src}"
    job = crawl_shape.job_from_results(results, base_url=base_url,
                                       source=mf.corpus_source(batch), now=_now())
    return os.path.join(crawls, storage.save_crawl(job) + ".json")


def _scrape_fn():
    async def scrape(batch: Dict[str, Any]) -> str:
        return await _artifact_for_batch_async(batch)
    return scrape


def select_todo(all_batches: List[Dict[str, Any]], pend: List[Dict[str, Any]], *,
                batch: str, batches: str, run_all: bool) -> List[Dict[str, Any]]:
    if batch:
        return [b for b in all_batches if b["id"] == batch]
    if batches:
        wanted = [s for s in batches.split(",") if s]
        return [b for b in all_batches if b["id"] in wanted]
    if run_all:
        return pend
    return pend[:1]


def _batch_host(batch: Dict[str, Any]) -> str:
    """Host used for the per-host concurrency cap; '' when a batch has no single
    host (swift-evolution / wwdc) so it isn't throttled against others."""
    if batch.get("root"):
        return workflow.host_of(batch["root"])
    urls = batch.get("urls") or []
    return workflow.host_of(urls[0]) if urls else ""


async def run_drain(batches: List[Dict[str, Any]], *, base: str,
                    include_restricted: bool, do_sft: bool, concurrency: int,
                    scrape_fn, per_host: Optional[int] = None,
                    progress: Optional[workflow.Progress] = None) -> Dict[str, Any]:
    """Three-phase fan-out drain. scrape_fn(batch) -> artifact path (awaitable).

    Phase 1 fans out the scrape across batches; Phase 2 routes each new artifact
    serially (single writer keeps the append-only ledgers race-free); Phase 3
    runs one global SFT pass. A batch is marked ok once its build succeeds.
    """
    prog = progress or workflow.Progress()
    state: Dict[str, Any] = {}

    # Phase 1 — SCRAPE (fan-out, bounded + per-host)
    prog.phase("Scrape", total=len(batches))
    results = await workflow.fan_out(
        batches, scrape_fn, concurrency=concurrency, per_host=per_host,
        host_fn=_batch_host, label_fn=lambda b: b["id"], progress=prog,
    )

    # Phase 2 — BUILD (serial reduce, single writer)
    prog.phase("Build", total=sum(1 for r in results if r.ok))
    for r in results:
        bid = r.item["id"]
        if not r.ok:
            state[bid] = {"status": "failed", "error": r.error, "ts": _now()}
            continue
        source = mf.corpus_source(r.item)
        try:
            recs = build_corpus.records_from_crawl(r.value, source)
            stats = build_corpus.write_records(recs, base, source)
            state[bid] = {"status": "ok", "artifact": r.value, "stats": stats, "ts": _now()}
            prog.task_done(bid, ok=True, detail=str(stats.get("written", {})))
        except Exception as exc:
            state[bid] = {"status": "failed", "error": f"build: {exc}", "ts": _now()}
            prog.task_done(bid, ok=False, detail=str(exc))

    # Phase 3 — SFT (one global pass; idempotent via its own state file)
    if do_sft:
        prog.phase("SFT", total=1)
        try:
            stats = await generate_sft.run(
                os.path.join(base, "corpus", "rag"),
                os.path.join(base, "corpus", "sft"),
                include_restricted=include_restricted, n=2, limit=0,
                state_path=os.path.join(base, "corpus", ".sft_state.json"),
            )
            prog.task_done("synthetic-sft", ok=True, detail=str(stats))
        except Exception as exc:
            prog.task_done("synthetic-sft", ok=False, detail=str(exc))

    return state


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", default=os.path.join("scripts", "swift64_manifest.yaml"))
    ap.add_argument("--state", default=os.path.join(DATA_DIR, "corpus", ".loop_state.json"))
    ap.add_argument("--list", action="store_true", help="list batches + done status")
    ap.add_argument("--dry-run", action="store_true", help="print the batch plan, scrape nothing")
    ap.add_argument("--batch", default="", help="run exactly one batch id")
    ap.add_argument("--all", action="store_true", help="drain every pending batch")
    ap.add_argument("--concurrency", type=int, default=3, help="batches scraped in parallel")
    ap.add_argument("--per-host", type=int, default=1,
                    help="max concurrent batches sharing a host (politeness; 0=unlimited)")
    ap.add_argument("--batches", default="", help="comma-separated batch ids to fan out")
    ap.add_argument("--no-sft", dest="do_sft", action="store_false", default=True,
                    help="skip the global synthetic-SFT pass")
    args = ap.parse_args()

    manifest_dict = mf.load_manifest(os.path.join(_REPO, args.manifest))
    errors = mf.validate_manifest(manifest_dict)
    if errors:
        print("manifest invalid:\n  " + "\n  ".join(errors))
        return 1
    include_restricted = bool(manifest_dict.get("sft_include_restricted", True))
    state = load_state(args.state)
    pend = pending(manifest_dict, state)

    if args.list:
        for b in mf.batches(manifest_dict):
            mark = "done" if b["id"] in state.get("done", {}) else "todo"
            print(f"  [{mark}] {b['id']:24s} {b['source']:16s} {b.get('version_hint','')}")
        return 0
    if args.dry_run:
        print("pending batches:")
        for b in pend:
            print(f"  {b['id']:24s} {b['source']:16s} argv={scraper_argv(b)}")
        return 0

    todo = select_todo(mf.batches(manifest_dict), pend,
                       batch=args.batch, batches=args.batches, run_all=args.all)
    if args.batch and not todo:
        print(f"no such batch: {args.batch}")
        return 1
    if not todo:
        print("nothing to do — all batches done (or none selected)")
        return 0

    base = DATA_DIR
    new_state = asyncio.run(run_drain(
        todo, base=base, include_restricted=include_restricted, do_sft=args.do_sft,
        concurrency=args.concurrency, per_host=(args.per_host or None),
        scrape_fn=_scrape_fn(), progress=workflow.Progress(),
    ))
    state.setdefault("done", {}).update(new_state)
    save_state(args.state, state)

    ok = sum(1 for v in state["done"].values() if v.get("status") == "ok")
    print(f"\nstate: {ok} ok / {len(mf.batches(manifest_dict))} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
