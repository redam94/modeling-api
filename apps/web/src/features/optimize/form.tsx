/**
 * The budget form: how much to spend, how cautious to be, and the limits per channel (and
 * portfolio groups in Expert mode). Problems are explained as you type, before any run.
 */
import { AlertTriangle, Lock, Plus, Trash2, Wallet } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, Switch } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect } from "@/components/ui/primitives";
import { amount } from "@/features/guided/plain";
import { cn } from "@/lib/utils";

import { OBJECTIVES, planProblems, targetBudget, type BudgetMode, type ChannelLimit, type PlanForm } from "./plan";

function numberOrNull(v: string): number | null {
  if (v.trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function RadioCards<T extends string>({ name, value, options, onChange, legend }: { name: string; value: T; options: readonly { value: T; label: ReactNode; help?: ReactNode }[]; onChange: (v: T) => void; legend: string }) {
  return (
    <fieldset className="grid gap-2">
      <legend className="pb-1 text-[13px] font-medium">{legend}</legend>
      <div className="grid gap-2 sm:grid-cols-2">
        {options.map((o) => (
          <label
            key={o.value}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-lg border bg-card px-3 py-2.5 text-[13px] hover:bg-accent/40 has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-ring",
              value === o.value && "border-primary/70 bg-accent/60",
            )}
          >
            <input type="radio" name={name} value={o.value} checked={value === o.value} onChange={() => onChange(o.value)} className="mt-0.5 accent-primary" />
            <span className="grid gap-0.5">
              <span className="font-medium">{o.label}</span>
              {o.help ? <span className="text-xs text-muted-foreground">{o.help}</span> : null}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export interface PlanFormProps {
  form: PlanForm;
  onChange: (f: PlanForm) => void;
  channels: readonly string[];
  current: Record<string, number>;
  geos: readonly string[];
  units: string | null;
  periods: number | null;
  expert: boolean;
  onSubmit: () => void;
  pending: boolean;
  canRun: boolean;
}

export function PlanFormView({ form, onChange, channels, current, geos, units, periods, expert, onSubmit, pending, canRun }: PlanFormProps) {
  const name = useId();
  const total = Object.values(current).reduce((a, b) => a + b, 0);
  const problems = planProblems(form, current);
  const set = (patch: Partial<PlanForm>) => onChange({ ...form, ...patch });
  const setLimit = (ch: string, patch: Partial<ChannelLimit>) => set({ limits: { ...form.limits, [ch]: { ...(form.limits[ch] ?? { unit: "multiplier", min: null, max: null, locked: false }), ...patch } } });
  const budget = targetBudget(form, total);
  const modes: { value: BudgetMode; label: ReactNode; help?: ReactNode }[] = [
    { value: "same", label: "Keep today's total", help: `Split ${amount(total, units)} better.` },
    { value: "total", label: "Set a new total" },
    { value: "change", label: "Change it by a percentage" },
    ...(expert ? [{ value: "free" as const, label: "Let the model choose the total", help: "Fund every channel up to break-even." }] : []),
  ];
  const limitsTable = (
    <div className="grid gap-3">
      <div className="overflow-x-auto rounded-lg border [contain:inline-size]">
        <table className="w-full text-[13px]" aria-label="Limits per channel">
          <thead>
            <tr className="border-b text-xs text-muted-foreground">
              <th scope="col" className="px-3 py-2 text-left font-medium">Channel</th>
              <th scope="col" className="px-3 py-2 text-right font-medium">Spend today</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Limits in</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">At least</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">At most</th>
              <th scope="col" className="px-3 py-2 text-left font-medium">Keep as is</th>
            </tr>
          </thead>
          <tbody>
            {channels.map((ch) => {
              const l = form.limits[ch] ?? { unit: "multiplier", min: null, max: null, locked: false };
              const ph = l.unit === "multiplier" ? [String(form.defaultMin), String(form.defaultMax)] : ["0", String(Math.round(2 * (current[ch] ?? 0)))];
              return (
                <tr key={ch} className="border-b last:border-0" data-channel={ch}>
                  <td className="px-3 py-1.5 font-medium">{ch}</td>
                  <td className="px-3 py-1.5 text-right tabular">{amount(current[ch] ?? 0, units)}</td>
                  <td className="px-3 py-1.5">
                    <NativeSelect aria-label={`Limits for ${ch} in`} value={l.unit} disabled={l.locked} onChange={(e) => setLimit(ch, { unit: e.target.value as ChannelLimit["unit"], min: null, max: null })} className="h-8 w-44 text-[13px]">
                      <option value="multiplier">× today&apos;s spend</option>
                      <option value="money">{units ?? "money"}</option>
                    </NativeSelect>
                  </td>
                  <td className="px-3 py-1.5">
                    <Input aria-label={`Minimum for ${ch}`} type="number" min={0} step="any" placeholder={ph[0]} disabled={l.locked} value={l.min ?? ""} onChange={(e) => setLimit(ch, { min: numberOrNull(e.target.value) })} className="h-8 w-28" />
                  </td>
                  <td className="px-3 py-1.5">
                    <Input aria-label={`Maximum for ${ch}`} type="number" min={0} step="any" placeholder={ph[1]} disabled={l.locked} value={l.max ?? ""} onChange={(e) => setLimit(ch, { max: numberOrNull(e.target.value) })} className="h-8 w-28" />
                  </td>
                  <td className="px-3 py-1.5">
                    <Checkbox aria-label={`Keep ${ch} at today's spend`} checked={l.locked} onCheckedChange={(c) => setLimit(ch, { locked: c === true })} />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex flex-wrap items-end gap-2 text-[13px]">
        <span className="pb-2 text-muted-foreground">Channels without limits may go from</span>
        <Input aria-label="Default minimum (× today's spend)" type="number" min={0} step={0.1} value={form.defaultMin} onChange={(e) => set({ defaultMin: Number(e.target.value) })} className="h-8 w-20" />
        <span className="pb-2 text-muted-foreground">× to</span>
        <Input aria-label="Default maximum (× today's spend)" type="number" min={0} step={0.1} value={form.defaultMax} onChange={(e) => set({ defaultMax: Number(e.target.value) })} className="h-8 w-20" />
        <span className="pb-2 text-muted-foreground">× today&apos;s spend.</span>
      </div>
    </div>
  );
  return (
    <form
      className="grid gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        if (!problems.length) onSubmit();
      }}
      aria-label="Budget plan"
    >
      <Card>
        <CardHeader>
          <div className="grid gap-1">
            <CardTitle>Your budget</CardTitle>
            <CardDescription>
              Today the channels below spend {amount(total, units)}
              {periods ? ` over ${periods} periods` : ""}. {budget !== null ? `The plan will spend ${amount(budget, units)}.` : "The model will choose the total."}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="grid gap-5">
          <RadioCards name={`${name}-mode`} legend="How much to spend" value={form.mode} options={modes} onChange={(mode) => set({ mode })} />
          {form.mode === "total" ? (
            <Field label={`Total budget${units ? ` (${units})` : ""}`} htmlFor={`${name}-total`}>
              <Input id={`${name}-total`} type="number" min={0} step="any" value={form.total ?? ""} onChange={(e) => set({ total: numberOrNull(e.target.value) })} className="w-48" />
            </Field>
          ) : null}
          {form.mode === "change" ? (
            <Field label="Change (%)" htmlFor={`${name}-pct`} hint="For example −10 to spend 10% less, 15 to spend 15% more.">
              <Input id={`${name}-pct`} type="number" min={-99} max={1000} step="any" value={form.changePct ?? ""} onChange={(e) => set({ changePct: numberOrNull(e.target.value) })} className="w-32" />
            </Field>
          ) : null}
          {form.mode === "free" ? (
            <Field label="Value of one unit of the result in money" htmlFor={`${name}-vpk`} hint="1 when the result is revenue in the same currency as spend.">
              <Input id={`${name}-vpk`} type="number" min={0} step="any" value={form.valuePerKpi ?? ""} onChange={(e) => set({ valuePerKpi: numberOrNull(e.target.value) })} className="w-32" />
            </Field>
          ) : null}
          <RadioCards name={`${name}-goal`} legend="How cautious should the plan be?" value={OBJECTIVES.some((o) => o.value === form.objective) ? form.objective : "custom"} options={[...OBJECTIVES.map((o) => ({ value: o.value, label: o.label, help: o.help })), ...(expert ? [{ value: "custom", label: "Custom objective", help: "mean, p1–p49 or cvar1–cvar49" }] : [])]} onChange={(objective) => set({ objective: objective === "custom" ? "p5" : objective })} />
          {expert && !OBJECTIVES.some((o) => o.value === form.objective) ? (
            <Field label="Objective" htmlFor={`${name}-obj`}>
              <Input id={`${name}-obj`} value={form.objective} onChange={(e) => set({ objective: e.target.value.trim() })} className="w-32 font-mono" />
            </Field>
          ) : null}
          <div className="flex flex-wrap items-end gap-6">
            <Field label="Periods the plan covers" htmlFor={`${name}-h`} hint={periods ? `Empty: ${periods}, as the fitted period.` : "Empty: as the fitted period."}>
              <Input id={`${name}-h`} type="number" min={1} max={520} value={form.horizon ?? ""} onChange={(e) => set({ horizon: numberOrNull(e.target.value) })} className="w-28" />
            </Field>
            {geos.length ? (
              <label className="flex items-center gap-3 pb-2 text-[13px]">
                <Switch checked={form.byGeo} onCheckedChange={(byGeo) => set({ byGeo })} aria-label="Plan each market separately" /> Plan each of the {geos.length} markets separately
              </label>
            ) : null}
            <label className="flex items-center gap-3 pb-2 text-[13px]">
              <Switch checked={form.frontier} onCheckedChange={(frontier) => set({ frontier })} aria-label="Show what other budgets would bring" /> Show what other budgets would bring
            </label>
          </div>
        </CardContent>
      </Card>

      {expert ? (
        <Card>
          <CardHeader>
            <div className="grid gap-1">
              <CardTitle>Limits per channel</CardTitle>
              <CardDescription>Bounds as multiples of today&apos;s spend or in money (money wins). Locked channels keep today&apos;s spend.</CardDescription>
            </div>
          </CardHeader>
          <CardContent>{limitsTable}</CardContent>
        </Card>
      ) : (
        <details className="rounded-xl border bg-card">
          <summary className="cursor-pointer px-5 py-3 text-[14px] font-medium select-none">
            <Lock aria-hidden className="mr-1.5 inline size-4 text-muted-foreground" /> Limits per channel (optional)
          </summary>
          <div className="border-t p-5">{limitsTable}</div>
        </details>
      )}

      {expert ? <GroupsEditor form={form} onChange={onChange} channels={channels} /> : null}

      {expert ? (
        <Card>
          <CardHeader>
            <CardTitle>Advanced</CardTitle>
          </CardHeader>
          <CardContent className="grid gap-4 sm:grid-cols-3">
            <Field label="Fund every channel at least" htmlFor={`${name}-minch`}>
              <Input id={`${name}-minch`} type="number" min={0} step="any" value={form.minChannelSpend ?? ""} onChange={(e) => set({ minChannelSpend: numberOrNull(e.target.value) })} />
            </Field>
            <Field label="Posterior draws" htmlFor={`${name}-draws`} hint="20–2000">
              <Input id={`${name}-draws`} type="number" min={20} max={2000} value={form.maxDraws} onChange={(e) => set({ maxDraws: Number(e.target.value) })} />
            </Field>
            <Field label="Optimiser steps" htmlFor={`${name}-steps`} hint="50–5000">
              <Input id={`${name}-steps`} type="number" min={50} max={5000} value={form.nSteps} onChange={(e) => set({ nSteps: Number(e.target.value) })} />
            </Field>
            <Field label="Frontier points" htmlFor={`${name}-fp`}>
              <Input id={`${name}-fp`} type="number" min={3} max={50} value={form.frontierPoints} disabled={!form.frontier} onChange={(e) => set({ frontierPoints: Number(e.target.value) })} />
            </Field>
            <Field label="Seed" htmlFor={`${name}-seed`}>
              <Input id={`${name}-seed`} type="number" min={0} value={form.seed ?? ""} onChange={(e) => set({ seed: numberOrNull(e.target.value) })} />
            </Field>
          </CardContent>
        </Card>
      ) : null}

      {problems.length ? (
        <div role="alert" className="rounded-lg border border-warning/40 bg-warning/8 p-3 text-[13px]">
          <ul className="grid gap-1" aria-label="Problems with the plan">
            {problems.map((p) => (
              <li key={p} className="flex items-start gap-2">
                <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" /> {p}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="submit" size="lg" disabled={pending || problems.length > 0 || !canRun}>
          {pending ? <Spinner /> : <Wallet />} Optimise the budget
        </Button>
        {!canRun ? <p className="text-xs text-muted-foreground">Your role cannot start runs in this project.</p> : <p className="text-xs text-muted-foreground">Runs in the sandbox over the model&apos;s uncertainty; usually under a minute.</p>}
      </div>
    </form>
  );
}

function GroupsEditor({ form, onChange, channels }: { form: PlanForm; onChange: (f: PlanForm) => void; channels: readonly string[] }) {
  const setGroups = (groups: PlanForm["groups"]) => onChange({ ...form, groups });
  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>Portfolio groups</CardTitle>
          <CardDescription>Constraints over several channels, e.g. digital gets at least 40% of the budget.</CardDescription>
        </div>
        <Button type="button" size="sm" variant="outline" onClick={() => setGroups([...form.groups, { name: `group ${form.groups.length + 1}`, channels: [], min_share: null, max_share: null }])}>
          <Plus /> Add a group
        </Button>
      </CardHeader>
      <CardContent className="grid gap-3">
        {!form.groups.length ? <p className="text-[13px] text-muted-foreground">No groups.</p> : null}
        {form.groups.map((g, i) => {
          const update = (patch: Partial<(typeof form.groups)[number]>) => setGroups(form.groups.map((x, j) => (j === i ? { ...x, ...patch } : x)));
          return (
            <fieldset key={i} className="grid gap-3 rounded-lg border p-3">
              <legend className="sr-only">Group {i + 1}</legend>
              <div className="flex flex-wrap items-end gap-3">
                <Field label="Name" htmlFor={`grp-${i}`}>
                  <Input id={`grp-${i}`} value={g.name} onChange={(e) => update({ name: e.target.value })} className="h-8 w-40" />
                </Field>
                <Field label="At least (%)" htmlFor={`grp-min-${i}`}>
                  <Input id={`grp-min-${i}`} type="number" min={0} max={100} value={g.min_share != null ? g.min_share * 100 : ""} onChange={(e) => update({ min_share: e.target.value === "" ? null : Number(e.target.value) / 100 })} className="h-8 w-24" />
                </Field>
                <Field label="At most (%)" htmlFor={`grp-max-${i}`}>
                  <Input id={`grp-max-${i}`} type="number" min={0} max={100} value={g.max_share != null ? g.max_share * 100 : ""} onChange={(e) => update({ max_share: e.target.value === "" ? null : Number(e.target.value) / 100 })} className="h-8 w-24" />
                </Field>
                <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove group ${g.name}`} onClick={() => setGroups(form.groups.filter((_, j) => j !== i))}>
                  <Trash2 />
                </Button>
              </div>
              <div className="flex flex-wrap gap-1.5" role="group" aria-label={`Channels in ${g.name}`}>
                {channels.map((ch) => (
                  <label key={ch} className={cn("inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs", g.channels.includes(ch) && "border-primary/60 bg-accent")}>
                    <input type="checkbox" className="accent-primary" checked={g.channels.includes(ch)} onChange={(e) => update({ channels: e.target.checked ? [...g.channels, ch] : g.channels.filter((c) => c !== ch) })} />
                    {ch}
                  </label>
                ))}
              </div>
            </fieldset>
          );
        })}
      </CardContent>
    </Card>
  );
}
