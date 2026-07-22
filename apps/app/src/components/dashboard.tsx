"use client";

import * as React from "react";
import { Download, FileJson, Filter, Globe2, LoaderCircle, RefreshCw, RotateCcw, Search, Sparkles, Waypoints } from "lucide-react";
import { toast } from "sonner";

import { AppShell, PageHeader, type ViewId } from "@/components/app-shell";
import { AcquisitionInspector } from "@/components/acquisition-inspector";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Toaster } from "@/components/ui/sonner";
import {
  CodeViewer,
  DataTable,
  EmptyState,
  ErrorState,
  InspectorHeader,
  InspectorPanel,
  KeyValueList,
  ProgressTimeline,
  SkeletonState,
  StatusBadge,
  type DataColumn,
  type TimelineStep,
} from "@/components/system";
import {
  ApiError,
  api,
  crawlMarkdown,
  formatBytes,
  formatDate,
  mergeCrawlPages,
  type Artifact,
  type CorpusRecord,
  type CorpusStats,
  type CrawlDisplayPage,
  type CrawlJob,
  type CrawlPagesResponse,
  type Health,
  type Run,
  type ScrapeResult,
} from "@/lib/api";
import { useLatestRequest } from "@/hooks/use-latest-request";
import { isAbortError, isTerminalCrawlStatus, nextCrawlPageCursor, nextCrawlPageDrainCursor, normalizePageLimit } from "@/lib/request-safety";
import { cn } from "@/lib/utils";

type LoadState = "idle" | "loading" | "ready" | "error";
const ACTIVE_CRAWL_KEY = "crawltrove.active-crawl";

function readActiveCrawl() {
  try {
    return window.sessionStorage.getItem(ACTIVE_CRAWL_KEY);
  } catch {
    return null;
  }
}

function rememberActiveCrawl(jobId: string) {
  try {
    window.sessionStorage.setItem(ACTIVE_CRAWL_KEY, jobId);
  } catch {
    // Storage is a recovery aid; a queued backend job remains valid without it.
  }
}

function forgetActiveCrawl() {
  try {
    window.sessionStorage.removeItem(ACTIVE_CRAWL_KEY);
  } catch {
    // Ignore storage restrictions while keeping the live UI state accurate.
  }
}

function messageOf(error: unknown) {
  return error instanceof Error ? error.message : "An unexpected error occurred.";
}

export function Dashboard() {
  const [view, setView] = React.useState<ViewId>("runs");
  const [health, setHealth] = React.useState<Health | null>(null);
  const [commandOpen, setCommandOpen] = React.useState(false);

  React.useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setCommandOpen((open) => !open);
      }
    };
    window.addEventListener("keydown", keydown);
    return () => window.removeEventListener("keydown", keydown);
  }, []);

  React.useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;

    const loadHealth = async () => {
      controller = new AbortController();
      try {
        const value = await api<Health>("/api/health", {
          signal: controller.signal,
        });
        if (!stopped) setHealth(value);
      } catch (healthError) {
        if (!stopped && !isAbortError(healthError)) setHealth(null);
      } finally {
        if (!stopped) timer = window.setTimeout(loadHealth, 30_000);
      }
    };

    void loadHealth();
    return () => {
      stopped = true;
      controller?.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  return (
    <AppShell activeView={view} onViewChange={setView} health={health} commandOpen={commandOpen} onCommandOpenChange={setCommandOpen}>
      <div className={cn("h-full", view !== "crawl" && "hidden")}>
        <CrawlWorkspace active={view === "crawl"} />
      </div>
      {view === "runs" && <RunsWorkspace onStartCrawl={() => setView("crawl")} />}
      {view === "documents" && <DocumentsWorkspace onStartCrawl={() => setView("crawl")} />}
      {view === "corpus" && <CorpusWorkspace />}
      <Toaster position="bottom-right" richColors closeButton />
    </AppShell>
  );
}

