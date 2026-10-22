/**
 * How far the sample project has got (v0.18 D20): three stages (set up, load the data, fit)
 * and a bar that follows the fit's own progress. The words change with each stage and are
 * read out politely; the bar never claims a time ("about a minute" is the sample's own
 * estimate, said once, on the card).
 */
import { cn } from "@/lib/utils";

import type { SampleInfo } from "./api";
import { sampleFraction, stageWords } from "./use-samples";

const STAGES = [
  { state: "provisioning", label: "Set up" },
  { state: "ingesting", label: "Load data" },
  { state: "fitting", label: "Fit" },
] as const;

export type SampleProgressTone = "block" | "card";

export function SampleProgress({ info, tone = "card", className }: { info: Pick<SampleInfo, "state" | "progress">; tone?: SampleProgressTone; className?: string }) {
  const at = STAGES.findIndex((s) => s.state === info.state);
  const pct = Math.round(sampleFraction(info) * 100);
  const words = stageWords(info);
  const onBlock = tone === "block";
  return (
    <div data-testid="sample-progress" data-state={info.state ?? "none"} className={cn("grid gap-2.5", className)}>
      <p role="status" aria-live="polite" className={cn("text-sm font-semibold", onBlock ? "text-block-foreground" : "text-foreground")}>
        {words}
        <span aria-hidden>…</span>
      </p>
      <div
        role="progressbar"
        aria-label="Sample project progress"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct}
        aria-valuetext={words}
        className={cn("relative h-2.5 w-full overflow-hidden rounded-full", onBlock ? "bg-block-raised" : "bg-muted")}
      >
        <div
          className={cn("h-full rounded-full transition-[width] duration-600 ease-[cubic-bezier(0.2,0.8,0.2,1)]", onBlock ? "bg-highlight" : "bg-primary")}
          style={{ width: `${Math.max(4, pct)}%` }}
        />
      </div>
      <ol aria-label="Stages" className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
        {STAGES.map((s, i) => {
          const done = at > i;
          const current = at === i;
          return (
            <li
              key={s.state}
              aria-current={current ? "step" : undefined}
              className={cn(
                "flex items-center gap-1.5",
                onBlock ? (current ? "font-semibold text-block-foreground" : "text-block-muted") : current ? "font-semibold text-foreground" : "text-muted-foreground",
              )}
            >
              <span
                aria-hidden
                className={cn(
                  "size-2 rounded-full",
                  done ? (onBlock ? "bg-highlight" : "bg-primary") : current ? (onBlock ? "bg-block-foreground" : "bg-foreground") : onBlock ? "bg-block-raised" : "bg-border-strong",
                )}
              />
              {s.label}
              {done ? <span className="sr-only">: done</span> : null}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
