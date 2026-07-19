"use client";

import * as React from "react";
import { Archive, Braces, ChevronRight, Database, FileText, PanelLeftClose, PanelLeftOpen, Play, Search, Server, SquareStack, Waypoints } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CommandDialog, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList, CommandSeparator, CommandShortcut } from "@/components/ui/command";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip";
import { StatusBadge } from "@/components/system";
import { cn } from "@/lib/utils";
import type { Health } from "@/lib/api";

export type ViewId = "crawl" | "runs" | "documents" | "corpus";

const navigation = [
  { id: "crawl" as const, label: "Crawl", icon: Waypoints },
  { id: "runs" as const, label: "Runs", icon: Play },
  { id: "documents" as const, label: "Documents", icon: FileText },
  { id: "corpus" as const, label: "Corpus", icon: Database },
];

function LogoMark() {
  return (
    <span className="relative flex size-7 shrink-0 items-center justify-center rounded-[7px] border border-white/10 bg-white/[0.045] text-zinc-100">
      <Archive className="size-3.5" />
      <span className="absolute -right-0.5 -top-0.5 size-1.5 rounded-full border border-[#090a0c] bg-indigo-400" />
    </span>
  );
}

export function AppShell({
  children,
  activeView,
  onViewChange,
  health,
  commandOpen,
  onCommandOpenChange,
}: {
  children: React.ReactNode;
  activeView: ViewId;
  onViewChange: (view: ViewId) => void;
  health: Health | null;
  commandOpen: boolean;
  onCommandOpenChange: (open: boolean) => void;
}) {
  const [collapsed, setCollapsed] = React.useState(false);

  React.useEffect(() => {
    const saved = window.localStorage.getItem("crawltrove.sidebar");
    if (saved) setCollapsed(saved === "collapsed");
  }, []);

  const updateCollapsed = (next: boolean) => {
    setCollapsed(next);
    window.localStorage.setItem("crawltrove.sidebar", next ? "collapsed" : "expanded");
  };

  return (
    <TooltipProvider delayDuration={300}>
      <div className="flex h-dvh min-h-[620px] overflow-hidden bg-[#090a0c] text-zinc-100">
        <aside className={cn("relative hidden shrink-0 flex-col border-r border-white/[0.075] bg-[#0b0c0f] transition-[width] duration-200 sm:flex", collapsed ? "w-[52px]" : "w-[192px]")}>
          <div className={cn("flex h-12 items-center border-b border-white/[0.075]", collapsed ? "justify-center px-2" : "gap-2.5 px-3")}>
            <LogoMark />
            {!collapsed && <span className="text-sm font-semibold tracking-[-0.02em]">CrawlTrove</span>}
          </div>

          <nav className="relative flex-1 space-y-1 p-2" aria-label="Primary navigation">
            <span className="absolute bottom-8 left-[25px] top-7 w-px bg-gradient-to-b from-indigo-400/40 via-white/[0.06] to-transparent" aria-hidden />
            {navigation.map((item) => {
              const Icon = item.icon;
              const active = item.id === activeView;
              const button = (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => onViewChange(item.id)}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "relative flex h-8 w-full items-center rounded-[6px] text-xs font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-indigo-400/60 disabled:pointer-events-none disabled:opacity-50",
                    collapsed ? "justify-center" : "gap-2.5 px-2.5",
                    active ? "bg-white/[0.065] text-zinc-100" : "text-zinc-500 hover:bg-white/[0.035] hover:text-zinc-200 active:bg-white/[0.06]",
                  )}
                >
                  <span className={cn("relative z-10 flex size-4 items-center justify-center rounded-[4px] bg-[#0b0c0f]", active && "text-indigo-300")}><Icon className="size-3.5" /></span>
                  {!collapsed && item.label}
                </button>
              );
              return collapsed ? (
                <Tooltip key={item.id}>
                  <TooltipTrigger asChild>{button}</TooltipTrigger>
                  <TooltipContent side="right">{item.label}</TooltipContent>
                </Tooltip>
              ) : button;
            })}
          </nav>

          <div className="border-t border-white/[0.075] p-2">
            {!collapsed && (
              <div className="mb-2 px-2 py-1.5">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-zinc-700">API</span>
                  <StatusBadge status={health?.status ?? "offline"} tone={health ? "success" : "error"} />
                </div>
                <p className="mt-1.5 truncate font-mono text-[10px] text-zinc-600">v{health?.version ?? "—"} · db {health?.db ?? "—"}</p>
              </div>
            )}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size={collapsed ? "icon-sm" : "sm"} className={cn("text-zinc-600 hover:text-zinc-200", !collapsed && "w-full justify-start")} onClick={() => updateCollapsed(!collapsed)}>
                  {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
                  {!collapsed && "Collapse"}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="right">{collapsed ? "Expand sidebar" : "Collapse sidebar"}</TooltipContent>
            </Tooltip>
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-12 shrink-0 items-center justify-between border-b border-white/[0.075] bg-[#0b0c0f]/95 px-3 backdrop-blur sm:px-4">
            <div className="flex items-center gap-2 sm:hidden">
              <LogoMark />
              <span className="text-sm font-semibold">CrawlTrove</span>
            </div>
            <div className="hidden items-center gap-2 text-xs text-zinc-600 sm:flex">
              <Server className="size-3.5" />
              <span>Operator console</span>
              <span className="text-zinc-800">/</span>
              <span className="capitalize text-zinc-400">{activeView}</span>
            </div>
            <div className="flex items-center gap-1.5">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button aria-label="Open command menu" variant="outline" size="sm" className="h-8 w-9 border-white/[0.08] bg-white/[0.02] px-0 text-zinc-500 hover:text-zinc-100 sm:w-52 sm:justify-start sm:px-2.5" onClick={() => onCommandOpenChange(true)}>
                    <Search className="size-3.5" />
                    <span className="hidden flex-1 text-left font-normal sm:inline">Search or jump to…</span>
                    <kbd className="hidden rounded-[4px] border border-white/10 bg-white/[0.035] px-1.5 font-mono text-[10px] text-zinc-600 sm:inline">⌘ K</kbd>
                  </Button>
                </TooltipTrigger>
                <TooltipContent className="sm:hidden">Open command menu</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button asChild variant="ghost" size="icon-sm" className="text-zinc-600 hover:text-zinc-100">
                    <a href="/docs" aria-label="Open API documentation"><Braces /></a>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>API documentation</TooltipContent>
              </Tooltip>
            </div>
          </header>

          <nav className="grid h-11 shrink-0 grid-cols-4 border-b border-white/[0.075] bg-[#0b0c0f] p-1 sm:hidden" aria-label="Mobile navigation">
            {navigation.map((item) => {
              const Icon = item.icon;
              return (
                <button key={item.id} type="button" onClick={() => onViewChange(item.id)} className={cn("flex items-center justify-center gap-1.5 rounded-[6px] text-[11px] text-zinc-600 outline-none hover:bg-white/[0.04] focus-visible:ring-2 focus-visible:ring-indigo-400/60", activeView === item.id && "bg-white/[0.06] text-zinc-100")}>
                  <Icon className="size-3.5" />{item.label}
                </button>
              );
            })}
          </nav>

          <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
        </div>
      </div>
      <GlobalCommandMenu open={commandOpen} onOpenChange={onCommandOpenChange} onNavigate={onViewChange} />
    </TooltipProvider>
  );
}

