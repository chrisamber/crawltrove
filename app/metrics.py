"""Low-cardinality Prometheus metrics for durable crawl operations."""
from __future__ import annotations

import threading
from typing import Any

from prometheus_client import Counter, Gauge, Histogram


LABEL_VALUES = {
    "route": frozenset({
        "local_http", "owned_proxy_http", "local_browser", "firecrawl_scrape",
        "firecrawl_interact", "brightdata_unlocker", "browserbase_session", "unknown",
    }),
    "provider": frozenset({"local", "firecrawl", "brightdata", "browserbase", "unknown"}),
    "outcome": frozenset({
        "succeeded", "retry", "retryable_failure", "failed", "cancelled",
        "abandoned", "blocked_robots", "waiting_input", "unknown",
    }),
    "meter": frozenset({
        "credits", "requests", "browserMinutes", "proxyBytes", "unknown",
    }),
    "state": frozenset({
        "pending", "running", "leased", "retry_wait", "waiting_input", "succeeded",
        "http_error", "blocked_robots", "extraction_failed", "permanent_failed",
        "failed", "completed", "partial", "cancelled", "timed_out", "active",
        "draining", "revoked", "incompatible", "offline", "starting", "waiting",
        "connected", "resuming", "closed", "expired", "open", "half_open", "unknown",
    }),
    "capability": frozenset({
        "http", "browser", "proxy", "firecrawl_scrape", "firecrawl_interact",
        "brightdata_unlocker", "browserbase_session", "unknown",
    }),
    "backend": frozenset({"owned", "browserbase", "unknown"}),
    "decision": frozenset({"allowed", "blocked", "unavailable", "unknown"}),
    "kind": frozenset({"downloaded", "artifact", "unknown"}),
    "reason": frozenset({"transport", "policy", "budget", "lease_expired", "unknown"}),
    "component": frozenset({"process", "worker", "unknown"}),
}


METRIC_LABELS = {
    "jobs": ("state",),
    "tasks": ("state",),
    "queue_depth": ("state",),
    "fetch_duration": ("route", "outcome"),
    "extract_duration": ("outcome",),
    "bytes": ("kind",),
    "retries": ("reason",),
    "origins": ("state",),
    "robots": ("decision",),
    "browser": ("outcome",),
    "leases": ("outcome",),
    "memory": ("component",),
    "artifacts": ("outcome",),
    "acquisition_attempts": ("route", "provider", "outcome"),
    "provider_usage": ("provider", "meter"),
    "workers": ("state", "capability"),
    "live_sessions": ("state", "backend"),
}


def normalize_label(label: str, value: object) -> str:
    """Map untrusted values to a fixed label set before metric observation."""
    normalized = str(value)
    return normalized if normalized in LABEL_VALUES[label] else "unknown"


jobs = Counter("crawltrove_jobs_total", "Crawl jobs", METRIC_LABELS["jobs"])
tasks = Counter("crawltrove_tasks_total", "Crawl tasks", METRIC_LABELS["tasks"])
queue_depth = Gauge("crawltrove_queue_depth", "Queued crawl tasks", METRIC_LABELS["queue_depth"])
fetch_duration = Histogram(
    "crawltrove_fetch_duration_seconds", "Fetch duration", METRIC_LABELS["fetch_duration"],
)
extract_duration = Histogram(
    "crawltrove_extract_duration_seconds", "Extraction duration", METRIC_LABELS["extract_duration"],
)
bytes_processed = Counter("crawltrove_bytes_total", "Processed bytes", METRIC_LABELS["bytes"])
retries = Counter("crawltrove_retries_total", "Task retries", METRIC_LABELS["retries"])
origins = Gauge("crawltrove_origins", "Origins by circuit state", METRIC_LABELS["origins"])
robots = Counter("crawltrove_robots_total", "Robots decisions", METRIC_LABELS["robots"])
browser = Counter("crawltrove_browser_total", "Browser operations", METRIC_LABELS["browser"])
leases = Counter("crawltrove_leases_total", "Lease outcomes", METRIC_LABELS["leases"])
memory = Gauge("crawltrove_memory_bytes", "Process memory", METRIC_LABELS["memory"])
artifacts = Counter("crawltrove_artifacts_total", "Artifact outcomes", METRIC_LABELS["artifacts"])
acquisition_attempts = Counter(
    "crawltrove_acquisition_attempts_total", "Acquisition attempts",
    METRIC_LABELS["acquisition_attempts"],
)
provider_usage = Counter(
    "crawltrove_provider_usage_total", "Provider native usage", METRIC_LABELS["provider_usage"],
)
workers = Gauge(
    "crawltrove_workers", "Workers by state and capability", METRIC_LABELS["workers"],
    multiprocess_mode="livesum",
)
live_sessions = Gauge(
    "crawltrove_live_sessions", "Live sessions", METRIC_LABELS["live_sessions"],
    multiprocess_mode="livesum",
)

_counter_lock = threading.Lock()
_attempt_totals: dict[tuple[str, ...], float] = {}
_provider_totals: dict[tuple[str, ...], float] = {}


def record_acquisition_attempt(route: object, provider: object, outcome: object) -> None:
    acquisition_attempts.labels(
        normalize_label("route", route),
        normalize_label("provider", provider),
        normalize_label("outcome", outcome),
    ).inc()


def record_provider_usage(provider: object, meter: object, amount: float) -> None:
    provider_usage.labels(
        normalize_label("provider", provider), normalize_label("meter", meter),
    ).inc(max(0, amount))


def _reconcile_counter(
    metric: Counter,
    previous: dict[tuple[str, ...], float],
    rows: list[tuple[tuple[str, ...], float]],
) -> None:
    """Advance a process counter to durable totals without ever decrementing it."""
    with _counter_lock:
        for labels, total in rows:
            prior = previous.get(labels, 0.0)
            if total > prior:
                metric.labels(*labels).inc(total - prior)
                previous[labels] = total


def refresh_durable_metrics(snapshot: dict[str, list[tuple[Any, ...]]]) -> None:
    """Refresh bounded gauges and reconcile durable counters before a scrape."""
    queue_depth.clear()
    for state, count in snapshot.get("tasks", []):
        queue_depth.labels(normalize_label("state", state)).set(float(count))

    origins.clear()
    for state, count in snapshot.get("origins", []):
        origins.labels(normalize_label("state", state)).set(float(count))

    workers.clear()
    for state, capability, count in snapshot.get("workers", []):
        workers.labels(
            normalize_label("state", state), normalize_label("capability", capability),
        ).set(float(count))

    live_sessions.clear()
    for state, backend, count in snapshot.get("sessions", []):
        live_sessions.labels(
            normalize_label("state", state), normalize_label("backend", backend),
        ).set(float(count))

    attempt_rows: list[tuple[tuple[str, ...], float]] = []
    for route, provider, outcome, count in snapshot.get("attempts", []):
        attempt_labels = (
            normalize_label("route", route), normalize_label("provider", provider),
            normalize_label("outcome", outcome),
        )
        attempt_rows.append((attempt_labels, float(count)))
    _reconcile_counter(acquisition_attempts, _attempt_totals, attempt_rows)

    usage_rows: list[tuple[tuple[str, ...], float]] = []
    for provider, meter, amount in snapshot.get("usage", []):
        provider_labels = (
            normalize_label("provider", provider), normalize_label("meter", meter),
        )
        usage_rows.append((provider_labels, float(amount)))
    _reconcile_counter(provider_usage, _provider_totals, usage_rows)