function CrawlWorkspace({ active }: { active: boolean }) {
  const [mode, setMode] = React.useState<"crawl" | "scrape">("crawl");
  const [url, setUrl] = React.useState("");
  const [engine, setEngine] = React.useState("auto");
  const [limit, setLimit] = React.useState(25);
  const [busy, setBusy] = React.useState(false);
  const [job, setJob] = React.useState<CrawlJob | null>(null);
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [crawlPages, setCrawlPages] = React.useState<CrawlDisplayPage[]>([]);
  const [scrape, setScrape] = React.useState<ScrapeResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [inspectorDismissed, setInspectorDismissed] = React.useState(false);
  const [pollRevision, setPollRevision] = React.useState(0);
  const [startSubmitRequest] = useLatestRequest();
  const crawlPagesRef = React.useRef<CrawlDisplayPage[]>([]);
  const pageCursorRef = React.useRef<number | null>(null);

  const resetCrawlPages = React.useCallback(() => {
    crawlPagesRef.current = [];
    pageCursorRef.current = null;
    setCrawlPages([]);
  }, []);

  React.useEffect(() => {
    const activeJobId = readActiveCrawl();
    if (!activeJobId) return;
    resetCrawlPages();
    setJob({ status: "queued", progress: 0, jobId: activeJobId });
    setJobId(activeJobId);
    setBusy(true);
  }, [resetCrawlPages]);

  React.useEffect(() => {
    if (!jobId) return;
    let stopped = false;
    let timer: number | undefined;
    let controller: AbortController | undefined;

    const poll = async () => {
      let continuePolling = true;
      controller = new AbortController();
      try {
        const encodedJobId = encodeURIComponent(jobId);
        const next = await api<CrawlJob>(`/api/crawl/${encodedJobId}`, {
          signal: controller.signal,
        });
        const requestedAfter = pageCursorRef.current;
        const fetchPageBatch = (after: number | null) => {
          const query = after === null ? "?limit=100" : `?after=${after}&limit=100`;
          return api<CrawlPagesResponse>(
            `/api/crawl/${encodedJobId}/pages${query}`,
            { signal: controller?.signal },
          );
        };
        const pageBatch = await fetchPageBatch(requestedAfter);
        if (stopped) return;

        const batchPages = Array.isArray(pageBatch.pages) ? pageBatch.pages : [];
        const fallbackInlinePages = crawlPagesRef.current.length === 0 && batchPages.length === 0
          ? (next.results ?? [])
          : [];
        let mergedPages = mergeCrawlPages(
          crawlPagesRef.current,
          batchPages.length ? batchPages : fallbackInlinePages,
        );

        const expectedResults = typeof next.resultCount === "number" ? next.resultCount : null;
        if (isTerminalCrawlStatus(next.status)) {
          let drainAfter: number | null = null;
          let drainBatch = requestedAfter === null ? pageBatch : await fetchPageBatch(null);
          let drainedPages: CrawlDisplayPage[] = [];
          for (let batchIndex = 0; batchIndex < 100; batchIndex += 1) {
            if (stopped) return;
            const rows = Array.isArray(drainBatch.pages) ? drainBatch.pages : [];
            drainedPages = mergeCrawlPages(drainedPages, rows);
            const nextAfter = nextCrawlPageDrainCursor({
              requestedAfter: drainAfter,
              nextAfter: drainBatch.nextAfter,
              batchSize: rows.length,
              capturedResults: drainedPages.filter(isCapturedCrawlPage).length,
              expectedResults,
            });
            if (nextAfter === null) break;
            drainAfter = nextAfter;
            drainBatch = await fetchPageBatch(drainAfter);
          }
          mergedPages = mergeCrawlPages(mergedPages, drainedPages);
          pageCursorRef.current = nextCrawlPageCursor(mergedPages, drainBatch.nextAfter);
        } else {
          pageCursorRef.current = nextCrawlPageCursor(
            mergedPages,
            pageBatch.nextAfter,
          );
        }
        crawlPagesRef.current = mergedPages;
        setCrawlPages(mergedPages);
        setJob(next);
        if (isTerminalCrawlStatus(next.status)) {
          continuePolling = false;
          forgetActiveCrawl();
          setBusy(false);
          setJobId(null);
          if (next.status === "completed") toast.success("Crawl completed");
          else if (next.status === "partial" || next.status === "interrupted") toast.warning(`Crawl ${next.status}`);
          else toast.error(`Crawl ${next.status}`);
        }
      } catch (pollError) {
        if (isAbortError(pollError)) return;
        continuePolling = false;
        if (!stopped) {
          if (pollError instanceof ApiError && pollError.status === 404) {
            forgetActiveCrawl();
            setBusy(false);
            setJobId(null);
          } else {
            setBusy(true);
          }
          setError(messageOf(pollError));
          toast.error("Crawl status unavailable");
        }
      } finally {
        if (!stopped && continuePolling) {
          timer = window.setTimeout(poll, 1_500);
        }
      }
    };

    void poll();
    return () => {
      stopped = true;
      controller?.abort();
      if (timer) window.clearTimeout(timer);
    };
  }, [jobId, pollRevision]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const targetUrl = url.trim();
    if (!targetUrl) {
      setError("Enter a target URL before starting.");
      toast.error("Target URL is required");
      return;
    }

    const controller = startSubmitRequest();
    const pageLimit = normalizePageLimit(limit);
    setError(null);
    setScrape(null);
    setJob(null);
    resetCrawlPages();
    setInspectorDismissed(false);
    setBusy(true);
    if (mode === "crawl") setLimit(pageLimit);
    try {
      if (mode === "scrape") {
        const result = await api<ScrapeResult>("/api/scrape", {
          method: "POST",
          body: JSON.stringify({ url: targetUrl, engine, onlyMainContent: true, waitForMs: 1000 }),
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setScrape(result);
        setBusy(false);
        toast.success("Page scraped");
      } else {
        const result = await api<{ jobId: string }>("/api/crawl", {
          method: "POST",
          body: JSON.stringify({ url: targetUrl, engine, limit: pageLimit, maxDepth: 2, onlyMainContent: true, useSitemap: true, screenshots: false }),
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        rememberActiveCrawl(result.jobId);
        setJob({ status: "queued", progress: 0, jobId: result.jobId });
        setJobId(result.jobId);
        toast.success("Crawl queued");
      }
    } catch (submitError) {
      if (isAbortError(submitError)) return;
      setBusy(false);
      setError(messageOf(submitError));
      toast.error(mode === "crawl" ? "Crawl could not start" : "Scrape failed");
    }
  };

  const capturedPages = crawlPages.filter(isCapturedCrawlPage);
  const progress = isTerminalCrawlStatus(job?.status ?? "")
    ? 1
    : normalizeProgress(job?.progress ?? crawlCounterProgress(job));
  const timeline = crawlTimeline(job?.status ?? "queued", progress);
  const inspectorOpen = Boolean(job || scrape || error) && !inspectorDismissed;
  const configuredUrl = typeof job?.config?.url === "string" ? job.config.url : "";
  const title = scrape?.title || scrape?.url || job?.base_url || configuredUrl || url || "Current operation";
  const markdown = crawlMarkdown(capturedPages);
  const retryPolling = () => {
    setError(null);
    setBusy(true);
    setPollRevision((revision) => revision + 1);
  };

  return (
    <Workspace>
      <PageHeader section="Acquire" title="Crawl" description="Capture one page or follow a bounded site frontier." actions={<>{inspectorDismissed && (job || scrape || error) ? <Button variant="outline" size="sm" onClick={() => setInspectorDismissed(false)}>View result</Button> : null}<StatusBadge status={error && jobId ? "unknown" : busy ? "running" : "ready"} tone={error && jobId ? "warning" : busy ? "info" : "neutral"} /></>} />
      <InspectorPanel
        open={active && inspectorOpen}
        onClose={() => setInspectorDismissed(true)}
        inspector={
          <InspectorContent>
            <InspectorHeader eyebrow={scrape ? "Scrape result" : job ? "Crawl run" : "Request error"} title={title} onClose={() => setInspectorDismissed(true)} />
            {error ? <ErrorState title="Request failed" description={error} onRetry={jobId ? retryPolling : undefined} /> : scrape ? (
              <>
                <KeyValueList items={[
                  { label: "URL", value: scrape.url },
                  { label: "Title", value: scrape.title ?? "—" },
                  { label: "Engine", value: String(scrape.metadata.engine ?? engine) },
                  { label: "Extractor", value: String(scrape.metadata.extractor ?? "—") },
                  { label: "Language", value: String(scrape.metadata.language ?? "—") },
                ]} />
                <CodeViewer className="flex-1" sources={{ markdown: scrape.markdown, html: scrape.html, json: JSON.stringify(scrape.metadata, null, 2) }} />
              </>
            ) : job ? (
              <div className="flex min-h-0 flex-1 flex-col">
                <KeyValueList items={[
                  { label: "Job", value: String(job.id ?? job.job_id ?? job.jobId ?? jobId ?? "—") },
                  { label: "Status", value: <StatusBadge status={job.status} /> },
                  { label: "Progress", value: `${Math.round(progress * 100)}%` },
                  { label: "Pages", value: Math.max(capturedPages.length, job.resultCount ?? 0) },
                  { label: "Errors", value: job.errors?.length ?? 0 },
                ]} />
                <div className="p-4"><ProgressTimeline steps={timeline} /></div>
                <AcquisitionInspector
                  job={job}
                  jobId={String(job.id ?? job.job_id ?? job.jobId ?? jobId ?? "")}
                  onRefresh={retryPolling}
                />
                <CodeViewer key={markdown ? "crawl-content" : "crawl-status"} className="flex-1" sources={{ markdown, events: crawlEvents(job, capturedPages), json: JSON.stringify({ ...job, pages: crawlPages }, null, 2) }} />
              </div>
            ) : null}
          </InspectorContent>
        }
      >
        <div className="h-full overflow-auto p-4 sm:p-5">
          <div className="mx-auto max-w-3xl">
            <div className="mb-5 flex items-start justify-between gap-6">
              <div>
                <p className="font-mono text-[10px] uppercase tracking-[0.12em] text-zinc-700">New acquisition</p>
                <h2 className="mt-1 text-lg font-medium tracking-[-0.025em] text-zinc-100">Turn a URL into durable source material.</h2>
                <p className="mt-1.5 max-w-xl text-xs leading-5 text-zinc-600">HTTP first, browser fallback when the page needs rendering. The backend keeps its existing safety, persistence, and extraction path.</p>
              </div>
              <div className="hidden size-10 shrink-0 items-center justify-center rounded-[8px] border border-indigo-400/15 bg-indigo-400/[0.06] text-indigo-300 sm:flex"><Globe2 className="size-4" /></div>
            </div>

            <form onSubmit={submit} className="overflow-hidden rounded-[8px] border border-white/[0.08] bg-[#0d0e11]">
              <div className="flex items-center justify-between border-b border-white/[0.08] px-3 py-2">
                <Tabs value={mode} onValueChange={(value) => setMode(value as "crawl" | "scrape")}>
                  <TabsList className="h-8 rounded-[6px] bg-white/[0.04] p-0.5">
                    <TabsTrigger value="crawl" className="h-7 rounded-[5px] px-3 text-xs"><Waypoints />Crawl site</TabsTrigger>
                    <TabsTrigger value="scrape" className="h-7 rounded-[5px] px-3 text-xs"><Sparkles />Scrape page</TabsTrigger>
                  </TabsList>
                </Tabs>
                <span className="hidden font-mono text-[9px] uppercase tracking-[0.12em] text-zinc-700 sm:inline">Public URLs only</span>
              </div>
              <div className="space-y-5 p-4 sm:p-5">
                <div className="space-y-2">
                  <Label htmlFor="crawl-url" className="text-xs text-zinc-400">Target URL</Label>
                  <Input id="crawl-url" type="url" required value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://docs.example.com" className="h-9 border-white/10 bg-[#090a0c] font-mono text-xs placeholder:text-zinc-800 focus-visible:border-indigo-400/50" disabled={busy} />
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="fetch-engine" className="text-xs text-zinc-400">Fetch engine</Label>
                    <Select value={engine} onValueChange={setEngine} disabled={busy}>
                      <SelectTrigger id="fetch-engine" className="h-8 w-full border-white/10 bg-[#090a0c] text-xs"><SelectValue /></SelectTrigger>
                      <SelectContent><SelectItem value="auto">Auto · HTTP then browser</SelectItem><SelectItem value="http">HTTP only</SelectItem><SelectItem value="browser">Browser only</SelectItem></SelectContent>
                    </Select>
                  </div>
                  <div className={cn("space-y-2", mode === "scrape" && "opacity-40")}>
                    <Label htmlFor="page-limit" className="text-xs text-zinc-400">Page limit</Label>
                    <Input id="page-limit" type="number" min={1} max={100} value={limit} onChange={(event) => setLimit(Number(event.target.value))} className="h-8 border-white/10 bg-[#090a0c] font-mono text-xs" disabled={busy || mode === "scrape"} />
                  </div>
                </div>
              </div>
              <div className="flex items-center justify-between border-t border-white/[0.08] bg-white/[0.015] px-4 py-3 sm:px-5">
                <span className="text-[11px] text-zinc-700">Sitemap discovery · max depth 2 · main content</span>
                <Button type="submit" disabled={busy || !url.trim()} className="h-9 min-w-28 bg-zinc-100 text-zinc-950 hover:bg-white active:bg-zinc-300">
                  {busy ? <LoaderCircle className="animate-spin" /> : mode === "crawl" ? <Waypoints /> : <Sparkles />}
                  {busy ? "Working…" : mode === "crawl" ? "Start crawl" : "Scrape page"}
                </Button>
              </div>
            </form>

            {!inspectorOpen && (
              <div className="mt-5 grid gap-px overflow-hidden rounded-[8px] border border-white/[0.08] bg-white/[0.08] sm:grid-cols-3">
                {[{ label: "01 · Discover", text: "Read sitemap and same-site links." }, { label: "02 · Capture", text: "Fetch cheaply, render only as needed." }, { label: "03 · Persist", text: "Store Markdown, metadata, and provenance." }].map((item) => (
                  <div key={item.label} className="bg-[#0d0e11] p-3.5"><p className="font-mono text-[9px] uppercase tracking-[0.1em] text-indigo-300/70">{item.label}</p><p className="mt-1.5 text-xs leading-5 text-zinc-600">{item.text}</p></div>
                ))}
              </div>
            )}
          </div>
        </div>
      </InspectorPanel>
    </Workspace>
  );
}

function RunsWorkspace({ onStartCrawl }: { onStartCrawl: () => void }) {
  const [runs, setRuns] = React.useState<Run[]>([]);
  const [state, setState] = React.useState<LoadState>("loading");
  const [error, setError] = React.useState("");
  const [selected, setSelected] = React.useState<Run | null>(null);
  const [query, setQuery] = React.useState("");
  const [status, setStatus] = React.useState("all");
  const [startLoadRequest] = useLatestRequest();
  const [startDetailRequest, cancelDetailRequest] = useLatestRequest();

  const load = React.useCallback(async () => {
    const controller = startLoadRequest();
    setState("loading");
    setError("");
    try {
      const params = status === "all" ? "" : `?status=${encodeURIComponent(status)}`;
      const response = await api<{ runs: Run[] }>(`/api/runs${params}`, {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setRuns(response.runs);
      setState("ready");
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setError(messageOf(loadError));
      setState("error");
    }
  }, [startLoadRequest, status]);

  React.useEffect(() => { void load(); }, [load]);

  const select = async (run: Run) => {
    const controller = startDetailRequest();
    setSelected(run);
    try {
      const detail = await api<Run>(`/api/runs/${run.id}`, {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) setSelected(detail);
    } catch (detailError) {
      if (isAbortError(detailError)) return;
      toast.error(messageOf(detailError));
    }
  };
  const closeInspector = () => {
    cancelDetailRequest();
    setSelected(null);
  };
  const filtered = runs.filter((run) => [run.externalId, run.status, run.engineUsed, run.id].some((value) => String(value ?? "").toLowerCase().includes(query.toLowerCase())));
  const columns: DataColumn<Run>[] = [
    { id: "run", header: "Run", cell: (run) => <div><p className="font-mono text-zinc-300">#{run.id}</p><p className="mt-0.5 truncate text-[10px] text-zinc-700">{run.externalId ?? "ad-hoc"}</p></div> },
    { id: "status", header: "Status", cell: (run) => <StatusBadge status={run.status} /> },
    { id: "trigger", header: "Trigger", cell: (run) => <span className="text-zinc-500">{run.trigger}</span> },
    { id: "engine", header: "Engine", cell: (run) => <span className="font-mono text-zinc-500">{run.engineUsed ?? "—"}</span>, className: "hidden md:table-cell" },
    { id: "pages", header: "Pages", cell: (run) => <span className="font-mono">{run.pagesCount ?? 0}</span> },
    { id: "started", header: "Started", cell: (run) => <span className="font-mono text-[10px] text-zinc-600">{formatDate(run.startedAt)}</span>, className: "hidden lg:table-cell" },
  ];

  return (
    <Workspace>
      <PageHeader section="Observe" title="Runs" description="Inspect persisted scrape and crawl executions." actions={<><Button variant="outline" size="sm" onClick={() => void load()} disabled={state === "loading"}><RefreshCw className={cn(state === "loading" && "animate-spin")} />Refresh</Button><Button size="sm" className="bg-zinc-100 text-zinc-950 hover:bg-white" onClick={onStartCrawl}><Waypoints />New crawl</Button></>} />
      <InspectorPanel open={Boolean(selected)} onClose={closeInspector} inspector={selected ? <RunInspector run={selected} onClose={closeInspector} /> : null}>
        <CollectionContent>
          <FilterBar>
            <div className="relative min-w-0 flex-1 sm:max-w-xs"><Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-zinc-700" /><Input aria-label="Filter runs" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter runs…" className="h-8 border-white/10 bg-[#0d0e11] pl-8 text-xs" /></div>
            <Select value={status} onValueChange={setStatus}><SelectTrigger aria-label="Filter by run status" className="h-8 w-32 border-white/10 bg-[#0d0e11] text-xs"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All statuses</SelectItem><SelectItem value="completed">Completed</SelectItem><SelectItem value="running">Running</SelectItem><SelectItem value="failed">Failed</SelectItem><SelectItem value="interrupted">Interrupted</SelectItem></SelectContent></Select>
            <span className="ml-auto font-mono text-[10px] text-zinc-700">{filtered.length} runs</span>
          </FilterBar>
          <div className="p-3 sm:p-4">{state === "loading" ? <SkeletonState /> : state === "error" ? <ErrorState description={error} onRetry={() => void load()} /> : <DataTable columns={columns} data={filtered} getRowId={(run) => run.id} selectedId={selected?.id} onRowSelect={(run) => void select(run)} empty={<EmptyState title="No runs found" description={query || status !== "all" ? "Change the filters to see more runs." : "Start a crawl to create the first persisted run."} action={<Button size="sm" onClick={onStartCrawl}><Waypoints />Start crawl</Button>} />} />}</div>
        </CollectionContent>
      </InspectorPanel>
    </Workspace>
  );
}

function RunInspector({ run, onClose }: { run: Run; onClose: () => void }) {
  const events = [
    `${formatDate(run.startedAt)}  run accepted · ${run.trigger}`,
    run.engineUsed ? `${formatDate(run.startedAt)}  engine selected · ${run.engineUsed}` : null,
    run.finishedAt ? `${formatDate(run.finishedAt)}  ${run.status} · ${run.pagesCount} page${run.pagesCount === 1 ? "" : "s"}` : "waiting  run in progress",
    run.errorMessage ? `error    ${run.errorMessage}` : null,
  ].filter(Boolean).join("\n");
  return (
    <InspectorContent>
      <InspectorHeader eyebrow="Persisted run" title={`Run #${run.id}`} onClose={onClose} />
      <KeyValueList items={[
        { label: "Status", value: <StatusBadge status={run.status} /> },
        { label: "External ID", value: run.externalId ?? "—" },
        { label: "Job", value: run.jobId ? `#${run.jobId}` : "Ad-hoc" },
        { label: "Trigger", value: run.trigger },
        { label: "Engine", value: run.engineUsed ?? "—" },
        { label: "Pages", value: run.pagesCount },
        { label: "Started", value: formatDate(run.startedAt) },
        { label: "Finished", value: formatDate(run.finishedAt) },
      ]} />
      {run.pages?.length ? <div className="border-b border-white/[0.08] px-4 py-3"><p className="mb-2 font-mono text-[9px] uppercase tracking-[0.12em] text-zinc-700">Captured pages</p>{run.pages.slice(0, 5).map((page) => <a key={page.id} href={page.rawMdPath ? `/${page.rawMdPath}` : page.url} className="block truncate py-1 text-xs text-zinc-500 hover:text-zinc-200" target="_blank" rel="noreferrer">{page.url}</a>)}</div> : null}
      <CodeViewer className="flex-1" sources={{ events, json: JSON.stringify(run, null, 2) }} />
    </InspectorContent>
  );
}

function DocumentsWorkspace({ onStartCrawl }: { onStartCrawl: () => void }) {
  const [items, setItems] = React.useState<Artifact[]>([]);
  const [state, setState] = React.useState<LoadState>("loading");
  const [error, setError] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [kind, setKind] = React.useState("all");
  const [selected, setSelected] = React.useState<Artifact | null>(null);
  const [payload, setPayload] = React.useState<{ markdown?: string; json?: string }>({});
  const [payloadState, setPayloadState] = React.useState<LoadState>("idle");
  const [payloadError, setPayloadError] = React.useState("");
  const [startLoadRequest] = useLatestRequest();
  const [startPayloadRequest, cancelPayloadRequest] = useLatestRequest();

  const load = React.useCallback(async () => {
    const controller = startLoadRequest();
    setState("loading");
    setError("");
    try {
      const response = await api<{ artifacts: Artifact[] }>("/api/artifacts", {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      setItems(response.artifacts);
      setState("ready");
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setError(messageOf(loadError));
      setState("error");
    }
  }, [startLoadRequest]);
  React.useEffect(() => { void load(); }, [load]);

  const loadPayload = React.useCallback(async (artifact: Artifact) => {
    const controller = startPayloadRequest();
    setPayloadState("loading");
    setPayloadError("");
    setPayload({});

    const read = async (path: string, label: string) => {
      const response = await fetch(path, { signal: controller.signal });
      if (!response.ok) {
        throw new Error(`Could not load ${label} (${response.status}).`);
      }
      return response.text();
    };

    try {
      const [markdown, json] = await Promise.all([
        read(artifact.md, "Markdown"),
        read(artifact.json, "JSON"),
      ]);
      if (controller.signal.aborted) return;
      setPayload({ markdown, json: prettyJson(json) });
      setPayloadState("ready");
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setPayloadError(messageOf(loadError));
      setPayloadState("error");
    }
  }, [startPayloadRequest]);

  const select = (artifact: Artifact) => {
    setSelected(artifact);
    void loadPayload(artifact);
  };
  const closeInspector = () => {
    cancelPayloadRequest();
    setSelected(null);
    setPayload({});
    setPayloadState("idle");
    setPayloadError("");
  };
  const filtered = items.filter((item) => (kind === "all" || item.kind === kind) && `${item.title} ${item.url} ${item.stem}`.toLowerCase().includes(query.toLowerCase()));
  const columns: DataColumn<Artifact>[] = [
    { id: "document", header: "Document", cell: (item) => <div className="min-w-0"><p className="truncate text-zinc-200">{item.title || item.url || item.stem}</p><p className="mt-0.5 truncate font-mono text-[10px] text-zinc-700">{item.stem}</p></div> },
    { id: "kind", header: "Kind", cell: (item) => <StatusBadge status={item.kind} tone={item.kind === "research" ? "info" : "neutral"} /> },
    { id: "pages", header: "Pages", cell: (item) => <span className="font-mono">{item.pages}</span> },
    { id: "size", header: "Size", cell: (item) => <span className="font-mono text-[10px] text-zinc-600">{formatBytes(item.bytes)}</span>, className: "hidden md:table-cell" },
    { id: "updated", header: "Captured", cell: (item) => <span className="font-mono text-[10px] text-zinc-600">{formatDate(item.mtime)}</span>, className: "hidden lg:table-cell" },
  ];

  return (
    <Workspace>
      <PageHeader section="Library" title="Documents" description="Read saved Markdown and source artifacts." actions={<><Button variant="outline" size="sm" onClick={() => void load()} disabled={state === "loading"}><RefreshCw className={cn(state === "loading" && "animate-spin")} />Refresh</Button><Button size="sm" className="bg-zinc-100 text-zinc-950 hover:bg-white" onClick={onStartCrawl}><Waypoints />Acquire</Button></>} />
      <InspectorPanel
        open={Boolean(selected)}
        onClose={closeInspector}
        inspector={selected ? (
          <InspectorContent>
            <InspectorHeader eyebrow={`${selected.kind} artifact`} title={selected.title || selected.stem} onClose={closeInspector} />
            <KeyValueList items={[{ label: "Source", value: selected.url }, { label: "Pages", value: selected.pages }, { label: "Size", value: formatBytes(selected.bytes) }, { label: "Captured", value: formatDate(selected.mtime) }]} />
            <div className="flex gap-2 border-b border-white/[0.08] p-3"><Button asChild variant="outline" size="sm"><a href={selected.md} target="_blank" rel="noreferrer"><Download />Markdown</a></Button><Button asChild variant="outline" size="sm"><a href={selected.json} target="_blank" rel="noreferrer"><FileJson />JSON</a></Button></div>
            {payloadState === "loading" ? <SkeletonState /> : payloadState === "error" ? <ErrorState title="Couldn’t load this document" description={payloadError} onRetry={() => void loadPayload(selected)} /> : payloadState === "ready" ? <CodeViewer className="flex-1" sources={{ markdown: payload.markdown, json: payload.json }} /> : null}
          </InspectorContent>
        ) : null}
      >
        <CollectionContent>
          <FilterBar><div className="relative min-w-0 flex-1 sm:max-w-xs"><Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-zinc-700" /><Input aria-label="Filter documents" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Filter documents…" className="h-8 border-white/10 bg-[#0d0e11] pl-8 text-xs" /></div><Select value={kind} onValueChange={setKind}><SelectTrigger aria-label="Filter by document kind" className="h-8 w-32 border-white/10 bg-[#0d0e11] text-xs"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All kinds</SelectItem><SelectItem value="scrape">Scrapes</SelectItem><SelectItem value="crawl">Crawls</SelectItem><SelectItem value="research">Research</SelectItem></SelectContent></Select><span className="ml-auto font-mono text-[10px] text-zinc-700">{filtered.length} files</span></FilterBar>
          <div className="p-3 sm:p-4">{state === "loading" ? <SkeletonState /> : state === "error" ? <ErrorState description={error} onRetry={() => void load()} /> : <DataTable columns={columns} data={filtered} getRowId={(item) => item.stem} selectedId={selected?.stem} onRowSelect={select} empty={<EmptyState title="No documents found" description={query || kind !== "all" ? "Change the filters to see more documents." : "Scrapes, crawls, and research reports will appear here."} action={<Button size="sm" onClick={onStartCrawl}>Acquire source</Button>} />} />}</div>
        </CollectionContent>
      </InspectorPanel>
    </Workspace>
  );
}

function CorpusWorkspace() {
  const [items, setItems] = React.useState<CorpusRecord[]>([]);
  const [stats, setStats] = React.useState<CorpusStats | null>(null);
  const [state, setState] = React.useState<LoadState>("loading");
  const [error, setError] = React.useState("");
  const [query, setQuery] = React.useState("");
  const [submittedQuery, setSubmittedQuery] = React.useState("");
  const [target, setTarget] = React.useState("all");
  const [selected, setSelected] = React.useState<CorpusRecord | null>(null);
  const [startLoadRequest] = useLatestRequest();

  const load = React.useCallback(async () => {
    const controller = startLoadRequest();
    setState("loading");
    setError("");
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (submittedQuery) params.set("q", submittedQuery);
      if (target !== "all") params.set("target", target);
      const [records, summary] = await Promise.all([
        api<{ items: CorpusRecord[] }>(`/api/corpus?${params}`, { signal: controller.signal }),
        api<{ stats: CorpusStats }>("/api/corpus/stats", { signal: controller.signal }),
      ]);
      if (controller.signal.aborted) return;
      setItems(records.items);
      setStats(summary.stats);
      setState("ready");
    } catch (loadError) {
      if (isAbortError(loadError)) return;
      setError(messageOf(loadError));
      setState("error");
    }
  }, [startLoadRequest, submittedQuery, target]);
  React.useEffect(() => { void load(); }, [load]);

  const columns: DataColumn<CorpusRecord>[] = [
    { id: "record", header: "Record", cell: (item) => <div className="min-w-0"><p className="truncate text-zinc-200">{item.title || item.headingPath.at(-1) || item.url || "Untitled record"}</p><p className="mt-0.5 truncate font-mono text-[10px] text-zinc-700">{item.namespace ?? "default"} · chunk {item.chunkIndex ?? "—"}</p></div> },
    { id: "target", header: "Target", cell: (item) => <StatusBadge status={item.target} tone={item.target === "rag" ? "info" : "neutral"} /> },
    { id: "tier", header: "Quality", cell: (item) => <span className="font-mono text-[10px] text-zinc-500">{item.qualityTier || "untiered"}</span> },
    { id: "license", header: "License", cell: (item) => <span className="font-mono text-[10px] text-zinc-500">{item.licenseBucket ?? "unknown"}</span>, className: "hidden md:table-cell" },
    { id: "framework", header: "Framework", cell: (item) => <span className="text-zinc-500">{item.framework ?? "—"}</span>, className: "hidden lg:table-cell" },
  ];
  const targetOptions = stats?.targets?.length ? stats.targets : ["rag", "sft", "dapt"];

  return (
    <Workspace>
      <PageHeader section="Prepare" title="Corpus" description="Inspect corpus-ready records, provenance, and quality signals." actions={<Button variant="outline" size="sm" onClick={() => void load()} disabled={state === "loading"}><RefreshCw className={cn(state === "loading" && "animate-spin")} />Refresh</Button>} />
      <InspectorPanel open={Boolean(selected)} onClose={() => setSelected(null)} inspector={selected ? <InspectorContent><InspectorHeader eyebrow={`${selected.target} corpus record`} title={selected.title || selected.id || "Record"} onClose={() => setSelected(null)} /><KeyValueList items={[{ label: "ID", value: selected.id ?? "—" }, { label: "Namespace", value: selected.namespace ?? "—" }, { label: "Framework", value: selected.framework ?? "—" }, { label: "Target", value: <StatusBadge status={selected.target} tone="info" /> }, { label: "Quality", value: selected.qualityTier || "untiered" }, { label: "License", value: selected.licenseBucket ?? "unknown" }, { label: "Chunk", value: selected.chunkIndex ?? "—" }, { label: "Source", value: selected.url ? <a href={selected.url} target="_blank" rel="noreferrer" className="text-indigo-300 hover:underline">Open source</a> : "—" }]} /><CodeViewer className="flex-1" sources={{ markdown: selected.snippet, json: JSON.stringify(selected, null, 2) }} /></InspectorContent> : null}>
        <CollectionContent>
          {stats && <div className="grid shrink-0 grid-cols-2 gap-px border-b border-white/[0.075] bg-white/[0.075] sm:grid-cols-4">{[
            { label: "Total records", value: stats.total },
            { label: "Namespaces", value: stats.namespaces.length },
            { label: "Targets", value: stats.targets.length },
            { label: "Quality tiers", value: stats.tiers.length },
          ].map((item) => <div key={item.label} className="bg-[#0b0c0f] px-4 py-3"><p className="font-mono text-lg tabular-nums tracking-[-0.04em] text-zinc-200">{item.value.toLocaleString()}</p><p className="mt-0.5 text-[10px] text-zinc-700">{item.label}</p></div>)}</div>}
          <FilterBar>
            <form className="flex min-w-0 flex-1 gap-2 sm:max-w-sm" onSubmit={(event) => { event.preventDefault(); setSubmittedQuery(query.trim()); }}><div className="relative min-w-0 flex-1"><Search className="absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-zinc-700" /><Input aria-label="Search corpus text" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search corpus text…" className="h-8 border-white/10 bg-[#0d0e11] pl-8 text-xs" /></div><Button type="submit" variant="outline" size="sm"><Filter />Filter</Button></form>
            <Select value={target} onValueChange={setTarget}><SelectTrigger aria-label="Filter by corpus target" className="h-8 w-28 border-white/10 bg-[#0d0e11] text-xs"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="all">All targets</SelectItem>{targetOptions.map((value) => <SelectItem key={value} value={value}>{value.toUpperCase()}</SelectItem>)}</SelectContent></Select>
            {(submittedQuery || target !== "all") && <Button variant="ghost" size="sm" className="text-zinc-600" onClick={() => { setQuery(""); setSubmittedQuery(""); setTarget("all"); }}><RotateCcw />Reset</Button>}
            <span className="ml-auto font-mono text-[10px] text-zinc-700">{items.length} shown</span>
          </FilterBar>
          <div className="p-3 sm:p-4">{state === "loading" ? <SkeletonState /> : state === "error" ? <ErrorState description={error} onRetry={() => void load()} /> : <DataTable columns={columns} data={items} getRowId={corpusRecordId} selectedId={selected ? corpusRecordId(selected) : null} onRowSelect={setSelected} empty={<EmptyState title="Corpus is empty" description="Build a RAG, SFT, or DAPT corpus to populate this workspace." />} />}</div>
        </CollectionContent>
      </InspectorPanel>
    </Workspace>
  );
}

function Workspace({ children }: { children: React.ReactNode }) {
  return <section className="flex h-full min-h-0 flex-col bg-[#090a0c]">{children}</section>;
}

function CollectionContent({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full min-h-0 flex-col overflow-auto">{children}</div>;
}

function FilterBar({ children }: { children: React.ReactNode }) {
  return <div className="flex min-h-12 shrink-0 flex-wrap items-center gap-2 border-b border-white/[0.075] bg-[#0b0c0f] px-3 py-2 sm:px-4">{children}</div>;
}

function InspectorContent({ children }: { children: React.ReactNode }) {
  return <div className="flex h-full min-h-0 flex-col overflow-hidden bg-[#0d0e11]">{children}</div>;
}

function normalizeProgress(value: unknown) {
  if (typeof value !== "number" || Number.isNaN(value)) return 0;
  return Math.max(0, Math.min(1, value > 1 ? value / 100 : value));
}

function crawlCounterProgress(job: CrawlJob | null) {
  if (!job) return 0;
  const discovered = typeof job.discovered_count === "number" ? job.discovered_count : 0;
  const terminal = typeof job.terminal_count === "number" ? job.terminal_count : 0;
  return discovered > 0 ? terminal / discovered : 0;
}

function isCapturedCrawlPage(page: CrawlDisplayPage) {
  return page.state === "succeeded" || page.markdown.length > 0;
}

function crawlTimeline(status: string, progress: number): TimelineStep[] {
  const terminal = isTerminalCrawlStatus(status);
  const failed = terminal && status !== "completed";
  const activeIndex = terminal ? 4 : progress < 0.05 ? 0 : progress < 0.25 ? 1 : progress < 0.7 ? 2 : progress < 0.95 ? 3 : 4;
  return [
    ["Queue accepted", "Validate scope and create the frontier."],
    ["Discover URLs", "Read sitemap and same-site links."],
    ["Fetch pages", "Use HTTP first and browser fallback."],
    ["Extract content", "Clean Markdown and compute signals."],
    ["Persist artifacts", "Store output, metadata, and provenance."],
  ].map(([label, description], index) => ({
    label,
    description,
    state: terminal
      ? (failed && index === activeIndex ? "error" : "done")
      : index < activeIndex
        ? "done"
        : index === activeIndex
          ? "active"
          : "pending",
  } as TimelineStep));
}

function crawlEvents(job: CrawlJob, pages: CrawlDisplayPage[]) {
  const errors = job.errors ?? [];
  return [
    `status   ${job.status}`,
    `progress ${Math.round((isTerminalCrawlStatus(job.status) ? 1 : normalizeProgress(job.progress ?? crawlCounterProgress(job))) * 100)}%`,
    `pages    ${pages.length}`,
    `errors   ${errors.length}`,
    ...pages.slice(-12).map((page, index) => `page ${String(index + 1).padStart(3, "0")}  ${String(page.url ?? page.title ?? "captured")}`),
    ...errors.slice(-5).map((item) => `error     ${typeof item === "string" ? item : JSON.stringify(item)}`),
  ].join("\n");
}

function prettyJson(source: string) {
  if (!source) return "";
  try { return JSON.stringify(JSON.parse(source), null, 2); } catch { return source; }
}

function corpusRecordId(record: CorpusRecord) {
  return record.id ?? `${record.file}:${record.chunkIndex}`;
}
