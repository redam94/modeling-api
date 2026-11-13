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

/**
 * Chains are tints of one hue (the data blue): the label tells them apart, and red stays free
 * for divergences.
 */
const CHAIN_COLORS = ["bg-chart-index", "bg-chart-index/80", "bg-chart-index/60", "bg-chart-index/45"];

/** "0.12", "0.0031": two significant digits, no exponent for usual step sizes. */
export function formatStepSize(v: number): string {
  return v >= 1e-4 ? String(Number(v.toPrecision(2))) : v.toExponential(1);
}

/**
 * One lane per chain: draws done vs total, tuning shown hatched, divergences counted, and the
 * sampler's step size when the engine reports it (v0.11). On a phone the numbers go under the
 * bar, so the bar keeps its width.
 */
export function ChainLanes({ chains }: { chains: ChainProgress[] }) {
  if (!chains.length) return null;
  return (
    <div className="grid gap-2" role="list" aria-label="Chains" data-tour="guided.run.chains">
      {chains.map((c) => {
        const frac = c.draws_total > 0 ? Math.min(1, c.draws_done / c.draws_total) : 0;
        const color = CHAIN_COLORS[c.chain % CHAIN_COLORS.length];
        const divergences = c.divergences ?? 0;
        const stepSize = c.step_size;
        return (
          <div key={c.chain} role="listitem" className="grid grid-cols-[4.5rem_minmax(0,1fr)] items-center gap-x-3 gap-y-1 text-xs sm:grid-cols-[4.5rem_minmax(0,1fr)_auto]">
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
            <span className="col-start-2 flex flex-wrap items-center gap-x-2 tabular text-muted-foreground sm:col-start-auto sm:justify-end">
              {c.tuning ? <span className="text-xs">tuning</span> : null}
              {c.draws_done.toLocaleString("en")}/{c.draws_total.toLocaleString("en")}
              <span
                // plain muted text: a lighter tint of it is under 4.5:1 on the card in the two themes
                className={cn("inline-flex min-w-16 items-center gap-1", divergences > 0 ? "font-medium text-destructive" : "text-muted-foreground")}
                title="Divergent transitions"
              >
                {divergences > 0 ? <AlertTriangle className="size-3" /> : null}
                {divergences} div.
              </span>
              {typeof stepSize === "number" ? (
                <span className="text-xs" title="Step size of the sampler">
                  step size {formatStepSize(stepSize)}
                </span>
              ) : null}
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
          <p className="text-base font-semibold">{p ? phaseLabel(p.phase) : "No progress report yet"}</p>
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
