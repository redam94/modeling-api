/**
 * The frame both explorers share: a section list with each section's state (ready, not
 * computed yet, does not apply, failed), the explore-run banner and the body of one section
 * (loading, problems and "not computed yet" handled once).
 */
import { Link } from "@tanstack/react-router";
import { AlertTriangle, BarChart3, CheckCircle2, Circle, Loader2, MinusCircle } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ErrorNotice, ProblemNotice, StateBadge } from "@/components/common";
import { Progress, Skeleton } from "@/components/ui/primitives";
import { PlotFrame } from "@/features/plots/plot-frame";
import { cn } from "@/lib/utils";

import type { Run } from "@/api/types";

import { recipeBody, useSectionPlots, type ExplorerTarget, type SectionState } from "./api";

export interface NavEntry {
  id: string;
  title: string;
  group: string;
  /** The section's state (view sections); absent for pages of their own (governance, lineage). */
  state?: SectionState | undefined;
}

const STATUS_ICON = {
  ok: { icon: CheckCircle2, className: "text-success", label: "ready" },
  missing: { icon: Circle, className: "text-muted-foreground", label: "not computed yet" },
  skipped: { icon: MinusCircle, className: "text-muted-foreground/70", label: "does not apply" },
  error: { icon: AlertTriangle, className: "text-destructive", label: "failed" },
} as const;

/** A section that does not apply to the data: out of the list, still one click away. */
export interface HiddenNavEntry {
  id: string;
  title: string;
  /** Why it does not apply, in plain words. */
  message: string;
}

/**
 * The section list: a vertical nav on wide screens, a wrapping list on phones. `groupOrder`
 * orders the groups (else the order of their first entry). `hidden` sections go into a closed
 * "Not for this data" list under the nav (open when the current section is one of them).
 */
