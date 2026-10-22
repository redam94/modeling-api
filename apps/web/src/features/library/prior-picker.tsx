/**
 * The prior picker of a driver: the distribution, its numbers, a "how sure are you?" shortcut,
 * and a live density sparkline drawn from the closed-form pdfs in `priors.ts` (no request).
 */
import { useId, useMemo } from "react";

import type { Issue } from "@/api/types";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { curvePath, describePrior, PRIOR_LABEL, priorCurve, resolvePrior } from "./priors";
import { issueText, NumberInput, ToggleChip } from "./ui";
import { POSITIVE_DISTS, PRIOR_DISTS, type PriorDist, type PriorSpec, type Sign } from "./types";

const W = 168;
const H = 52;

/** The density of a prior (and its sign) as a small filled curve, with 0 marked. */
export function PriorSparkline({ prior, sign = "any", className, label }: { prior: PriorSpec | undefined; sign?: Sign; className?: string; label?: string }) {
  const { line, area, zeroX } = useMemo(() => curvePath(priorCurve(prior, sign, 72), W, H, 3), [prior, sign]);
  const words = describePrior(prior, sign);
  return (
    <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={label ? `${label}: ${words}` : words} className={cn("h-13 w-full max-w-[168px]", className)} preserveAspectRatio="none">
      <line x1={0} x2={W} y1={H - 3} y2={H - 3} className="stroke-chart-base" strokeWidth={1} />
      {zeroX !== null ? <line x1={zeroX} x2={zeroX} y1={4} y2={H - 3} className="stroke-chart-ref" strokeWidth={1.5} strokeDasharray="3 3" /> : null}
      <path d={area} className="fill-chart-band" />
      <path d={line} className="fill-none stroke-chart-index" strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

const SURE: { label: string; sigma: number; title: string }[] = [
  { label: "Not sure", sigma: 2, title: "A wide prior: the data decides almost everything." },
  { label: "Fairly sure", sigma: 1, title: "A modest effect is likely." },
  { label: "Very sure", sigma: 0.3, title: "A narrow prior: small effects only, unless the data insists." },
];

export function PriorPicker({
  value,
  onChange,
  sign = "any",
  issues,
  pointer,
  label,
  positiveOnly = false,
}: {
  value: PriorSpec | undefined;
  onChange: (p: PriorSpec) => void;
  sign?: Sign;
  issues?: readonly Issue[];
  /** JSON pointer of this prior in the document (`/declarative/terms/0/prior`). */
  pointer: string;
  label: string;
  /** Scale priors (group spread, noise): positive distributions only. */
  positiveOnly?: boolean;
}) {
  const id = useId();
  const p = resolvePrior(value);
  const dists = positiveOnly ? PRIOR_DISTS.filter((d) => POSITIVE_DISTS.has(d)) : PRIOR_DISTS;
  const usesMu = p.dist !== "half_normal" && p.dist !== "exponential";
  const set = (patch: Partial<PriorSpec>) => {
    const next: PriorSpec = { ...(value ?? {}), ...patch };
    if (next.dist !== "student_t") delete next.nu;
    if (next.dist === "half_normal" || next.dist === "exponential") delete next.mu;
    onChange(next);
  };
  return (
    <div className="grid gap-3" data-pointer={pointer}>
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-end gap-3">
        <Field label={`${label}: prior`} htmlFor={`${id}-dist`} error={issueText(issues, `${pointer}/dist`)}>
          <NativeSelect id={`${id}-dist`} data-pointer={`${pointer}/dist`} value={p.dist} onChange={(e) => set({ dist: e.target.value as PriorDist })}>
            {dists.map((d) => (
              <option key={d} value={d}>
                {PRIOR_LABEL[d]}
              </option>
            ))}
          </NativeSelect>
        </Field>
        <PriorSparkline prior={value} sign={sign} label={label} className="mb-0.5" />
      </div>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label={`How sure are you about ${label}?`}>
        {SURE.map((s) => (
          <ToggleChip key={s.label} pressed={Math.abs(p.sigma - s.sigma) < 1e-9} onClick={() => set({ sigma: s.sigma })} title={s.title}>
            {s.label}
          </ToggleChip>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {usesMu ? (
          <Field label={p.dist === "lognormal" ? "Centre (log scale)" : "Centre"} htmlFor={`${id}-mu`} error={issueText(issues, `${pointer}/mu`)}>
            <NumberInput id={`${id}-mu`} data-pointer={`${pointer}/mu`} value={value?.mu ?? 0} fallback={0} onValue={(v) => set({ mu: v ?? 0 })} />
          </Field>
        ) : null}
        <Field label={p.dist === "lognormal" ? "Spread (log scale)" : "Spread"} htmlFor={`${id}-sigma`} error={issueText(issues, `${pointer}/sigma`)}>
          <NumberInput id={`${id}-sigma`} data-pointer={`${pointer}/sigma`} value={value?.sigma ?? 1} fallback={1} onValue={(v) => set({ sigma: v ?? 1 })} />
        </Field>
        {p.dist === "student_t" ? (
          <Field label="Tail weight (ν)" htmlFor={`${id}-nu`} hint="Smaller means heavier tails. Default 3." error={issueText(issues, `${pointer}/nu`)}>
            <NumberInput id={`${id}-nu`} data-pointer={`${pointer}/nu`} value={value?.nu ?? null} placeholder="3" onValue={(v) => set({ nu: v ?? null })} />
          </Field>
        ) : null}
      </div>
      <p className="text-xs text-muted-foreground">{describePrior(value, sign)}</p>
    </div>
  );
}
