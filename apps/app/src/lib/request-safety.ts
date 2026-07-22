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
  "partial",
  "failed",
  "cancelled",
  "timed_out",
  "interrupted",
]);

export function isTerminalCrawlStatus(status: string) {
  return TERMINAL_CRAWL_STATUSES.has(status);
}

const ACTIVE_CRAWL_PAGE_STATES = new Set([
  "pending",
  "leased",
  "retry_wait",
  "waiting_input",
]);

export function nextCrawlPageCursor(
  pages: Array<{ discovery_seq?: number; state?: string }>,
  serverCursor: number | null,
) {
  const unresolved = pages
    .filter((page) => (
      typeof page.discovery_seq === "number" &&
      typeof page.state === "string" &&
      ACTIVE_CRAWL_PAGE_STATES.has(page.state)
    ))
    .map((page) => page.discovery_seq as number);
  if (unresolved.length) {
    const first = Math.min(...unresolved);
    return first <= 0 ? null : first - 1;
  }
  return typeof serverCursor === "number" && Number.isFinite(serverCursor)
    ? serverCursor
    : null;
}

export function nextCrawlPageDrainCursor({
  requestedAfter,
  nextAfter,
  batchSize,
  capturedResults,
  expectedResults,
}: {
  requestedAfter: number | null;
  nextAfter: number | null;
  batchSize: number;
  capturedResults: number;
  expectedResults: number | null;
}) {
  if (batchSize === 0) return null;
  if (expectedResults !== null && capturedResults >= expectedResults) return null;
  if (typeof nextAfter !== "number" || !Number.isFinite(nextAfter)) return null;
  if (requestedAfter !== null && nextAfter <= requestedAfter) return null;
  return nextAfter;
}
