import { AlertTriangle } from "lucide-react";

import type { ChainProgress, ProgressSnapshot, Run } from "@/api/types";
import { Progress } from "@/components/ui/primitives";
import { formatDuration, percent, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const PHASE_LABEL: Record<string, string> = {
  bootstrap: "Starting the sandbox",
  load_inputs: "Loading inputs",
  validate: "Validating data",
  preprocess: "Preprocessing",
  fit: "Fitting",
  sample: "Sampling",
  score: "Scoring",
  evaluate: "Evaluating",
  derive: "Deriving quantities",
  plots: "Rendering plots",
  upload: "Uploading outputs",
  done: "Done",
};

export function phaseLabel(phase: string): string {
  return PHASE_LABEL[phase] ?? phase;
}

const CHAIN_COLORS = [
  "bg-series-1",
  "bg-series-2",
  "bg-series-3",
  "bg-series-4",
  "bg-series-5",
  "bg-series-6",
  "bg-series-7",
  "bg-series-8",
];

/** One lane per chain: draws done vs total, tuning shown hatched, divergences counted. */
export function ChainLanes({ chains }: { chains: ChainProgress[] }) {
  if (!chains.length) return null;
  return (
    <div className="grid gap-2" role="list" aria-label="Chains">
      {chains.map((c) => {
        const frac = c.draws_total > 0 ? Math.min(1, c.draws_done / c.draws_total) : 0;
        const color = CHAIN_COLORS[c.chain % CHAIN_COLORS.length];
        const divergences = c.divergences ?? 0;
        return (
          <div key={c.chain} role="listitem" className="grid grid-cols-[4.5rem_1fr_auto] items-center gap-3 text-xs">
            <span className="font-medium text-muted-foreground">Chain {c.chain}</span>
            <div
              className="relative h-2.5 overflow-hidden rounded-full bg-muted"
              role="progressbar"
              aria-label={`Chain ${c.chain}`}
              aria-valuemin={0}
              aria-valuemax={c.draws_total}
              aria-valuenow={c.draws_done}
            >
              <div
                className={cn("h-full rounded-full transition-[width] duration-500", color, c.tuning && "opacity-45")}
                style={{ width: `${frac * 100}%` }}
              />
            </div>
            <span className="flex items-center gap-2 tabular text-muted-foreground">
              {c.tuning ? <span className="text-[11px]">tuning</span> : null}
              {c.draws_done.toLocaleString("en")}/{c.draws_total.toLocaleString("en")}
              <span
                className={cn("inline-flex min-w-16 items-center gap-1", divergences > 0 ? "font-medium text-destructive" : "text-muted-foreground/70")}
                title="Divergent transitions"
              >
                {divergences > 0 ? <AlertTriangle className="size-3" /> : null}
                {divergences} div.
              </span>
            </span>
          </div>
        );
      })}
    </div>
  );
}

export function ProgressPanel({ run, progress }: { run: Run; progress: ProgressSnapshot | null }) {
  const p = progress ?? run.progress ?? null;
  const totalDiv = (p?.chains ?? []).reduce((acc, c) => acc + (c.divergences ?? 0), 0);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <p className="text-[15px] font-semibold">{p ? phaseLabel(p.phase) : "No progress report yet"}</p>
          {p?.message ? <p className="text-muted-foreground">{p.message}</p> : null}
        </div>
        <div className="flex gap-4 text-xs text-muted-foreground tabular">
          {p?.fraction != null ? <span>{percent(p.fraction)}</span> : null}
          {p?.eta_seconds != null ? <span>about {formatDuration(p.eta_seconds)} left</span> : null}
          {p?.updated_at ? <span>updated {relativeTime(p.updated_at)}</span> : null}
        </div>
      </div>
      <Progress value={p?.fraction ?? null} label="Run progress" />
      {p?.chains?.length ? (
        <>
          <ChainLanes chains={p.chains} />
          {totalDiv > 0 ? (
            <p className="text-xs text-destructive">
              {totalDiv} divergent transition{totalDiv === 1 ? "" : "s"} until now. When the fit finishes, look at the convergence diagnostics.
            </p>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
