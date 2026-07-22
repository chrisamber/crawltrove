"use client";

import * as React from "react";
import { ExternalLink, LoaderCircle } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { DataTable, KeyValueList, StatusBadge, type DataColumn } from "@/components/system";
import { api, formatDate, type AcquisitionAttempt, type CrawlJob, type LiveSession, type ProviderUsage, type Worker } from "@/lib/api";
import { formatAttempt, formatNativeUsage, formatRoute } from "@/lib/acquisition-format";

type SessionAction = "resume" | "cancel";

type SessionOpenResponse = {
  url?: string;
};

function attemptsFor(job: CrawlJob): AcquisitionAttempt[] {
  return [...(job.attempts ?? [])].sort((left, right) => (
    left.startedAt.localeCompare(right.startedAt) || left.id.localeCompare(right.id)
  ));
}

function usageFor(job: CrawlJob): ProviderUsage[] {
  return job.usage ?? job.providerUsage ?? [];
}

function nativeUsage(attempt: AcquisitionAttempt): string {
  const provider = attempt.provider ?? "owned";
  const values = Object.entries(attempt.nativeUsage ?? {})
    .filter(([, value]) => typeof value === "number")
    .map(([meter, value]) => formatNativeUsage(provider, meter, value))
    .filter((value) => value !== "—");
  return values.join(", ") || "—";
}

function sessionPath(jobId: string, session: LiveSession) {
  return `/api/crawl/${encodeURIComponent(jobId)}/sessions/${encodeURIComponent(session.id)}`;
}

