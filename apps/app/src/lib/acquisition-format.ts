export type AcquisitionRoute =
  | "local_http"
  | "owned_proxy_http"
  | "local_browser"
  | "brightdata_unlocker"
  | "firecrawl_scrape"
  | "browserbase_session"
  | "firecrawl_interact";

export type AcquisitionAttempt = {
  route: AcquisitionRoute;
  provider: string;
  outcome: string;
  blockReason?: string | null;
  durationMs?: number | null;
};

const PROVIDER_LABELS: Record<string, string> = {
  local: "Local",
  owned: "Owned worker",
  firecrawl: "Firecrawl",
  brightdata: "Bright Data",
  browserbase: "Browserbase",
};

const ROUTE_LABELS: Record<AcquisitionRoute, string> = {
  local_http: "Local HTTP",
  owned_proxy_http: "Owned proxy HTTP",
  local_browser: "Local browser",
  brightdata_unlocker: "Bright Data Unlocker",
  firecrawl_scrape: "Firecrawl Scrape",
  browserbase_session: "Browserbase Session",
  firecrawl_interact: "Firecrawl Interact",
};

const OUTCOME_LABELS: Record<string, string> = {
  succeeded: "succeeded",
  success: "succeeded",
  retryable_failure: "retryable",
  permanent_failure: "permanent failure",
  cancelled: "cancelled",
  lease_expired: "lease expired",
};

const BLOCK_LABELS: Record<string, string> = {
  challenge: "challenge",
  blocked_challenge: "challenge",
  rate_limited: "rate limited",
  provider_rate_limited: "rate limited",
  robots: "robots blocked",
  policy: "policy blocked",
  unsafe_request_url: "unsafe URL",
  unsafe_final_url: "unsafe URL",
};

const METER_LABELS: Record<string, Record<string, string>> = {
  firecrawl: { credits: "credit" },
  brightdata: { requests: "request" },
  browserbase: { browserMinutes: "browser minute", proxyBytes: "proxy byte" },
};

function formatNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 }).format(value);
}

export function formatProvider(provider: string): string {
  return PROVIDER_LABELS[provider] ?? "Unknown provider";
}

export function formatRoute(route: AcquisitionRoute): string {
  return ROUTE_LABELS[route];
}

export function formatNativeUsage(provider: string, meter: string, value: number): string {
  const unit = METER_LABELS[provider]?.[meter];
  if (!unit || !Number.isFinite(value) || value < 0) return "—";
  const quantity = formatNumber(value);
  return `${quantity} ${unit}${value === 1 ? "" : "s"}`;
}

export function formatDuration(durationMs?: number | null): string {
  if (durationMs === undefined || durationMs === null || !Number.isFinite(durationMs) || durationMs < 0) {
    return "—";
  }
  if (durationMs < 1000) return `${Math.round(durationMs)}ms`;
  return `${formatNumber(durationMs / 1000)}s`;
}

export function formatAttempt(attempt: AcquisitionAttempt): string {
  const outcome = OUTCOME_LABELS[attempt.outcome] ?? "unknown outcome";
  const block = attempt.blockReason ? BLOCK_LABELS[attempt.blockReason] ?? "blocked" : null;
  const parts = [formatProvider(attempt.provider), block, outcome, formatDuration(attempt.durationMs)];
  return parts.filter((part): part is string => Boolean(part && part !== "—")).join(" · ");
}
