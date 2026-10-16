/**
 * Step 6, "What if…": per-channel spend sliders answered instantly from the fit's per-draw
 * response grid (joint uncertainty, no server round trip), "Run exact" for the real score run
 * next to the instant answer, one-click scenarios ("turn a channel off", "budget × k") and a
 * forecast with its fan chart and split line.
 */
import { useNavigate } from "@tanstack/react-router";
import { ArrowRight, Calculator, LineChart, RotateCcw, Zap } from "lucide-react";
import { useMemo, useState } from "react";

import { can } from "@/api/access";
import type { ModelVersion } from "@/api/types";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Field, Input, NativeSelect, Skeleton } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { PlotFrame } from "@/features/plots/plot-frame";

import { useResponseGrid } from "../api";
import { StepActions } from "../frame";
import { amount, band, multiplierText, ratio, share, signedAmount, signedShare } from "../plain";
import { fanPlot } from "../plots";
import { useScoreJob } from "../score-job";
import { GUIDED_LABEL, hasTask, timeDimOf, wideVarOf } from "../session";
import { ChangeBand, ValueSlider } from "../ui";
import type { GuidedSession } from "../use-session";
import type { CounterfactualView } from "../views";
import { evaluateScenario, sliderRange, WhatIfError, type ResponseGrid, type WhatIfResult } from "../whatif";

function RunningNote({ text }: { text: string }) {
  return (
    <p className="flex items-center gap-2 text-[13px]" aria-live="polite">
      <Spinner /> {text}
    </p>
  );
}

function deltaSentence(lo: number, mid: number, hi: number, units: string | null): string {
  return `${signedAmount(mid, units)} (between ${signedAmount(lo, units)} and ${signedAmount(hi, units)})`;
}

function cfBand(view: CounterfactualView): { lo: number; mid: number; hi: number } {
  const b = band(view.delta_total.quantiles) ?? [view.delta_total.mean, view.delta_total.mean];
  return { lo: b[0], mid: view.delta_total.mean, hi: b[1] };
}

// ----------------------------------------------------------------------------- sliders

