const API_BASE = process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "";

export type Health = {
  status: string;
  service: string;
  version: string;
  db: "up" | "down" | "disabled";
};

export type Run = {
  id: number;
  externalId: string | null;
  jobId: number | null;
  trigger: string;
  status: string;
  engineUsed: string | null;
  pagesCount: number;
  errorMessage: string | null;
  rawOutputPath: string | null;
  startedAt: string;
  finishedAt: string | null;
  createdAt: string;
  pages?: PageRecord[];
};

export type PageRecord = {
  id: number;
  url: string;
  statusCode: number | null;
  engine: string | null;
  extractor: string | null;
  contentHash: string | null;
  rawJsonPath: string | null;
  rawMdPath: string | null;
  rawHtmlPath: string | null;
  metadata: Record<string, unknown> | null;
  createdAt: string;
};

export type Artifact = {
  kind: "scrape" | "crawl" | "research";
  stem: string;
  title: string;
  url: string;
  pages: number;
  bytes: number;
  mtime: number;
  json: string;
  md: string;
};

export type CorpusRecord = {
  id: string | null;
  url: string | null;
  title: string | null;
  namespace: string | null;
  framework: string | null;
  licenseBucket: string | null;
  qualityTier: string;
  chunkIndex: number | null;
  headingPath: string[];
  target: string;
  file: string;
  snippet: string;
};

export type CorpusStats = {
  total: number;
  byTarget: Record<string, number>;
  byNamespace: Record<string, number>;
  byBucket: Record<string, number>;
  byTier: Record<string, number>;
  namespaces: string[];
  buckets: string[];
  tiers: string[];
  frameworks: string[];
  targets: string[];
};

export type CrawlStatus =
  | "pending"
  | "queued"
  | "running"
  | "leased"
  | "retry_wait"
  | "waiting_input"
  | "completed"
  | "partial"
  | "failed"
  | "cancelled"
  | "timed_out"
  | "interrupted";

export type CrawlTaskState =
  | "pending"
  | "leased"
  | "retry_wait"
  | "waiting_input"
  | "succeeded"
  | "http_error"
  | "blocked_robots"
  | "extraction_failed"
  | "permanent_failed"
  | "cancelled";

export type AcquisitionRoute =
  | "local_http"
  | "owned_proxy_http"
  | "local_browser"
  | "brightdata_unlocker"
  | "firecrawl_scrape"
  | "browserbase_session"
  | "firecrawl_interact";

export type NativeUsage = Record<string, number>;

export type AcquisitionAttempt = {
  id: string;
  route: AcquisitionRoute;
  provider: string | null;
  outcome: string | null;
  blockReason: string | null;
  errorCode: string | null;
  durationMs: number | null;
  nativeUsage: NativeUsage;
  startedAt: string;
  finishedAt: string | null;
};

export type ProviderUsage = {
  provider: string;
  meter: string;
  limit: number;
  reserved: number;
  consumed: number;
};

export type Worker = {
  id: string;
  state: "active" | "offline" | "draining" | "revoked" | "unknown";
  capabilities: string[];
  lastSeenAt: string | null;
};

export type LiveSession = {
  id: string;
  backend: "browserbase" | "firecrawl" | "owned";
  state: "waiting" | "active" | "closed" | "expired";
  expiresAt: string;
};

export type CrawlPageMetadata = {
  url?: string;
  title?: string;
  engine?: string;
  extractor?: string;
  status_code?: number | null;
  downloaded_bytes?: number;
  artifact_bytes?: number;
};

export type CrawlTask = {
  discoverySeq?: number;
  originalUrl?: string;
  normalizedUrl?: string;
  finalUrl?: string | null;
  statusCode?: number | null;
  downloadedBytes?: number;
  artifactBytes?: number;
  createdAt?: string;
  discovery_seq?: number;
  original_url?: string;
  normalized_url?: string;
  state?: CrawlTaskState;
  final_url?: string | null;
  status_code?: number | null;
  title?: string | null;
  markdown?: string | null;
  html?: string | null;
  metadata?: CrawlPageMetadata | null;
  downloaded_bytes?: number;
  artifact_bytes?: number;
  created_at?: string;
  url?: string;
};

export type CrawlPage = CrawlTask;

export type CrawlDisplayPage = CrawlPage & {
  url: string;
  title: string;
  markdown: string;
  html: string;
  metadata: CrawlPageMetadata;
};

export type CrawlPagesResponse = {
  pages: CrawlPage[];
  nextAfter: number | null;
};

