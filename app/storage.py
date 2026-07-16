"""Persistence for scrape/crawl artifacts.

Every successful scrape and every finished crawl is written to disk as a
paired .json (full structured result) + .md (human-readable markdown) file
under DATA_DIR. DATA_DIR is bind-mounted to ./data on the host, so artifacts
survive container restarts and redeploys.
"""
import os
import re
import json
import shutil
import time
import uuid
import datetime
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("storage")

# In the container WORKDIR is /workspace, so this resolves to /workspace/data
# (bind-mounted to ./data on the host). Overridable via the DATA_DIR env var.
_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
DATA_DIR = os.environ.get("DATA_DIR", _DEFAULT_DATA_DIR)
SCRAPES_DIR = os.path.join(DATA_DIR, "scrapes")
CRAWLS_DIR = os.path.join(DATA_DIR, "crawls")
RESEARCH_DIR = os.path.join(DATA_DIR, "research")
RESEARCH_CHECKPOINTS_DIR = os.path.join(RESEARCH_DIR, "checkpoints")
CRAWL_CHECKPOINTS_DIR = os.path.join(CRAWLS_DIR, "checkpoints")
LLMSTXT_DIR = os.path.join(DATA_DIR, "llmstxt")


def ensure_dirs() -> None:
    os.makedirs(SCRAPES_DIR, exist_ok=True)
    os.makedirs(CRAWLS_DIR, exist_ok=True)
    os.makedirs(RESEARCH_DIR, exist_ok=True)
    os.makedirs(RESEARCH_CHECKPOINTS_DIR, exist_ok=True)
    os.makedirs(CRAWL_CHECKPOINTS_DIR, exist_ok=True)
    os.makedirs(LLMSTXT_DIR, exist_ok=True)


def _slug(url: str, maxlen: int = 60) -> str:
    s = re.sub(r"^https?://", "", url or "")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s[:maxlen] or "page"


def _stem(url: str) -> str:
    """Timestamped, collision-resistant filename stem."""
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{ts}__{_slug(url)}__{uuid.uuid4().hex[:6]}"


