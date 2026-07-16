"""Outbound webhooks: notify a configured endpoint when a run reaches a terminal state.

Opt-in via WEBHOOK_URL (no-op when unset, mirroring DATABASE_URL/DATA_RETENTION_DAYS).
Fire-and-forget: any delivery failure is logged and swallowed — a webhook must never
break a run, exactly like the persistence layer it hangs off.

Config:
    WEBHOOK_URL        endpoint to POST run-completion events to (unset => disabled)
    WEBHOOK_SECRET     optional; HMAC-SHA256 sign the body, sent as
                       X-CrawlTrove-Signature: sha256=<hex> so receivers can verify
    WEBHOOK_TIMEOUT_S  per-request timeout, default 10s

Dispatch points: repo.record_run_finish (both scrape and crawl runs finalize
through it) fires run.* events, and ResearchManager.run_research's finalize
fires research.* events — one event per terminal run either way.
"""
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("webhooks")

EVENT_RUN_COMPLETED = "run.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_RESEARCH_COMPLETED = "research.completed"
EVENT_RESEARCH_FAILED = "research.failed"
EVENT_RESEARCH_CANCELLED = "research.cancelled"


def _url() -> str:
    return os.environ.get("WEBHOOK_URL", "").strip()


def _secret() -> str:
    return os.environ.get("WEBHOOK_SECRET", "")


def _timeout() -> float:
    try:
        return float(os.environ.get("WEBHOOK_TIMEOUT_S", "10"))
    except ValueError:
        return 10.0


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(run: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a scrape_runs row into the webhook event body."""
    status = run.get("status")
    event = EVENT_RUN_FAILED if status == "failed" else EVENT_RUN_COMPLETED
    return {
        "event": event,
        "run": {
            "id": run.get("id"),
            "job_id": run.get("job_id"),
            "external_id": run.get("external_id"),
            "trigger": run.get("trigger"),
            "status": status,
            "engine_used": run.get("engine_used"),
            "pages_count": run.get("pages_count"),
            "error_message": run.get("error_message"),
            "raw_output_path": run.get("raw_output_path"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
        },
    }


def build_research_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """Shape a research job dict into the webhook event body."""
    status = job.get("status")
    event = {"failed": EVENT_RESEARCH_FAILED,
             "cancelled": EVENT_RESEARCH_CANCELLED}.get(status,
                                                        EVENT_RESEARCH_COMPLETED)
    stem = job.get("artifact_stem")
    return {
        "event": event,
        "research": {
            "job_id": job.get("job_id"),
            "query": job.get("query"),
            "status": status,
            "rounds_run": job.get("rounds_run"),
            "pages_scraped": job.get("pages_scraped"),
            "llm_calls": job.get("llm_calls"),
            "sources_count": len(job.get("sources") or []),
            "insufficient": job.get("insufficient"),
            "error": job.get("error"),
            "artifact_stem": stem,
            "report_path": f"data/research/{stem}.md" if stem else None,
            "artifact_path": f"data/research/{stem}.json" if stem else None,
            "start_time": job.get("start_time"),
            "end_time": job.get("end_time"),
        },
    }


async def _post(payload: Dict[str, Any]) -> bool:
    """Shared delivery: sign + POST one event body to WEBHOOK_URL.

    Returns True on a 2xx/3xx; all exceptions are swallowed — callers must
    not let a webhook fail a run.
    """
    url = _url()
    if not url:
        return False
    # default=str renders datetime/Decimal fields (datetimes come straight off the
    # run row in production) without a bespoke serializer.
    body = json.dumps(payload, default=str).encode()
    headers = {"Content-Type": "application/json"}
    secret = _secret()
    if secret:
        headers["X-CrawlTrove-Signature"] = _sign(body, secret)
    # ponytail: synchronous POST off the run's background task (HTTP response already
    # returned the runId). Bounded by WEBHOOK_TIMEOUT_S; make it create_task if a slow
    # receiver delaying task teardown ever matters. No retry/queue — receivers dedup.
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(url, content=body, headers=headers)
    except Exception as e:
        logger.warning("webhook delivery failed (%s): %s", url, e)
        return False
    if resp.status_code >= 400:
        logger.warning("webhook %s -> %s returned %s",
                       payload.get("event"), url, resp.status_code)
        return False
    logger.info("webhook delivered to %s (%s)", url, resp.status_code)
    return True


async def deliver(run: Optional[Dict[str, Any]]) -> bool:
    """POST a run-completion event to WEBHOOK_URL. Returns True on a 2xx/3xx.

    No-op (returns False, no network) when WEBHOOK_URL is unset or run is None.
    """
    if not _url() or not run:
        return False
    return await _post(build_payload(run))


async def deliver_research(job: Optional[Dict[str, Any]]) -> bool:
    """POST a research terminal-state event. Same no-op/swallow contract as
    deliver()."""
    if not _url() or not job:
        return False
    return await _post(build_research_payload(job))