export function ExplorerNav({
  entries,
  current,
  onSelect,
  label,
  hidden = [],
  groupOrder,
}: {
  entries: readonly NavEntry[];
  current: string;
  onSelect: (id: string) => void;
  label: string;
  hidden?: readonly HiddenNavEntry[];
  groupOrder?: readonly string[];
}) {
  const present = [...new Set(entries.map((e) => e.group))];
  const groups = groupOrder ? [...groupOrder.filter((g) => present.includes(g)), ...present.filter((g) => !groupOrder.includes(g))] : present;
  const currentHidden = hidden.some((h) => h.id === current);
  return (
    <nav aria-label={label} className="grid content-start gap-4 lg:sticky lg:top-4">
      {groups.map((g) => (
        <div key={g} className="grid gap-0.5">
          <h2 className="px-2 pb-1 text-[11px] font-semibold tracking-wide text-muted-foreground uppercase">{g}</h2>
          <ul className="flex flex-wrap gap-1 lg:grid lg:gap-0.5">
            {entries
              .filter((e) => e.group === g)
              .map((e) => {
                const meta = e.state ? STATUS_ICON[e.state.status] : null;
                const Icon = meta?.icon;
                return (
                  <li key={e.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(e.id)}
                      aria-current={current === e.id ? "page" : undefined}
                      title={meta ? `${e.title}: ${meta.label}` : e.title}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-md border border-transparent px-2 py-1.5 text-left text-[13px] hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
                        current === e.id && "border-border bg-accent font-medium text-accent-foreground",
                        e.state?.status === "skipped" && "text-muted-foreground",
                      )}
                      data-section={e.id}
                      data-status={e.state?.status}
                    >
                      {Icon ? <Icon aria-hidden className={cn("size-3.5 shrink-0", meta.className)} /> : <BarChart3 aria-hidden className="size-3.5 shrink-0 text-muted-foreground" />}
                      <span className="min-w-0 flex-1 truncate">{e.title}</span>
                      {meta ? <span className="sr-only">({meta.label})</span> : null}
                    </button>
                  </li>
                );
              })}
          </ul>
        </div>
      ))}
      {hidden.length ? (
        <details className="group grid gap-1 border-t pt-3" open={currentHidden || undefined} data-testid="nav-not-applicable">
          <summary className="cursor-pointer px-2 text-[12px] font-medium text-muted-foreground select-none hover:text-foreground">Not for this data ({hidden.length})</summary>
          <ul className="mt-1 flex flex-wrap gap-1 lg:grid lg:gap-0.5" aria-label="Views that do not apply to this data">
            {hidden.map((h) => (
              <li key={h.id}>
                <button
                  type="button"
                  onClick={() => onSelect(h.id)}
                  aria-current={current === h.id ? "page" : undefined}
                  title={`${h.title}: ${h.message}`}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md border border-transparent px-2 py-1 text-left text-[12px] text-muted-foreground hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring",
                    current === h.id && "border-border bg-accent font-medium text-accent-foreground",
                  )}
                  data-section={h.id}
                  data-status="skipped"
                  data-reason="not_applicable"
                >
                  <MinusCircle aria-hidden className="size-3.5 shrink-0 text-muted-foreground/70" />
                  <span className="min-w-0 flex-1 truncate">{h.title}</span>
                  <span className="sr-only">(does not apply to this data)</span>
                </button>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </nav>
  );
}

/** The state of the explore run behind the page (queued, running, done, failed). */
export function RunBanner({ exploration, error, title }: { exploration: { done: boolean; run: Run } | undefined; error?: unknown; title: string }) {
  if (error) return <ErrorNotice error={error} title="The app could not load the explore run" />;
  if (!exploration) return null;
  const run = exploration.run;
  if (exploration.done && run.state === "succeeded") return null;
  if (exploration.done) {
    return run.problem ? (
      <ProblemNotice problem={run.problem} title={`${title} did not finish`} />
    ) : (
      <div role="alert" className="flex items-center gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-3 text-[13px]">
        <AlertTriangle className="size-4 text-destructive" /> {title} ended as {run.state}.
      </div>
    );
  }
  return (
    <div className="grid gap-2 rounded-lg border bg-card p-4" aria-live="polite" data-testid="explore-run">
      <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
        <span className="flex items-center gap-2 font-medium">
          <Loader2 aria-hidden className="size-4 animate-spin text-primary" /> {title}…
        </span>
        <span className="flex items-center gap-2">
          <StateBadge state={run.state} />
          <Link to="/runs/$runId" params={{ runId: run.id }} className="text-xs text-muted-foreground hover:underline">
            Open the run
          </Link>
        </span>
      </div>
      <Progress value={run.progress?.fraction ?? null} label={`${title} progress`} />
      {run.progress?.message ? <p className="text-xs text-muted-foreground">{run.progress.message}</p> : null}
    </div>
  );
}

/**
 * The body of a view section: a skeleton while it loads, the reason when it does not apply
 * or failed, a call to compute it when it was not computed yet, else `children`.
 */
export function SectionGate({ state, isPending, error, notComputed, children }: { state: SectionState | undefined; isPending: boolean; error: unknown; notComputed: ReactNode; children: ReactNode }) {
  if (!state || state.status === "missing") return <>{notComputed}</>;
  if (state.status === "skipped") {
    return (
      <div className="flex items-start gap-2 rounded-lg border border-dashed p-5 text-[13px] text-muted-foreground" data-testid="section-skipped">
        <MinusCircle aria-hidden className="mt-0.5 size-4 shrink-0" />
        <p>{state.message ?? "This view does not apply here."}</p>
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/35 bg-destructive/6 p-5 text-[13px]">
        <AlertTriangle aria-hidden className="mt-0.5 size-4 shrink-0 text-destructive" />
        <p>{state.message ?? "The sandbox could not compute this view."} For the technical details, look at the warnings of the run.</p>
      </div>
    );
  }
  if (error) return <ErrorNotice error={error} title="The app could not load the view" />;
  if (isPending) {
    return (
      <div className="grid gap-3" aria-busy="true" aria-label="Loading the view">
        <Skeleton className="h-16" />
        <Skeleton className="h-72" />
      </div>
    );
  }
  return <>{children}</>;
}

/**
 * The section's export-ready platform plots (`mapi_plots.explorer` recipes, the same the
 * Model/Data Explorer API serves), for experts who want every series at once.
 */
export function RecipePlots({ target, state, runId, expert }: { target: ExplorerTarget; state: SectionState | undefined; runId: string | null; expert: boolean }) {
  // Fetched and rendered only once opened (plot frames in a closed disclosure never load).
  const [open, setOpen] = useState(false);
  const plots = useSectionPlots(target, state, runId, expert && open);
  if (!expert || !state?.hasPlots) return null;
  return (
    <details className="group rounded-lg border" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer px-4 py-2.5 text-[13px] font-medium select-none hover:bg-accent/40">All series (platform plots, export-ready)</summary>
      {open ? (
        <div className="grid gap-4 border-t p-4">
          {plots.isError ? <ErrorNotice error={plots.error} title="The app could not load the plots" /> : null}
          {plots.isPending ? <Skeleton className="h-60" /> : null}
          {(plots.data ?? []).map(({ plot: p, kind, datasets }, i) => (
            <div key={`${p.id}-${i}`} className="grid gap-1">
              <PlotFrame kind={kind} spec={recipeBody(p)} datasets={datasets} title={p.title} exportName={p.id} />
              {p.caption_plain ? <p className="text-xs text-muted-foreground">{p.caption_plain}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
    </details>
  );
}