export type CrawlConfig = {
  url?: string;
  limit?: number;
  maxDepth?: number;
  timeoutSeconds?: number;
  onlyMainContent?: boolean;
  engine?: "auto" | "http" | "browser";
};

export type CrawlError = {
  url?: string;
  state?: CrawlTaskState;
  http_status?: number | null;
  error_class?: string | null;
  error_code?: string | null;
  error_message?: string | null;
  retry_after_at?: string | null;
  finished_at?: string | null;
};

export type CrawlJob = {
  id?: string;
  job_id?: string;
  jobId?: string;
  base_url?: string;
  seedUrl?: string;
  state?: CrawlStatus;
  status: CrawlStatus;
  progress?: number;
  resultCount?: number;
  discoveredCount?: number;
  terminalCount?: number;
  succeededCount?: number;
  failedCount?: number;
  blockedCount?: number;
  maxPages?: number;
  maxBytes?: number;
  maxArtifactBytes?: number;
  deadlineAt?: string;
  discovered_count?: number;
  terminal_count?: number;
  succeeded_count?: number;
  failed_count?: number;
  blocked_count?: number;
  max_pages?: number;
  deadline_at?: string;
  terminalReason?: string | null;
  config?: CrawlConfig;
  results?: CrawlPage[];
  errors?: Array<CrawlError | string>;
  attempts?: AcquisitionAttempt[];
  usage?: ProviderUsage[];
  providerUsage?: ProviderUsage[];
  workers?: Worker[];
  activeSession?: LiveSession | null;
  error?: string;
};

function stringValue(...values: unknown[]) {
  const value = values.find((candidate) => typeof candidate === "string" && candidate.length > 0);
  return typeof value === "string" ? value : "";
}

function crawlPageKey(page: CrawlPage) {
  if (typeof page.discovery_seq === "number" && Number.isFinite(page.discovery_seq)) {
    return `sequence:${page.discovery_seq}`;
  }
  return `url:${stringValue(page.final_url, page.original_url, page.normalized_url, page.url)}`;
}

export function crawlPageToResult(page: CrawlPage): CrawlDisplayPage {
  const metadata = page.metadata && typeof page.metadata === "object" ? page.metadata : {};
  return {
    ...page,
    url: stringValue(
      page.final_url,
      page.original_url,
      page.normalized_url,
      page.url,
      metadata.url,
    ),
    title: stringValue(page.title, metadata.title),
    markdown: typeof page.markdown === "string" ? page.markdown : "",
    html: typeof page.html === "string" ? page.html : "",
    metadata,
  };
}

export function mergeCrawlPages(
  existing: CrawlDisplayPage[],
  incoming: CrawlPage[],
): CrawlDisplayPage[] {
  const pages = new Map(existing.map((page) => [crawlPageKey(page), page]));
  for (const update of incoming) {
    const key = crawlPageKey(update);
    const previous = pages.get(key);
    const combined: CrawlPage = {
      ...previous,
      ...update,
      metadata: {
        ...(previous?.metadata ?? {}),
        ...(update.metadata ?? {}),
      },
    };
    if (previous && (update.markdown === undefined || update.markdown === null)) {
      combined.markdown = previous.markdown;
    }
    if (previous && (update.html === undefined || update.html === null)) {
      combined.html = previous.html;
    }
    pages.set(key, crawlPageToResult(combined));
  }
  return [...pages.values()].sort((left, right) => {
    const leftSequence = typeof left.discovery_seq === "number" ? left.discovery_seq : Number.MAX_SAFE_INTEGER;
    const rightSequence = typeof right.discovery_seq === "number" ? right.discovery_seq : Number.MAX_SAFE_INTEGER;
    return leftSequence - rightSequence || left.url.localeCompare(right.url);
  });
}

export function crawlMarkdown(pages: CrawlDisplayPage[]) {
  return pages
    .filter((page) => page.state === "succeeded" || page.markdown.length > 0)
    .map((page) => {
      const heading = page.title || page.url || "Captured page";
      return `# ${heading}\n\n${page.markdown}`.trimEnd();
    })
    .join("\n\n---\n\n");
}

export type ScrapeResult = {
  success: boolean;
  url: string;
  title?: string;
  markdown: string;
  html: string;
  metadata: Record<string, unknown>;
};

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // Keep the status-based fallback for non-JSON failures.
    }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export function formatDate(value?: string | number | null) {
  if (value === undefined || value === null || value === "") return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.valueOf())
    ? "—"
    : new Intl.DateTimeFormat(undefined, {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      }).format(date);
}

export function formatBytes(value: number) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / 1024 / 1024).toFixed(1)} MB`;
}
