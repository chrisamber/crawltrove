export function isAbortError(error: unknown) {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

export function normalizePageLimit(value: number) {
  if (!Number.isFinite(value)) return 25;
  return Math.min(500, Math.max(1, Math.trunc(value)));
}

const TERMINAL_CRAWL_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

export function isTerminalCrawlStatus(status: string) {
  return TERMINAL_CRAWL_STATUSES.has(status);
}
