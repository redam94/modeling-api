/**
 * Plan scenarios: one slider a channel, answered at once in the browser from the response grid
 * (`whatif.ts`, the port of the server's `evaluate_scenario`), with the live zone word of each
 * channel, the change against the current plan and "Why the result differs from the plan".
 * "Save scenario" keeps the plan as a private readout (no run). "Get exact result" (when the
 * analyst allows it) asks the model for a new run; that can take up to two minutes.
 */
import { RotateCcw } from "lucide-react";
import { useId, useMemo, useState } from "react";

import { InlineMessage, StatGrid, StatTile, TagWord, ZONE_TONE } from "@/components/ods";
import { grouped } from "@/components/ods/format";
import { DivergingBars } from "@/components/readout-charts";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/primitives";
import { parseResponseGrid, WhatIfError, type ResponseGrid } from "@/features/guided/whatif";

import { dashboardErrorMessage, useDashboardGrid, useReadout, useSaveScenario } from "../../client";
import { isFullReadout, type ChannelDef, type ClientDashboard, type DashboardClient, type DashboardGrid } from "../../types";
import { ReadoutState, ReadoutTiles, Section, Working } from "../parts";
import { clampMultiplier, formatValue, kpiWord, multiplierWords, periodOf, plural, scenarioModel, signedGrouped, sliderBounds, type ScenarioChannel } from "../readout-model";

function toGrid(raw: DashboardGrid): ResponseGrid | WhatIfError {
  try {
    return parseResponseGrid(raw);
  } catch (err) {
    return err instanceof WhatIfError ? err : new WhatIfError(String(err), "VIEW_INVALID");
  }
}

function ChannelSlider({
  def,
  grid,
  value,
  outcome,
  unitWord,
  period,
  onChange,
}: {
  def: ChannelDef;
  grid: ResponseGrid;
  value: number;
  outcome: ScenarioChannel | undefined;
  unitWord: string;
  period: string;
  onChange: (m: number) => void;
}) {
  const id = useId();
  const b = sliderBounds(def, grid);
  const words = multiplierWords(value);
  return (
    <div className="grid min-w-0 gap-2 border-b pb-4 last:border-b-0 last:pb-0" data-channel={def.name}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Label htmlFor={id}>{def.label}</Label>
        <span className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground tabular">{words}</span>
          {outcome?.zone ? <TagWord tone={ZONE_TONE[outcome.zone]}>{outcome.zoneWord}</TagWord> : null}
        </span>
      </div>
      <input
        id={id}
        type="range"
        min={b.min}
        max={b.max}
        step={b.step}
        value={value}
        aria-valuetext={`${words}${outcome?.zone ? `, ${outcome.zoneWord}` : ""}`}
        onChange={(e) => onChange(clampMultiplier(Number(e.target.value), b))}
        className="focus-ring h-6 w-full cursor-pointer accent-primary"
      />
      {outcome ? (
        <p className="text-xs text-hint tabular">
          {Math.abs(outcome.delta) < 0.5 ? `No change in ${unitWord}` : `${signedGrouped(outcome.delta, 0)} ${unitWord} a ${period}`}
        </p>
      ) : null}
    </div>
  );
}

