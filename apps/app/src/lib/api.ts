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

export type CrawlJob = {
  job_id?: string;
  jobId?: string;
  base_url?: string;
  status: string;
  progress?: number;
  results?: Array<Record<string, unknown>>;
  errors?: Array<Record<string, unknown> | string>;
  error?: string;
  [key: string]: unknown;
};

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
