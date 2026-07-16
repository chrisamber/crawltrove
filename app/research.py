"""Autonomous deep-research runs: plan → search → read → assess → synthesize.

The job store is in-memory (same pattern as WebCrawler) but every in-flight
run is checkpointed to data/research/checkpoints/<job_id>.json (Phase 2 of
docs/spec-deep-research.md): full job dict + loop state, rewritten at every
expensive step. On restart the checkpoints rehydrate as status "interrupted"
and can be resumed — automatically at startup or via
POST /api/research/{jobId}/resume — without re-reading pages or double-
counting budgets. Every page fetch goes through the standard WebScraper
waterfall and is saved with storage.save_scrape, so research pages carry the
usual corpus signals and citations point at real data/ artifacts. Every LLM
step goes through research_llm (the /api/extract backend waterfall).
"""
import asyncio
import datetime
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

from app import research_llm, search, storage, vecindex, webhooks
from app.db import repo
from app.normalize import normalize_url
from app.scraper import WebScraper

logger = logging.getLogger("research")

MAX_CONCURRENT = 2
DEFAULT_LLM_CALL_CAP = 40
TERMINAL = ("completed", "failed", "cancelled")
PICK_PER_ROUND = 6


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class StopRun(Exception):
    """Internal control flow: a budget ceiling was hit or cancel was requested."""


def _fresh_state() -> Dict[str, Any]:
    """Loop-carried state, threaded through _loop so it can be checkpointed.

    phase "search" = the next thing to do is search+select for round_no+1;
    phase "read" = mid-round, `pending` is the URLs still to read in round_no.
    """
    return {"seen": set(), "pending": [], "queries": None,
            "round_no": 0, "phase": "search"}


