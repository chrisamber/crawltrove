"use client";

import * as React from "react";
import { AlertTriangle, Check, ChevronRight, Circle, Copy, GripVertical, Inbox, LoaderCircle, RotateCcw, X } from "lucide-react";
import { Group, Panel, Separator as PanelSeparator } from "react-resizable-panels";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

type StatusTone = "neutral" | "info" | "success" | "warning" | "error";

const statusTones: Record<StatusTone, string> = {
  neutral: "border-white/10 bg-white/[0.04] text-zinc-400",
  info: "border-indigo-400/20 bg-indigo-400/10 text-indigo-300",
  success: "border-emerald-400/20 bg-emerald-400/10 text-emerald-300",
  warning: "border-amber-400/20 bg-amber-400/10 text-amber-300",
  error: "border-rose-400/20 bg-rose-400/10 text-rose-300",
};

const semanticStatus: Record<string, StatusTone> = {
  completed: "success",
  healthy: "success",
  up: "success",
  active: "info",
  running: "info",
  crawling: "info",
  pending: "warning",
  queued: "warning",
  interrupted: "warning",
  down: "error",
  failed: "error",
  error: "error",
  cancelled: "neutral",
  disabled: "neutral",
};

export function StatusBadge({ status, tone, className }: { status: string; tone?: StatusTone; className?: string }) {
  const resolved = tone ?? semanticStatus[status.toLowerCase()] ?? "neutral";
  return (
    <Badge
      variant="outline"
      className={cn("h-5 rounded-[5px] px-1.5 font-mono text-[10px] font-medium uppercase tracking-[0.08em]", statusTones[resolved], className)}
    >
      {status}
    </Badge>
  );
}

export type DataColumn<T> = {
  id: string;
  header: React.ReactNode;
  cell: (item: T) => React.ReactNode;
  className?: string;
};