export function Scenarios({ client, dashboard, preview = false }: { client: DashboardClient; dashboard: ClientDashboard; preview?: boolean }) {
  const gridQ = useDashboardGrid(client);
  const [multipliers, setMultipliers] = useState<Record<string, number>>({});
  const [label, setLabel] = useState("");
  const [exactSeq, setExactSeq] = useState<number | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const save = useSaveScenario(client);
  const exact = useReadout(client, exactSeq);
  const labelId = useId();

  const grid = useMemo(() => (gridQ.data ? toGrid(gridQ.data) : null), [gridQ.data]);
  const ready = grid && !(grid instanceof WhatIfError) ? grid : null;
  const model = useMemo(() => {
    if (!ready) return null;
    try {
      return scenarioModel(ready, dashboard, multipliers);
    } catch (err) {
      return err instanceof WhatIfError ? err : null;
    }
  }, [ready, dashboard, multipliers]);

  if (gridQ.isPending) return <Working>Loading the plan tools…</Working>;
  if (gridQ.isError) return <InlineMessage tone="error">{dashboardErrorMessage(gridQ.error)}</InlineMessage>;
  if (!gridQ.data) return <InlineMessage tone="info">Plan scenarios are not available for this dashboard.</InlineMessage>;
  if (!ready || !model || model instanceof WhatIfError) {
    return <InlineMessage tone="error">The plan tools cannot use this model&apos;s data. Ask your analyst to check the dashboard.</InlineMessage>;
  }

  const noun = kpiWord(dashboard.kpi);
  const period = periodOf(dashboard);
  const allowExact = Boolean(dashboard.inputs?.exact);
  const paused = dashboard.live_inputs === "paused";
  const byName = new Map(model.channels.map((c) => [c.name, c]));
  const channels = (dashboard.channels ?? []).filter((c) => ready.channels.includes(c.name));
  const kpiUnit = dashboard.kpi.unit === "currency" ? "currency" : "count";
  const current = model.current;
  const body = { multipliers: Object.fromEntries(channels.map((c) => [c.name, multipliers[c.name] ?? 1])), ...(label.trim() ? { label: label.trim() } : {}) };

  const reset = () => {
    setMultipliers({});
    setSaved(null);
  };
  const onSave = () => {
    setSaved(null);
    save.mutate(
      { body: { ...body, exact: false } },
      { onSuccess: () => setSaved(`We saved the plan${label.trim() ? ` "${label.trim()}"` : ""}. Only you and the analysts see it, in History.`) },
    );
  };
  const onExact = () => {
    setSaved(null);
    save.mutate(
      { body: { ...body, exact: true }, waitSeconds: 30 },
      { onSuccess: (r) => setExactSeq(r.seq) },
    );
  };
  const exactReadout = exact.data ?? null;
  const pending = save.isPending;

  return (
    <div className="grid min-w-0 gap-6" data-tab="scenarios">
      <p className="max-w-[68ch] text-sm text-muted-foreground">
        Move a slider to change the spend on a channel. The numbers change at once. The numbers are for one {period}: the average of the {ready.periods} {plural(period, ready.periods)} the model learned from. The model does not change.
      </p>
      <div className="grid min-w-0 gap-6 lg:grid-cols-12">
        <Section className="lg:col-span-5" title="Spend by channel" action={<Button variant="action" size="sm" onClick={reset} disabled={current}><RotateCcw /> Reset to current plan</Button>}>
          <div className="grid gap-4">
            {channels.map((c) => (
              <ChannelSlider
                key={c.name}
                def={c}
                grid={ready}
                value={multipliers[c.name] ?? 1}
                outcome={byName.get(c.name)}
                unitWord={noun}
                period={period}
                onChange={(m) => {
                  setSaved(null);
                  setMultipliers((prev) => ({ ...prev, [c.name]: m }));
                }}
              />
            ))}
          </div>
        </Section>
        <div className="grid min-w-0 content-start gap-6 lg:col-span-7">
          <p className="text-xl text-foreground" aria-live="polite" data-readout="scenario-lead">
            {model.lead}
          </p>
          <StatGrid className="xl:grid-cols-2">
            <StatTile
              label={`Expected ${noun}`}
              value={model.kpi ? formatValue(model.kpi.mean, kpiUnit, dashboard.kpi) : signedGrouped(model.kpiDelta, 0)}
              delta={Math.abs(model.kpiDelta) < 0.5 ? null : `${grouped(Math.abs(model.kpiDelta), dashboard.kpi.decimals ?? 0)} ${model.kpiDelta > 0 ? "more" : "less"} than the current plan`}
              deltaDir={Math.abs(model.kpiDelta) < 0.5 ? "flat" : model.kpiDelta > 0 ? "up" : "down"}
              caption={model.kpi && model.kpi.low !== null && model.kpi.high !== null ? `A ${period} on average. Likely range ${formatValue(model.kpi.low, kpiUnit, dashboard.kpi)} to ${formatValue(model.kpi.high, kpiUnit, dashboard.kpi)}` : `The change in a ${period} against the current plan`}
            />
            <StatTile
              label="Media spend"
              value={formatValue(model.channels.reduce((a, c) => a + c.spend, 0), "currency", { currency: dashboard.kpi.unit === "currency" ? (dashboard.kpi.currency ?? null) : null, decimals: 0 })}
              delta={Math.abs(model.spendDelta) < 0.5 ? null : `${grouped(Math.abs(model.spendDelta), 0)} ${model.spendDelta > 0 ? "more" : "less"} than the current plan`}
              deltaDir="flat"
              caption={`A ${period} on average over ${ready.periods} ${plural(period, ready.periods)}`}
            />
          </StatGrid>
          <Section title="Why the result differs from the plan" description={`The change of each channel's ${noun} in a ${period} against the current plan.`} data-readout="plan-diff">
            {current ? (
              <p className="text-sm text-muted-foreground">Move a slider to compare a plan with the current plan.</p>
            ) : (
              <DivergingBars
                label={`Change in ${noun} by channel`}
                rows={model.channels.map((c) => ({ key: c.name, label: c.label, value: c.delta }))}
                net={{ label: "All media", value: model.kpiDelta }}
              />
            )}
          </Section>
          <div className="grid gap-3">
            <div className="grid gap-1.5 sm:max-w-sm">
              <Label htmlFor={labelId}>Name of the plan (optional)</Label>
              <Input id={labelId} value={label} maxLength={80} onChange={(e) => setLabel(e.target.value)} disabled={preview} />
            </div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={onSave} disabled={preview || pending}>
                Save scenario
              </Button>
              {allowExact ? (
                <Button onClick={onExact} disabled={preview || pending || paused || current}>
                  Get exact result
                </Button>
              ) : null}
            </div>
            {allowExact ? <p className="text-xs text-hint">The exact result uses a new model run. This can take up to two minutes.</p> : null}
            {save.isError ? <InlineMessage tone="error">{dashboardErrorMessage(save.error)}</InlineMessage> : null}
            {saved ? <InlineMessage tone="success">{saved}</InlineMessage> : null}
            {pending && !saved ? <Working>Sending the plan…</Working> : null}
          </div>
          {exactSeq !== null ? (
            <Section title="Exact result" description="The model calculated this plan with a new run." data-readout="exact">
              {exactReadout && isFullReadout(exactReadout) && exactReadout.status === "ready" && exactReadout.snapshot ? (
                <>
                  <p className="text-base text-foreground">{exactReadout.snapshot.lead}</p>
                  <ReadoutTiles dashboard={dashboard} readout={exactReadout} label="Exact result" />
                </>
              ) : exactReadout ? (
                <>
                  <ReadoutState readout={exactReadout} />
                  {exactReadout.status !== "failed" ? <p className="text-xs text-hint">This can take up to two minutes.</p> : null}
                </>
              ) : (
                <Working>We calculate the exact result. This can take up to two minutes.</Working>
              )}
            </Section>
          ) : null}
        </div>
      </div>
    </div>
  );
}