function WhatIf({ grid, mv, session }: { grid: ResponseGrid; mv: ModelVersion; session: GuidedSession }) {
  const range = sliderRange(grid);
  const [mult, setMult] = useState<Record<string, number>>(() => Object.fromEntries(grid.channels.map((c) => [c, 1])));
  const wide = wideVarOf(session.template);
  const grants = useGrants();
  const exact = useScoreJob(mv, "counterfactual");
  const [exactFor, setExactFor] = useState<string | null>(null);
  const key = JSON.stringify(mult);
  const result = useMemo((): WhatIfResult | WhatIfError => {
    try {
      return evaluateScenario(grid, { multipliers: mult });
    } catch (e) {
      if (e instanceof WhatIfError) return e;
      throw e;
    }
  }, [grid, mult]);
  const units = grid.kpi_units;
  const changed = Object.values(mult).some((m) => Math.abs(m - 1) > 1e-9);
  const exactView = exact.job.status === "done" && exactFor === key ? exact.job.counterfactual : null;
  const staleExact = exact.job.status === "done" && exactFor !== key;

  const runExact = () => {
    setExactFor(key);
    void exact.run(
      { kind: "whatif", model: mv.ref, spend_var: wide?.variable ?? "spend", channel_dim: wide?.dim ?? "channel", scenario: { multipliers: mult, label: "Guided what-if" } },
      { [GUIDED_LABEL]: "whatif" },
    );
  };

  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>Change the budget</CardTitle>
          <CardDescription>Move a slider to spend more or less on a channel over the same period. The answer updates as you move, with the range of likely outcomes.</CardDescription>
        </div>
        <Button variant="ghost" size="sm" disabled={!changed} onClick={() => setMult(Object.fromEntries(grid.channels.map((c) => [c, 1])))}>
          <RotateCcw /> Reset
        </Button>
      </CardHeader>
      <CardContent className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
        <ul className="grid content-start gap-5" aria-label="Spend per channel">
          {grid.channels.map((ch, c) => {
            const m = mult[ch] ?? 1;
            const spend = (grid.current_spend[c] ?? 0) * m;
            return (
              <li key={ch} className="grid gap-2" data-channel={ch}>
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="font-semibold" id={`wi-${ch}`}>
                    {ch}
                  </span>
                  <span className="text-[13px] tabular text-muted-foreground">
                    <span className="font-medium text-foreground">{multiplierText(m)}</span> · {amount(spend, grid.spend_units)}
                  </span>
                </div>
                <ValueSlider
                  label={`Spend on ${ch}`}
                  valueText={`${multiplierText(m)}, ${amount(spend, grid.spend_units)}`}
                  value={m}
                  min={range.min}
                  max={range.max}
                  step={range.step}
                  onValueChange={(v) => setMult((x) => ({ ...x, [ch]: Math.round(v * 100) / 100 }))}
                />
              </li>
            );
          })}
        </ul>
        <div className="grid content-start gap-4 rounded-lg border bg-muted/30 p-4" aria-live="polite" data-testid="whatif-result">
          {result instanceof WhatIfError ? (
            <p role="alert" className="text-[13px] text-destructive">
              {result.message}
            </p>
          ) : (
            <>
              <div className="grid gap-0.5">
                <p className="text-xs font-medium text-muted-foreground">Change in results (instant estimate)</p>
                <p className="text-2xl font-semibold tracking-tight tabular" data-testid="whatif-delta">
                  {signedAmount(result.kpi_delta.mean, units)}
                </p>
                <p className="text-xs text-muted-foreground tabular">
                  90% likely between {signedAmount(result.kpi_delta.quantiles[0]!, units)} and {signedAmount(result.kpi_delta.quantiles[2]!, units)}
                </p>
              </div>
              <ChangeBand
                lo={result.kpi_delta.quantiles[0]!}
                mid={result.kpi_delta.mean}
                hi={result.kpi_delta.quantiles[2]!}
                exact={exactView ? cfBand(exactView) : null}
                label={`Change in results ${deltaSentence(result.kpi_delta.quantiles[0]!, result.kpi_delta.mean, result.kpi_delta.quantiles[2]!, units)}${exactView ? `; exact run ${deltaSentence(cfBand(exactView).lo, cfBand(exactView).mid, cfBand(exactView).hi, units)}` : ""}`}
              />
              <dl className="grid grid-cols-2 gap-3 text-[13px]">
                <div>
                  <dt className="text-xs text-muted-foreground">Change in spend</dt>
                  <dd className="font-medium tabular">{signedAmount(result.spend_delta, grid.spend_units)}</dd>
                </div>
                <div>
                  <dt className="text-xs text-muted-foreground">Return on the change</dt>
                  <dd className="font-medium tabular">{result.roi_delta ? `${ratio(result.roi_delta.mean)} per 1` : "—"}</dd>
                </div>
              </dl>
              <div className="grid gap-2 border-t pt-3">
                <Button variant="outline" size="sm" className="justify-self-start" onClick={runExact} disabled={!changed || exact.job.status === "running" || !can(grants, "run", "model")}>
                  {exact.job.status === "running" ? <Spinner /> : <Calculator />} Run exact
                </Button>
                <p className="text-xs text-muted-foreground">The instant estimate reads precomputed curves. "Run exact" asks the full model, which takes a moment.</p>
                {exact.job.status === "running" ? <RunningNote text="Running the full model on this scenario…" /> : null}
                {exact.job.status === "error" ? <ErrorNotice error={exact.job.error} title="The exact run did not work" /> : null}
                {exactView ? (
                  <div className="grid gap-0.5" data-testid="exact-result">
                    <p className="text-xs font-medium text-muted-foreground">Exact run</p>
                    <p className="text-lg font-semibold tabular">{signedAmount(exactView.delta_total.mean, exactView.units ?? units)}</p>
                    <p className="text-xs text-muted-foreground tabular">
                      90% likely between {signedAmount(cfBand(exactView).lo, units)} and {signedAmount(cfBand(exactView).hi, units)}
                      {exactView.delta_share ? ` · ${signedShare(exactView.delta_share.mean)} of results` : ""}
                    </p>
                  </div>
                ) : staleExact ? (
                  <p className="text-xs text-warning">The sliders moved since the exact run. Run it again for the new scenario.</p>
                ) : null}
              </div>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------------- presets

function Presets({ mv, session, channels }: { mv: ModelVersion; session: GuidedSession; channels: string[] }) {
  const wide = wideVarOf(session.template);
  const choice = session.template.family === "choice";
  const job = useScoreJob(mv, "counterfactual");
  const grants = useGrants();
  const [channel, setChannel] = useState(channels[0] ?? "");
  const [k, setK] = useState(choice ? 1.1 : 1.2);
  const [asked, setAsked] = useState<string | null>(null);
  const base = { model: mv.ref, spend_var: wide?.variable ?? "spend", channel_dim: wide?.dim ?? "channel" };
  const disabled = job.job.status === "running" || !can(grants, "run", "model");
  const view = job.job.status === "done" ? job.job.counterfactual : null;
  const units = view?.units ?? null;
  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>{choice ? "Price scenarios" : "Quick scenarios"}</CardTitle>
          <CardDescription>{choice ? "What happens to choices if a product's price changes." : "One-click questions, answered by the full model."}</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="grid gap-4 md:grid-cols-2">
          {!choice ? (
            <div className="grid content-start gap-2 rounded-lg border p-4">
              <Field label="What if we turned off a channel?" htmlFor="preset-off">
                <NativeSelect id="preset-off" value={channel} onChange={(e) => setChannel(e.target.value)}>
                  {channels.map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </NativeSelect>
              </Field>
              <Button
                variant="outline"
                size="sm"
                className="justify-self-start"
                disabled={disabled || !channel}
                onClick={() => {
                  setAsked(`Turning off ${channel}`);
                  void job.run({ kind: "counterfactual", ...base, preset: { kind: "channel_off", channel } }, { [GUIDED_LABEL]: "scenario" });
                }}
              >
                <Zap /> Turn off {channel}
              </Button>
            </div>
          ) : null}
          <div className="grid content-start gap-2 rounded-lg border p-4">
            <Field label={choice ? `Change the price of ${channel || "a product"} by` : "What if every budget changed by"} htmlFor="preset-k">
              <div className="flex flex-wrap gap-2">
                {choice ? (
                  <NativeSelect aria-label="Product" value={channel} onChange={(e) => setChannel(e.target.value)} className="w-40">
                    {channels.map((c) => (
                      <option key={c} value={c}>
                        {c}
                      </option>
                    ))}
                  </NativeSelect>
                ) : null}
                <NativeSelect id="preset-k" value={k} onChange={(e) => setK(Number(e.target.value))} className="w-32">
                  {[0.5, 0.8, 0.9, 1.1, 1.2, 1.5, 2].map((x) => (
                    <option key={x} value={x}>
                      {multiplierText(x)}
                    </option>
                  ))}
                </NativeSelect>
              </div>
            </Field>
            <Button
              variant="outline"
              size="sm"
              className="justify-self-start"
              disabled={disabled}
              onClick={() => {
                setAsked(choice ? `${channel} price ${multiplierText(k)}` : `Every budget ${multiplierText(k)}`);
                void job.run({ kind: "counterfactual", ...base, preset: { kind: "budget_scale", k, ...(choice ? { channels: [channel] } : {}) } }, { [GUIDED_LABEL]: "scenario" });
              }}
            >
              <Zap /> Work it out
            </Button>
          </div>
        </div>
        {job.job.status === "running" ? <RunningNote text="Running the full model on this scenario…" /> : null}
        {job.job.status === "error" ? <ErrorNotice error={job.job.error} title="The scenario did not run" /> : null}
        {view ? (
          <div className="grid gap-1 rounded-lg border bg-muted/30 p-4" aria-live="polite" data-testid="scenario-result">
            <p className="text-xs font-medium text-muted-foreground">{asked}</p>
            <p className="text-xl font-semibold tabular">{signedAmount(view.delta_total.mean, units)}</p>
            <p className="text-xs text-muted-foreground tabular">
              90% likely between {signedAmount(cfBand(view).lo, units)} and {signedAmount(cfBand(view).hi, units)}
              {view.delta_share ? ` · ${signedShare(view.delta_share.mean)} of results` : ""}
            </p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------------- forecast

function Forecast({ mv, session }: { mv: ModelVersion; session: GuidedSession }) {
  const job = useScoreJob(mv, "forecast");
  const grants = useGrants();
  const horizonQ = session.template.questions.find((q) => q.id === "horizon");
  const maxH = typeof horizonQ?.maximum === "number" ? horizonQ.maximum : 104;
  const [periods, setPeriods] = useState(13);
  const view = job.job.status === "done" ? job.job.forecast : null;
  const plot = useMemo(() => (view ? fanPlot(view.fan, { title: "Results", splitAt: view.split_at }) : null), [view]);
  const total = view?.total ? band(view.total.quantiles) : null;
  const valid = Number.isInteger(periods) && periods >= 1 && periods <= maxH;
  return (
    <Card>
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <LineChart aria-hidden className="size-4 text-muted-foreground" /> Forecast
            </span>
          </CardTitle>
          <CardDescription>How results may develop if you carry on as in the latest weeks.</CardDescription>
        </div>
      </CardHeader>
      <CardContent className="grid gap-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="How many weeks ahead?" htmlFor="horizon" error={valid ? undefined : `Choose between 1 and ${maxH} weeks.`}>
            <Input id="horizon" type="number" min={1} max={maxH} value={periods} onChange={(e) => setPeriods(Number(e.target.value))} className="w-28" />
          </Field>
          <Button
            variant="outline"
            disabled={!valid || job.job.status === "running" || !can(grants, "run", "model")}
            onClick={() => void job.run({ kind: "forecast", model: mv.ref, periods, dim: timeDimOf(session.template), future_covariates: "carry_last" }, { [GUIDED_LABEL]: "forecast" })}
          >
            {job.job.status === "running" ? <Spinner /> : <LineChart />} Make a forecast
          </Button>
        </div>
        {job.job.status === "running" ? <RunningNote text="Forecasting…" /> : null}
        {job.job.status === "error" ? <ErrorNotice error={job.job.error} title="The forecast did not run" /> : null}
        {view && plot ? (
          <div className="grid gap-2" data-testid="forecast-result">
            {view.total ? (
              <p className="text-[13px]">
                Over the next {periods} weeks: <span className="font-semibold tabular">{amount(view.total.mean, view.units)}</span>
                {total ? <span className="text-muted-foreground"> (90% likely between {amount(total[0], view.units)} and {amount(total[1], view.units)})</span> : null}
              </p>
            ) : null}
            <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title="Forecast with its likely range" height={300} exportName="forecast" />
            <p className="text-xs text-muted-foreground">Dots are your actual results. After the dashed line, the darker band holds half of the likely outcomes and the lighter band 90%.</p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function ExploreStep({ session }: { session: GuidedSession }) {
  const navigate = useNavigate();
  const mv = session.modelVersion;
  const grid = useResponseGrid(mv);
  if (!session.lastGoodFit) {
    return <p className="text-[13px] text-muted-foreground">Fit the model first; scenarios use the fitted model.</p>;
  }
  if (!mv || grid.isPending) return <Skeleton className="h-72" />;
  const whatif = hasTask(session.template, "whatif");
  const wide = wideVarOf(session.template);
  const channels = grid.data?.channels ?? (wide ? (session.datasetVersion?.summary?.coords?.[wide.dim]?.values ?? []).map(String) : []);
  return (
    <div className="grid gap-6">
      {whatif && grid.data ? <WhatIf grid={grid.data} mv={mv} session={session} /> : null}
      {whatif && grid.isSuccess && !grid.data ? <p className="text-[13px] text-muted-foreground">This model did not save response curves, so the instant sliders are not available. The scenarios below still work.</p> : null}
      {hasTask(session.template, "counterfactual") && channels.length ? <Presets mv={mv} session={session} channels={channels} /> : null}
      {hasTask(session.template, "forecast") ? <Forecast mv={mv} session={session} /> : null}
      <p className="text-xs text-muted-foreground">
        Results are in {grid.data?.kpi_units ?? "the units of your data"} and cover the {grid.data ? `${grid.data.periods} periods` : "period"} of your data; ranges are 90% likely.
        {grid.data ? ` The sliders reach from ${share(sliderRange(grid.data).min)} to ${share(sliderRange(grid.data).max)} of what you actually spent; use "Run exact" for anything else.` : ""}
      </p>
      <StepActions>
        <Button variant="ghost" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "report" } })}>
          Back
        </Button>
        <Button onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "share" } })}>
          Share <ArrowRight />
        </Button>
      </StepActions>
    </div>
  );
}