def save_scrape(result: Dict[str, Any]) -> str:
    """Persist a single scrape result. Returns the artifact stem."""
    ensure_dirs()
    stem = _stem(result.get("url", ""))
    with open(os.path.join(SCRAPES_DIR, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    md = (
        f"# {result.get('title', '')}\n\n"
        f"> source: {result.get('url', '')}\n\n"
        f"{result.get('markdown', '')}\n"
    )
    with open(os.path.join(SCRAPES_DIR, stem + ".md"), "w", encoding="utf-8") as f:
        f.write(md)
    return stem


def save_crawl(job: Dict[str, Any]) -> str:
    """Persist a finished crawl job (full json + concatenated markdown)."""
    ensure_dirs()
    stem = _stem(job.get("base_url", ""))
    with open(os.path.join(CRAWLS_DIR, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    results = job.get("results", []) or []
    parts = [
        f"# Crawl: {job.get('base_url', '')}",
        f"\n> {len(results)} pages · status={job.get('status')} · "
        f"{job.get('start_time')} → {job.get('end_time')}\n",
    ]
    for p in results:
        parts.append(
            f"\n\n---\n\n## {p.get('title', '')}\n\n"
            f"> {p.get('url', '')}\n\n{p.get('markdown', '')}"
        )
    with open(os.path.join(CRAWLS_DIR, stem + ".md"), "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return stem


def save_research(job: Dict[str, Any]) -> str:
    """Persist a finished research run: .json (full job state + provenance)
    + .md (the report). Returns the artifact stem."""
    ensure_dirs()
    stem = _stem(job.get("query", ""))
    with open(os.path.join(RESEARCH_DIR, stem + ".json"), "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    md = (f"# Research: {job.get('query', '')}\n\n"
          f"> status={job.get('status')} · {len(job.get('sources') or [])} sources\n\n"
          f"{job.get('report') or ''}\n")
    with open(os.path.join(RESEARCH_DIR, stem + ".md"), "w", encoding="utf-8") as f:
        f.write(md)
    return stem


def save_llmstxt(url: str, llms_txt: str, llms_full_txt: str) -> Dict[str, str]:
    """Persist a generated llms.txt pair; returns their /data-relative paths."""
    ensure_dirs()
    stem = _stem(url)
    paths = {}
    for suffix, text in (("llms.txt", llms_txt), ("llms-full.txt", llms_full_txt)):
        name = f"{stem}-{suffix}"
        with open(os.path.join(LLMSTXT_DIR, name), "w", encoding="utf-8") as f:
            f.write(text)
        key = "llmstxt_path" if suffix == "llms.txt" else "llms_full_path"
        paths[key] = f"data/llmstxt/{name}"
    return paths


def _save_checkpoint(folder: str, job_id: str, payload: Dict[str, Any]) -> None:
    """Atomic checkpoint write (json tmp + os.replace).

    The tmp name carries a uuid suffix so concurrent writers of the same job
    (the crawler's worker pool, unlike research's single loop) can never
    interleave into a corrupt file — last os.replace wins, both are complete.
    """
    ensure_dirs()
    path = os.path.join(folder, f"{job_id}.json")
    tmp = f"{path}.{uuid.uuid4().hex[:8]}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


def _load_checkpoints(folder: str, label: str) -> List[Dict[str, Any]]:
    """All checkpoints in a folder, newest updated_at first; corrupt or
    unreadable files are skipped (never raises) — a bad checkpoint must not
    prevent startup or the healthy ones from restoring."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(folder):
        return out
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and payload.get("job"):
                out.append(payload)
        except Exception as e:
            logger.warning("skipping unreadable %s checkpoint %s: %s", label, name, e)
    out.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return out


def _delete_checkpoint(folder: str, job_id: str, label: str) -> None:
    """Best-effort removal once a run is terminal (its artifact is saved)."""
    try:
        path = os.path.join(folder, f"{job_id}.json")
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        logger.warning("failed to delete %s checkpoint %s: %s", label, job_id, e)


def save_research_checkpoint(job_id: str, payload: Dict[str, Any]) -> None:
    """Restart-survival unit for in-flight research runs (spec-deep-research
    Phase 2): full job dict + loop state. Overwritten on every checkpoint
    site; deleted when the run reaches a terminal state."""
    _save_checkpoint(RESEARCH_CHECKPOINTS_DIR, job_id, payload)


def load_research_checkpoints() -> List[Dict[str, Any]]:
    return _load_checkpoints(RESEARCH_CHECKPOINTS_DIR, "research")


def delete_research_checkpoint(job_id: str) -> None:
    _delete_checkpoint(RESEARCH_CHECKPOINTS_DIR, job_id, "research")


def save_crawl_checkpoint(job_id: str, payload: Dict[str, Any]) -> None:
    """Restart-survival unit for in-flight crawls: full job dict + the loop
    state (visited set, pending frontier, screenshot counter) that otherwise
    lives only in run_crawl's frame."""
    _save_checkpoint(CRAWL_CHECKPOINTS_DIR, job_id, payload)


def load_crawl_checkpoints() -> List[Dict[str, Any]]:
    return _load_checkpoints(CRAWL_CHECKPOINTS_DIR, "crawl")


def delete_crawl_checkpoint(job_id: str) -> None:
    _delete_checkpoint(CRAWL_CHECKPOINTS_DIR, job_id, "crawl")


def _runs_dir() -> str:
    """Per-run raw-capture root, resolved from DATA_DIR at call time."""
    return os.path.join(DATA_DIR, "runs")


def save_run_raw(
    stem: str,
    page_no: int,
    *,
    raw_html: Optional[str] = None,
    screenshot: Optional[bytes] = None,
) -> Dict[str, str]:
    """Write a page's raw HTML and/or screenshot under data/runs/<stem>/.

    Returns the relative path(s) for the DB ({"raw_html_path","screenshot_path"})
    — only the keys actually written. Best-effort: a write failure logs and
    returns whatever did succeed rather than raising (raw capture is additive and
    must never fail a scrape).
    """
    out: Dict[str, str] = {}
    if not stem or (raw_html is None and not screenshot):
        return out
    try:
        run_dir = os.path.join(_runs_dir(), stem)
        os.makedirs(run_dir, exist_ok=True)
        if raw_html is not None:
            # The .txt suffix prevents browsers and generic static servers from
            # executing attacker-controlled page source as same-origin HTML.
            name = f"page-{page_no}.html.txt"
            with open(os.path.join(run_dir, name), "w", encoding="utf-8") as f:
                f.write(raw_html)
            out["raw_html_path"] = f"data/runs/{stem}/{name}"
        if screenshot:
            name = f"page-{page_no}.png"
            with open(os.path.join(run_dir, name), "wb") as f:
                f.write(screenshot)
            out["screenshot_path"] = f"data/runs/{stem}/{name}"
    except Exception as e:
        logger.warning("save_run_raw failed for %s/page-%s: %s", stem, page_no, e)
    return out


def prune(max_age_days: int, keep_runs: int = 50) -> Dict[str, int]:
    """Delete scrape/crawl/run artifacts older than max_age_days.

    Always keeps the `keep_runs` most-recent entries per kind so a quiet period
    never empties the store. NEVER touches data/index (the dedup index) or
    data/backups. Best-effort: never raises. Returns {"removed", "kept"}.
    """
    removed = kept = 0
    cutoff = time.time() - max(0, max_age_days) * 86400

    def _entries(folder, kind):
        if not os.path.isdir(folder):
            return []
        out = []
        if kind == "runs":
            names = [n for n in os.listdir(folder)
                     if os.path.isdir(os.path.join(folder, n))]
        else:
            # Collapse the .json/.md pair into a single stem entry.
            names = sorted({n[:-5] for n in os.listdir(folder) if n.endswith(".json")})
        for n in names:
            p = os.path.join(folder, n)
            # For file-pair kinds the stem has no extension on disk — stat the
            # .json half; for runs the stem IS the directory.
            stat_path = p if kind == "runs" else p + ".json"
            try:
                out.append((p, n, os.path.getmtime(stat_path)))
            except OSError:
                continue
        return out

    targets = [
        (SCRAPES_DIR, "scrapes"),
        (CRAWLS_DIR, "crawls"),
        (RESEARCH_DIR, "research"),
        (_runs_dir(), "runs"),
    ]
    for folder, kind in targets:
        entries = sorted(_entries(folder, kind), key=lambda e: e[2], reverse=True)
        for idx, (path, name, mtime) in enumerate(entries):
            if idx < keep_runs or mtime >= cutoff:
                kept += 1
                continue
            try:
                if kind == "runs":
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    for ext in (".json", ".md"):
                        f = os.path.join(folder, name + ext)
                        if os.path.exists(f):
                            os.remove(f)
                removed += 1
            except Exception as e:
                logger.warning("prune failed to remove %s: %s", path, e)

    # Stale checkpoints: a non-terminal checkpoint older than the cutoff is a
    # dead run — sweep without the keep_runs guard.
    for ckpt_dir in (RESEARCH_CHECKPOINTS_DIR, CRAWL_CHECKPOINTS_DIR):
        if not os.path.isdir(ckpt_dir):
            continue
        for name in os.listdir(ckpt_dir):
            if not name.endswith(".json"):
                continue
            p = os.path.join(ckpt_dir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
                    removed += 1
                else:
                    kept += 1
            except OSError:
                continue
    if removed:
        logger.info("prune: removed %d artifact(s), kept %d (max_age_days=%d, keep_runs=%d)",
                    removed, kept, max_age_days, keep_runs)
    return {"removed": removed, "kept": kept}


def list_artifacts() -> List[Dict[str, Any]]:
    """Return metadata for every saved artifact, newest first."""
    items: List[Dict[str, Any]] = []
    for kind, folder, subdir in (
        ("scrape", SCRAPES_DIR, "scrapes"),
        ("crawl", CRAWLS_DIR, "crawls"),
        ("research", RESEARCH_DIR, "research"),
    ):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            if not name.endswith(".json"):
                continue
            stem = name[:-5]
            jpath = os.path.join(folder, name)
            try:
                with open(jpath, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
            st = os.stat(jpath)
            if kind == "scrape":
                title = data.get("title", "") or data.get("url", "")
                url = data.get("url", "")
                pages = 1
            elif kind == "crawl":
                title = data.get("base_url", "")
                url = data.get("base_url", "")
                pages = len(data.get("results", []) or [])
            else:
                title = data.get("query", "")
                url = data.get("query", "")
                pages = len(data.get("sources", []) or [])
            items.append({
                "kind": kind,
                "stem": stem,
                "title": title,
                "url": url,
                "pages": pages,
                "bytes": st.st_size,
                "mtime": st.st_mtime,
                "json": f"/data/{subdir}/{stem}.json",
                "md": f"/data/{subdir}/{stem}.md",
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items