export function AcquisitionInspector({
  job,
  jobId,
  onRefresh,
}: {
  job: CrawlJob;
  jobId: string;
  onRefresh: () => void;
}) {
  const [pending, setPending] = React.useState(false);
  const [confirmation, setConfirmation] = React.useState<SessionAction | null>(null);
  const actionTrigger = React.useRef<HTMLButtonElement>(null);
  const session = job.activeSession && !["closed", "expired", "cancelled"].includes(
    job.activeSession.state,
  ) ? job.activeSession : null;
  const attempts = attemptsFor(job);
  const usage = usageFor(job);

  const restoreFocus = React.useCallback(() => {
    window.requestAnimationFrame(() => actionTrigger.current?.focus());
  }, []);

  const closeConfirmation = React.useCallback(() => {
    setConfirmation(null);
    restoreFocus();
  }, [restoreFocus]);

  const openSession = async () => {
    if (!session || pending) return;
    // Open synchronously to preserve the browser's user-gesture popup permission.
    const opened = window.open("", "_blank");
    if (opened) opened.opener = null;
    setPending(true);
    try {
      const response = await api<SessionOpenResponse>(`${sessionPath(jobId, session)}/token`, {
        method: "POST",
      });
      if (!opened || typeof response.url !== "string" || !response.url) {
        throw new Error("The session window could not be opened");
      }
      const target = new URL(response.url, window.location.origin);
      const expectedPath = `/api/acquisition/sessions/${encodeURIComponent(session.id)}/open`;
      if (target.origin !== window.location.origin || target.pathname !== expectedPath) {
        throw new Error("The session endpoint returned an unsafe URL");
      }
      // The one-use URL remains only in this local variable and the new window.
      opened.location.replace(target.href);
    } catch (error) {
      opened?.close();
      toast.error(error instanceof Error ? error.message : "Could not open the live session");
    } finally {
      setPending(false);
      actionTrigger.current?.focus();
    }
  };

  const runSessionAction = async () => {
    if (!session || !confirmation) return;
    const action = confirmation;
    setPending(true);
    try {
      await api(`${sessionPath(jobId, session)}/${action}`, { method: "POST" });
      toast.success(action === "resume" ? "Session resumed" : "Session cancelled");
      closeConfirmation();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : `Could not ${action} the session`);
    } finally {
      setPending(false);
      onRefresh();
    }
  };

  const attemptColumns: DataColumn<AcquisitionAttempt>[] = [
    {
      id: "route",
      header: "Route",
      cell: (attempt) => <span className="font-mono text-[11px]">{formatRoute(attempt.route)}</span>,
    },
    {
      id: "outcome",
      header: "Outcome",
      cell: (attempt) => <StatusBadge status={attempt.outcome ?? "unknown"} />,
    },
    {
      id: "detail",
      header: "Detail",
      cell: (attempt) => formatAttempt({
        route: attempt.route,
        provider: attempt.provider ?? "owned",
        outcome: attempt.outcome ?? "unknown",
        blockReason: attempt.blockReason,
        durationMs: attempt.durationMs,
      }),
    },
    { id: "error", header: "Error", cell: (attempt) => attempt.errorCode ?? "—" },
    { id: "usage", header: "Native usage", cell: nativeUsage },
  ];

  const workerColumns: DataColumn<Worker>[] = [
    { id: "state", header: "Readiness", cell: (worker) => <StatusBadge status={worker.state} /> },
    {
      id: "capabilities",
      header: "Capabilities",
      cell: (worker) => worker.capabilities.length ? worker.capabilities.join(", ") : "—",
    },
    { id: "seen", header: "Last seen", cell: (worker) => formatDate(worker.lastSeenAt) },
  ];

  return (
    <section className="space-y-4 border-t border-white/[0.08] p-4" aria-label="Acquisition details">
      <KeyValueList items={[{
        label: "Task lease",
        value: <StatusBadge status={job.state ?? job.status} />,
      }]} />
      {session ? (
        <div className="rounded-[8px] border border-amber-400/20 bg-amber-400/[0.05] p-3">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <p className="text-xs font-medium text-amber-200">Human input required</p>
              <p className="mt-0.5 text-[11px] text-zinc-500">{session.backend} · expires {formatDate(session.expiresAt)}</p>
            </div>
            <StatusBadge status="waiting_input" />
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" onClick={(event) => { actionTrigger.current = event.currentTarget; void openSession(); }} disabled={pending}>
              {pending ? <LoaderCircle className="animate-spin" /> : <ExternalLink />}Open session
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={(event) => { actionTrigger.current = event.currentTarget; setConfirmation("resume"); }} disabled={pending}>Resume</Button>
            <Button type="button" size="sm" variant="outline" onClick={(event) => { actionTrigger.current = event.currentTarget; setConfirmation("cancel"); }} disabled={pending}>Cancel</Button>
          </div>
        </div>
      ) : null}

      <div>
        <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.08em] text-zinc-600">Acquisition attempts</p>
        <DataTable columns={attemptColumns} data={attempts} getRowId={(attempt) => attempt.id} empty={<p className="text-xs text-zinc-600">No acquisition attempts recorded.</p>} />
      </div>

      {usage.length ? (
        <div>
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.08em] text-zinc-600">Provider usage</p>
          <KeyValueList items={usage.map((entry) => ({
            label: `${entry.provider} · ${entry.meter}`,
            value: `${formatNativeUsage(entry.provider, entry.meter, entry.consumed)} used · ${formatNativeUsage(entry.provider, entry.meter, entry.reserved)} reserved · ${formatNativeUsage(entry.provider, entry.meter, entry.limit)} limit`,
          }))} />
        </div>
      ) : null}

      {job.workers?.length ? (
        <div>
          <p className="mb-2 font-mono text-[10px] uppercase tracking-[0.08em] text-zinc-600">Workers</p>
          <DataTable columns={workerColumns} data={job.workers} getRowId={(worker) => worker.id} />
        </div>
      ) : null}

      <Dialog open={confirmation !== null} onOpenChange={(open) => !open && closeConfirmation()}>
        <DialogContent className="border-white/10 bg-[#0d0e11] text-zinc-100" showCloseButton={!pending}>
          <DialogHeader>
            <DialogTitle>{confirmation === "resume" ? "Resume session?" : "Cancel session?"}</DialogTitle>
            <DialogDescription>
              {confirmation === "resume"
                ? "Resume this live acquisition after the operator has completed the required step."
                : "Cancel this live acquisition. It cannot be resumed from this session."}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={closeConfirmation} disabled={pending}>Keep session</Button>
            <Button type="button" variant={confirmation === "cancel" ? "destructive" : "default"} onClick={() => void runSessionAction()} disabled={pending}>
              {pending ? <LoaderCircle className="animate-spin" /> : null}
              {confirmation === "resume" ? "Resume" : "Cancel session"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