function GlobalCommandMenu({ open, onOpenChange, onNavigate }: { open: boolean; onOpenChange: (open: boolean) => void; onNavigate: (view: ViewId) => void }) {
  const run = (view: ViewId) => {
    onNavigate(view);
    onOpenChange(false);
  };
  return (
    <CommandDialog open={open} onOpenChange={onOpenChange} title="CrawlTrove command menu" description="Navigate the operator console">
      <CommandInput placeholder="Type a command or view…" />
      <CommandList>
        <CommandEmpty>No matching command.</CommandEmpty>
        <CommandGroup heading="Navigate">
          <CommandItem onSelect={() => run("crawl")}><Waypoints />Start a crawl<CommandShortcut>G C</CommandShortcut></CommandItem>
          <CommandItem onSelect={() => run("runs")}><Play />View runs<CommandShortcut>G R</CommandShortcut></CommandItem>
          <CommandItem onSelect={() => run("documents")}><FileText />Browse documents<CommandShortcut>G D</CommandShortcut></CommandItem>
          <CommandItem onSelect={() => run("corpus")}><Database />Browse corpus<CommandShortcut>G O</CommandShortcut></CommandItem>
        </CommandGroup>
        <CommandSeparator />
        <CommandGroup heading="Resources">
          <CommandItem onSelect={() => { window.location.href = "/docs"; }}><Braces />Open API documentation</CommandItem>
          <CommandItem onSelect={() => { window.location.href = "/artifacts"; }}><SquareStack />Open artifact index</CommandItem>
        </CommandGroup>
      </CommandList>
    </CommandDialog>
  );
}

export function PageHeader({
  section,
  title,
  description,
  actions,
}: {
  section: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-[76px] shrink-0 flex-col justify-between gap-3 border-b border-white/[0.075] px-4 py-3 sm:flex-row sm:items-center sm:px-5">
      <div className="min-w-0">
        <div className="mb-1 flex items-center gap-1 font-mono text-[9px] uppercase tracking-[0.12em] text-zinc-700">
          <span>Workspace</span><ChevronRight className="size-2.5" /><span>{section}</span>
        </div>
        <div className="flex items-baseline gap-2.5">
          <h1 className="text-base font-semibold tracking-[-0.02em] text-zinc-100">{title}</h1>
          <p className="hidden truncate text-xs text-zinc-600 md:block">{description}</p>
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}