export function DataTable<T>({
  columns,
  data,
  getRowId,
  selectedId,
  onRowSelect,
  empty,
}: {
  columns: DataColumn<T>[];
  data: T[];
  getRowId: (item: T) => React.Key;
  selectedId?: React.Key | null;
  onRowSelect?: (item: T) => void;
  empty?: React.ReactNode;
}) {
  if (!data.length) return <>{empty ?? <EmptyState title="Nothing here yet" description="New records will appear here." />}</>;

  return (
    <div className="overflow-hidden rounded-[8px] border border-white/[0.08] bg-[#0d0e11]">
      <Table>
        <TableHeader className="bg-white/[0.025]">
          <TableRow className="border-white/[0.08] hover:bg-transparent">
            {columns.map((column) => (
              <TableHead key={column.id} className={cn("h-9 px-3 font-mono text-[10px] uppercase tracking-[0.08em] text-zinc-500", column.className)}>
                {column.header}
              </TableHead>
            ))}
            {onRowSelect && <TableHead className="w-10"><span className="sr-only">Actions</span></TableHead>}
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((item) => {
            const id = getRowId(item);
            const selected = selectedId === id;
            return (
              <TableRow
                key={id}
                onClick={() => onRowSelect?.(item)}
                className={cn(
                  "group h-12 border-white/[0.065] transition-colors hover:bg-white/[0.035]",
                  onRowSelect && "cursor-pointer active:bg-white/[0.055]",
                  selected && "bg-indigo-400/[0.07] hover:bg-indigo-400/[0.09]",
                )}
              >
                {columns.map((column) => (
                  <TableCell key={column.id} className={cn("max-w-80 px-3 py-2 text-xs text-zinc-300", column.className)}>
                    {column.cell(item)}
                  </TableCell>
                ))}
                {onRowSelect && (
                  <TableCell className="w-10 px-1.5">
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon-sm"
                          aria-label={`Open details for ${String(id)}`}
                          aria-current={selected ? "true" : undefined}
                          className="text-zinc-700 group-hover:text-zinc-400"
                          onClick={(event) => {
                            event.stopPropagation();
                            onRowSelect(item);
                          }}
                        >
                          <ChevronRight className="size-3.5" />
                        </Button>
                      </TooltipTrigger>
                      <TooltipContent>Open details</TooltipContent>
                    </Tooltip>
                  </TableCell>
                )}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}

function useDesktopSplit() {
  const [desktop, setDesktop] = React.useState(false);
  React.useEffect(() => {
    const query = window.matchMedia("(min-width: 900px)");
    const update = () => setDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return desktop;
}

export function InspectorPanel({
  children,
  inspector,
  open,
  onClose,
}: {
  children: React.ReactNode;
  inspector: React.ReactNode;
  open: boolean;
  onClose: () => void;
}) {
  const desktop = useDesktopSplit();
  if (!desktop) {
    return (
      <>
        {children}
        <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
          <DialogContent className="h-[88dvh] max-w-[calc(100%-24px)] overflow-hidden border-white/10 bg-[#0d0e11] p-0 sm:max-w-xl">
            <DialogTitle className="sr-only">Selected item details</DialogTitle>
            {inspector}
          </DialogContent>
        </Dialog>
      </>
    );
  }

  if (!open) return <>{children}</>;
  return (
    <Group orientation="horizontal" className="h-full" defaultLayout={{ content: 68, inspector: 32 }}>
      <Panel id="content" minSize="44%">
        {children}
      </Panel>
      <PanelSeparator aria-label="Resize inspector" className="group relative w-px bg-white/[0.08] outline-none focus-visible:bg-indigo-400">
        <div className="absolute left-1/2 top-1/2 z-10 flex h-8 w-3 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-white/10 bg-[#16171b] text-zinc-600 transition-colors group-hover:text-zinc-300">
          <GripVertical className="size-2.5" />
        </div>
      </PanelSeparator>
      <Panel id="inspector" defaultSize="32%" minSize={320} maxSize="48%">
        {inspector}
      </Panel>
    </Group>
  );
}

export function InspectorHeader({ eyebrow, title, onClose }: { eyebrow: string; title: string; onClose: () => void }) {
  return (
    <div className="flex h-14 items-center justify-between border-b border-white/[0.08] px-4">
      <div className="min-w-0">
        <p className="font-mono text-[9px] uppercase tracking-[0.12em] text-zinc-600">{eyebrow}</p>
        <h2 className="truncate text-sm font-medium text-zinc-100">{title}</h2>
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button size="icon-sm" variant="ghost" onClick={onClose} aria-label="Close inspector" className="text-zinc-500 hover:text-zinc-100"><X /></Button>
        </TooltipTrigger>
        <TooltipContent>Close inspector</TooltipContent>
      </Tooltip>
    </div>
  );
}

export function KeyValueList({ items }: { items: Array<{ label: string; value: React.ReactNode }> }) {
  return (
    <dl className="divide-y divide-white/[0.065] border-y border-white/[0.065]">
      {items.map((item) => (
        <div key={item.label} className="grid grid-cols-[112px_minmax(0,1fr)] gap-3 px-4 py-2.5 text-xs">
          <dt className="text-zinc-600">{item.label}</dt>
          <dd className="min-w-0 break-words font-mono text-zinc-300">{item.value === null || item.value === undefined || item.value === "" ? "—" : item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

type CodeKind = "markdown" | "html" | "json" | "events";
const codeLabels: Record<CodeKind, string> = { markdown: "Markdown", html: "HTML", json: "JSON", events: "Events" };

export function CodeViewer({ sources, className }: { sources: Partial<Record<CodeKind, string>>; className?: string }) {
  const entries = (Object.entries(sources) as Array<[CodeKind, string]>).filter((entry) => entry[1]);
  const first = entries[0]?.[0] ?? "json";
  if (!entries.length) return <EmptyState compact title="No source payload" description="This item has metadata only." />;
  return (
    <Tabs defaultValue={first} className={cn("min-h-0", className)}>
      <div className="flex h-9 items-center justify-between border-b border-white/[0.08] px-2">
        <TabsList variant="line" className="h-9 gap-0">
          {entries.map(([kind]) => (
            <TabsTrigger key={kind} value={kind} className="h-8 px-2 font-mono text-[10px]">
              {codeLabels[kind]}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      {entries.map(([kind, source]) => (
        <TabsContent key={kind} value={kind} className="relative min-h-0">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                size="icon-sm"
                variant="ghost"
                className="absolute right-2 top-2 z-10 bg-[#111216]/80 text-zinc-500 backdrop-blur hover:text-zinc-100"
                aria-label={`Copy ${codeLabels[kind]}`}
                onClick={async () => {
                  try {
                    await navigator.clipboard.writeText(source);
                    toast.success(`${codeLabels[kind]} copied`);
                  } catch {
                    toast.error(`Couldn’t copy ${codeLabels[kind].toLowerCase()}`);
                  }
                }}
              >
                <Copy />
              </Button>
            </TooltipTrigger>
            <TooltipContent>Copy {codeLabels[kind]}</TooltipContent>
          </Tooltip>
          <ScrollArea className="h-80">
            <pre className="min-w-max p-4 pr-12 font-mono text-[11px] leading-5 text-zinc-400"><code>{source}</code></pre>
          </ScrollArea>
        </TabsContent>
      ))}
    </Tabs>
  );
}

export type TimelineStep = { label: string; description?: string; state: "done" | "active" | "pending" | "error" };

export function ProgressTimeline({ steps }: { steps: TimelineStep[] }) {
  return (
    <ol className="space-y-0">
      {steps.map((step, index) => (
        <li key={step.label} aria-current={step.state === "active" ? "step" : undefined} className="relative grid grid-cols-[20px_1fr] gap-3 pb-4 last:pb-0">
          {index < steps.length - 1 && <span className={cn("absolute left-[9px] top-4 h-[calc(100%-8px)] w-px", step.state === "done" ? "bg-indigo-400/50" : "bg-white/[0.08]")} />}
          <span className={cn("relative z-10 mt-0.5 flex size-5 items-center justify-center rounded-full border bg-[#0d0e11]", step.state === "done" && "border-indigo-400/50 text-indigo-300", step.state === "active" && "border-indigo-400 text-indigo-300 shadow-[0_0_0_3px_rgba(129,140,248,0.12)]", step.state === "pending" && "border-white/10 text-zinc-700", step.state === "error" && "border-rose-400/50 text-rose-300")}>
            {step.state === "done" ? <Check className="size-3" /> : step.state === "active" ? <LoaderCircle className="size-3 animate-spin" /> : step.state === "error" ? <AlertTriangle className="size-3" /> : <Circle className="size-2 fill-current" />}
          </span>
          <div>
            <p className={cn("text-xs font-medium", step.state === "pending" ? "text-zinc-600" : "text-zinc-200")}>{step.label}</p>
            {step.description && <p className="mt-0.5 text-[11px] text-zinc-600">{step.description}</p>}
          </div>
        </li>
      ))}
    </ol>
  );
}

export function EmptyState({ title, description, action, compact = false }: { title: string; description: string; action?: React.ReactNode; compact?: boolean }) {
  return (
    <div className={cn("flex flex-col items-center justify-center text-center", compact ? "min-h-40 px-6 py-8" : "min-h-80 px-8 py-14")}>
      <div className="mb-3 flex size-8 items-center justify-center rounded-[7px] border border-white/[0.08] bg-white/[0.025] text-zinc-600"><Inbox className="size-4" /></div>
      <h3 className="text-sm font-medium text-zinc-200">{title}</h3>
      <p className="mt-1 max-w-sm text-xs leading-5 text-zinc-600">{description}</p>
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

export function ErrorState({ title = "Couldn’t load this view", description, onRetry }: { title?: string; description: string; onRetry?: () => void }) {
  return (
    <div className="flex min-h-72 flex-col items-center justify-center px-8 text-center" role="alert">
      <div className="mb-3 flex size-8 items-center justify-center rounded-[7px] border border-rose-400/15 bg-rose-400/[0.07] text-rose-300"><AlertTriangle className="size-4" /></div>
      <h3 className="text-sm font-medium text-zinc-200">{title}</h3>
      <p className="mt-1 max-w-md text-xs leading-5 text-zinc-600">{description}</p>
      {onRetry && <Button variant="outline" size="sm" className="mt-4" onClick={onRetry}><RotateCcw />Retry</Button>}
    </div>
  );
}

export function SkeletonState() {
  return (
    <div className="space-y-2 rounded-[8px] border border-white/[0.08] p-3" aria-label="Loading" role="status">
      {Array.from({ length: 6 }).map((_, index) => (
        <div key={index} className="flex h-10 items-center gap-4 border-b border-white/[0.05] last:border-0">
          <Skeleton className="h-3 w-20 bg-white/[0.05]" />
          <Skeleton className="h-3 flex-1 bg-white/[0.05]" />
          <Skeleton className="h-3 w-14 bg-white/[0.05]" />
        </div>
      ))}
    </div>
  );
}