class ResearchManager:
    def __init__(self):
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.scraper = WebScraper()
        # Strong refs to resumed background tasks (runner pattern) — resumes
        # are not tied to a request, so BackgroundTasks can't hold them.
        self._tasks: "set[asyncio.Task]" = set()
        # job_id -> loop-state payload from a restored checkpoint, consumed
        # when the job is resumed.
        self._pending_resume: Dict[str, Dict[str, Any]] = {}

    def create_job(self, query: str, max_rounds: int = 4, max_pages: int = 25,
                   max_minutes: int = 30,
                   seed_urls: Optional[List[str]] = None) -> str:
        job_id = str(uuid.uuid4())
        self.jobs[job_id] = {
            "job_id": job_id, "query": query, "status": "queued",
            "max_rounds": max_rounds, "max_pages": max_pages,
            "max_minutes": max_minutes, "llm_call_cap": DEFAULT_LLM_CALL_CAP,
            "seed_urls": list(seed_urls or []),
            "rounds_run": 0, "pages_scraped": 0, "llm_calls": 0,
            "activity": [], "sources": [], "report": None,
            "insufficient": False, "unverified_citations": [],
            "cancel_requested": False, "error": None,
            "start_time": None, "end_time": None, "artifact_stem": None,
            "deadline_utc": None,
        }
        return job_id

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def active_jobs(self) -> List[str]:
        # "interrupted" jobs are rehydrated-but-idle: they consume no worker
        # and must not starve the MAX_CONCURRENT cap.
        return [j["job_id"] for j in self.jobs.values()
                if j["status"] not in TERMINAL and j["status"] != "interrupted"]

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job["status"] in TERMINAL:
            return False
        job["cancel_requested"] = True
        return True

    def _log(self, job: Dict[str, Any], phase: str, message: str) -> None:
        job["activity"].append({"ts": _now(), "phase": phase, "message": message})
        logger.info("research %s [%s] %s", job["job_id"], phase, message)

    def _check(self, job: Dict[str, Any], deadline: float) -> None:
        if job["cancel_requested"]:
            raise StopRun("cancelled")
        if time.monotonic() > deadline:
            raise StopRun("max_minutes")

    async def _llm(self, job: Dict[str, Any], fn, *args):
        """Budgeted LLM step. Synthesis is exempt (called directly) so a run
        that hits the cap can still produce its report."""
        if job["llm_calls"] >= job["llm_call_cap"]:
            raise StopRun("llm_call_cap")
        job["llm_calls"] += 1
        return await fn(*args)

    def _checkpoint(self, job: Dict[str, Any], state: Dict[str, Any]) -> None:
        """Persist the run for restart-survival. Swallows its own failures —
        a checkpoint write must never fail the research run (resilient like
        every other signal)."""
        try:
            storage.save_research_checkpoint(job["job_id"], {
                "version": 1,
                "job": job,
                "loop": {
                    "round_no": state["round_no"],
                    "phase": state["phase"],
                    "queries": state["queries"],
                    "pending": list(state["pending"]),
                    "seen": sorted(state["seen"]),
                },
                "updated_at": _now(),
            })
        except Exception as e:
            logger.warning("checkpoint failed for %s: %s", job.get("job_id"), e)

    def restore_from_checkpoints(self) -> List[str]:
        """Rehydrate non-terminal checkpoints into the job store (startup).

        Restored jobs get status "interrupted" and stay idle until resumed;
        their loop state is parked for resume_research. Returns the restored
        job ids, newest checkpoint first. Never raises.
        """
        restored: List[str] = []
        try:
            payloads = storage.load_research_checkpoints()
        except Exception as e:
            logger.error("failed to load research checkpoints: %s", e)
            return restored
        for payload in payloads:
            job = payload.get("job") or {}
            job_id = job.get("job_id")
            if not job_id or job_id in self.jobs:
                continue
            if job.get("status") in TERMINAL:
                # Crash between save_research and checkpoint delete — done.
                storage.delete_research_checkpoint(job_id)
                continue
            job["status"] = "interrupted"
            self.jobs[job_id] = job
            self._pending_resume[job_id] = payload.get("loop") or {}
            self._log(job, "restore", "restored from checkpoint after restart")
            restored.append(job_id)
        return restored

    def resume_research(self, job_id: str) -> bool:
        """Resume an interrupted job as a background task (runner pattern:
        asyncio.create_task + strong ref, not request-scoped BackgroundTasks)."""
        job = self.jobs.get(job_id)
        if not job or job["status"] != "interrupted":
            return False
        loop = self._pending_resume.pop(job_id, None) or {}
        state = _fresh_state()
        state["seen"] = set(loop.get("seen") or [])
        state["pending"] = list(loop.get("pending") or [])
        state["queries"] = loop.get("queries")
        state["round_no"] = int(loop.get("round_no") or 0)
        state["phase"] = loop.get("phase") or "search"
        job["status"] = "queued"
        self._log(job, "resume", "resuming from checkpoint")
        task = asyncio.create_task(self.run_research(job_id, state=state))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def run_research(self, job_id: str,
                           state: Optional[Dict[str, Any]] = None) -> None:
        """Entry point for fresh (BackgroundTask) and resumed runs. Always
        terminates the job; never raises."""
        job = self.jobs.get(job_id)
        if not job or job["status"] in TERMINAL:
            return
        job["start_time"] = job["start_time"] or _now()
        state = state if state is not None else _fresh_state()

        # Wall-clock deadline so it survives a restart; the monotonic deadline
        # _check() polls is derived from whatever budget remains.
        now = datetime.datetime.now(datetime.timezone.utc)
        if not job.get("deadline_utc"):
            job["deadline_utc"] = (
                now + datetime.timedelta(minutes=job["max_minutes"])).isoformat()
        try:
            remaining = (datetime.datetime.fromisoformat(job["deadline_utc"])
                         - now).total_seconds()
        except Exception:
            remaining = job["max_minutes"] * 60
        deadline = time.monotonic() + max(0.0, remaining)

        self._checkpoint(job, state)
        await repo.upsert_research_run(job)
        try:
            if remaining <= 0:
                raise StopRun("max_minutes")
            await self._loop(job, deadline, state)
        except StopRun as e:
            self._log(job, "stop", f"run stopped early: {e}")
        except Exception as e:
            job["error"] = str(e)
            self._log(job, "error", f"run failed: {e}")
        await self._synthesize(job)
        if job["error"] is not None:
            job["status"] = "failed"
        elif job["cancel_requested"]:
            job["status"] = "cancelled"
        else:
            job["status"] = "completed"
        job["end_time"] = _now()
        try:
            job["artifact_stem"] = storage.save_research(job)
            storage.delete_research_checkpoint(job_id)
        except Exception as e:
            logger.error("failed to save research artifact for %s: %s", job_id, e)
        # Best-effort semantic index of the finished report (no-op unless
        # EMBEDDINGS_BASE_URL is set; swallow-and-default).
        stem = job.get("artifact_stem")
        if stem and job.get("report"):
            await vecindex.index_document(
                "research", stem, None, job.get("report") or "",
                meta={"query": job.get("query"), "stem": stem})
        await repo.upsert_research_run(job)
        # One research.* event per terminal run (completed/failed/cancelled),
        # mirroring record_run_finish for scrape/crawl runs. Swallowed inside.
        await webhooks.deliver_research(job)

    async def _loop(self, job: Dict[str, Any], deadline: float,
                    state: Dict[str, Any]) -> None:
        seen: set = state["seen"]
        if state["queries"] is None:
            job["status"] = "planning"
            state["queries"] = await self._llm(job, research_llm.plan_queries,
                                               job["query"])
            self._log(job, "planning", f"queries: {state['queries']}")
            # Caller-supplied URLs read first (drained by round 1's select)
            state["pending"] = [u for u in job["seed_urls"]
                                if normalize_url(u) not in seen]
            self._checkpoint(job, state)

        # phase "read" resumes inside round_no; otherwise the next round starts.
        start_round = (state["round_no"] if state["phase"] == "read"
                       else state["round_no"] + 1)

        for round_no in range(max(1, start_round), job["max_rounds"] + 1):
            self._check(job, deadline)
            if job["pages_scraped"] >= job["max_pages"]:
                raise StopRun("max_pages")
            job["rounds_run"] = max(job["rounds_run"], round_no)
            state["round_no"] = round_no

            if state["phase"] != "read":
                job["status"] = "searching"
                fresh: List[Dict[str, str]] = []
                for q in state["queries"]:
                    results = await search.search(q)
                    self._log(job, "searching", f"{q!r} -> {len(results)} results")
                    for r in results:
                        norm = normalize_url(r["url"])
                        if (r["url"].startswith("http") and norm not in seen
                                and all(normalize_url(f["url"]) != norm for f in fresh)):
                            fresh.append(r)

                budget_left = job["max_pages"] - job["pages_scraped"]
                urls = [u for u in state["pending"]
                        if normalize_url(u) not in seen][:budget_left]
                if fresh and budget_left - len(urls) > 0:
                    k = min(PICK_PER_ROUND, budget_left - len(urls))
                    picked = await self._llm(job, research_llm.select_urls,
                                             job["query"], fresh, k)
                    urls += [u for u in picked if u not in urls]
                self._log(job, "selecting", f"reading {len(urls)} pages")
                state["pending"] = urls
                state["phase"] = "read"
                self._checkpoint(job, state)

            job["status"] = "reading"
            while state["pending"]:
                self._check(job, deadline)
                if job["pages_scraped"] >= job["max_pages"]:
                    raise StopRun("max_pages")
                url = state["pending"].pop(0)
                seen.add(normalize_url(url))
                # Claim the URL on disk before the expensive read: a crash
                # mid-read skips it on resume instead of crash-looping on it.
                self._checkpoint(job, state)
                await self._read_page(job, url)
                self._checkpoint(job, state)

            self._check(job, deadline)
            job["status"] = "planning"
            verdict = await self._llm(job, research_llm.assess, job["query"],
                                      job["sources"], job["max_rounds"] - round_no)
            self._log(job, "assessing",
                      f"enough={verdict.get('enough')}: {verdict.get('reasoning', '')}")
            if verdict.get("enough") or not verdict.get("new_queries"):
                return
            state["queries"] = verdict["new_queries"]
            state["phase"] = "search"
            self._checkpoint(job, state)

    async def _read_page(self, job: Dict[str, Any], url: str) -> None:
        try:
            result = await self.scraper.scrape(url, wait_for_ms=500)
        except Exception as e:
            self._log(job, "reading", f"scrape crashed for {url}: {e}")
            return
        job["pages_scraped"] += 1
        if not result.get("success"):
            self._log(job, "reading",
                      f"scrape failed for {url}: {result.get('error')}")
            return
        result.pop("_raw", None)
        artifact = None
        try:
            artifact = storage.save_scrape(result)
        except Exception as e:
            logger.warning("failed to save research page artifact: %s", e)
        meta = result.get("metadata") or {}
        notes = await self._llm(job, research_llm.take_notes,
                                job["query"], result.get("markdown") or "", url)
        source = {
            "index": len(job["sources"]) + 1,
            "url": url,
            "title": result.get("title") or url,
            "artifact": f"data/scrapes/{artifact}.json" if artifact else None,
            "quality_score": (meta.get("quality") or {}).get("score"),
            "relevant": bool(notes.get("relevant")),
            "notes": notes.get("notes") or "",
            "key_facts": notes.get("key_facts") or [],
        }
        job["sources"].append(source)
        self._log(job, "reading",
                  f"read [{source['index']}] {url} (relevant={source['relevant']})")

    async def _synthesize(self, job: Dict[str, Any]) -> None:
        """Best-effort wind-down: always leaves job['report'] set; never raises.
        Quality scores RANK the synthesis input; nothing is dropped (signals
        flag, never filter — the full source list stays in job['sources'])."""
        job["status"] = "synthesizing"
        relevant = sorted((s for s in job["sources"] if s["relevant"]),
                          key=lambda s: s.get("quality_score") or 0.0,
                          reverse=True)
        if not relevant:
            job["insufficient"] = True
            job["report"] = (
                f"# Research: {job['query']}\n\nInsufficient sources: no relevant "
                "pages were gathered, so no synthesis was produced. See the "
                "activity log for what was tried.")
            return
        try:
            job["llm_calls"] += 1  # counted but exempt from the cap
            out = await research_llm.synthesize(job["query"], relevant)
        except Exception as e:
            self._log(job, "synthesizing", f"synthesis failed: {e}")
            if job["error"] is None:
                job["error"] = f"synthesis failed: {e}"
            facts = "\n".join(f"- [{s['index']}] {fact}"
                              for s in relevant for fact in s["key_facts"])
            job["report"] = (f"# Research (partial): {job['query']}\n\n"
                             f"Synthesis failed; raw findings below.\n\n{facts}")
            return
        job["insufficient"] = bool(out.get("insufficient"))
        report = out.get("report_markdown") or ""
        job["unverified_citations"] = research_llm.validate_citations(
            report, len(job["sources"]))
        sources_md = "\n".join(
            f"{s['index']}. {s['title']} — {s['url']}"
            + (f" (`{s['artifact']}`)" if s["artifact"] else "")
            for s in sorted(job["sources"], key=lambda s: s["index"]))
        job["report"] = f"{report}\n\n## Sources\n{sources_md}"
