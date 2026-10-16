/**
 * Step 3, "What you believe": ROI or share ranges per channel, optional experiment results, and
 * a sandboxed preview of what the beliefs mean before anything is fitted (a `translate_priors`
 * run: prior ROI / share distributions and a KPI fan, drawn in the plot sandbox).
 */
import { useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { ArrowRight, Eye, FlaskConical, Plus, Trash2 } from "lucide-react";
import { useId, useMemo, useState, type ReactNode } from "react";
import { stringify } from "yaml";

import { can } from "@/api/access";
import { api, errorMessage, newIdempotencyKey, unwrap } from "@/api/client";
import { qk, useProjectScope } from "@/api/queries";
import { ErrorNotice, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Checkbox, SegmentedControl } from "@/components/ui/overlays";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label, NativeSelect } from "@/components/ui/primitives";
import { useGrants } from "@/features/auth/workspace";
import { PlotFrame } from "@/features/plots/plot-frame";

import { usePriorTranslation, useStartPriorTranslation } from "../api";
import { StepActions } from "../frame";
import { band, ratio, share } from "../plain";
import { distributionPlot, fanPlot } from "../plots";
import {
  beliefProblems,
  beliefsKey,
  coverageSentence,
  CREDIBILITY_CHOICES,
  formFromCalibration,
  newLiftRow,
  ROI_LIMITS,
  SHARE_LIMITS,
  toBusinessPriors,
  toLiftTests,
  withCalibration,
  withMode,
  type BeliefMode,
  type ChannelBeliefForm,
  type LiftTestRow,
  type PriorsForm,
} from "../priors";
import { GUIDED_MESSAGE, hasTask, valueAt, wideVarOf } from "../session";
import type { PriorTranslationResult } from "../types";
import { RangeSlider } from "../ui";
import type { GuidedSession } from "../use-session";
import { IngestStatus } from "./data-summary";

function coordValues(session: GuidedSession, dim: string): string[] {
  const c = session.datasetVersion?.summary?.coords?.[dim];
  return (c?.values ?? []).map(String);
}

/** The categorical dim of the data other than the channel dim (markets). */
function geoDim(session: GuidedSession, wideDim: string | null): string | null {
  const dims = session.datasetVersion?.data_schema?.dims ?? {};
  return Object.entries(dims).find(([name, d]) => name !== wideDim && d.kind === "categorical")?.[0] ?? null;
}

function fmt(mode: BeliefMode) {
  return mode === "roi" ? (v: number) => ratio(v) : (v: number) => share(v);
}

function ChannelRow({ row, mode, onChange, error }: { row: ChannelBeliefForm; mode: BeliefMode; onChange: (r: ChannelBeliefForm) => void; error: string | null }) {
  const id = useId();
  const limits = mode === "roi" ? ROI_LIMITS : SHARE_LIMITS;
  const f = fmt(mode);
  const scale = mode === "roi" ? 1 : 100;
  return (
    <li className="grid gap-3 rounded-lg border p-4 md:grid-cols-[10rem_minmax(0,1fr)_auto] md:items-center" data-channel={row.channel}>
      <div className="flex items-center gap-2">
        <Checkbox id={`${id}-on`} checked={row.enabled} onCheckedChange={(v) => onChange({ ...row, enabled: v === true })} />
        <Label htmlFor={`${id}-on`} className="font-semibold">
          {row.channel}
        </Label>
      </div>
      {row.enabled ? (
        <>
          <div className="grid gap-1.5">
            <RangeSlider
              label={mode === "roi" ? `ROI range for ${row.channel}` : `Share range for ${row.channel}`}
              value={[row.low, row.high]}
              min={limits.min}
              max={limits.max}
              step={limits.step}
              format={f}
              onValueChange={([low, high]) => onChange({ ...row, low, high })}
            />
            <p className="text-xs text-muted-foreground" aria-live="polite">
              {mode === "roi" ? `Every 1 spent brings back between ${f(row.low)} and ${f(row.high)}.` : `Between ${f(row.low)} and ${f(row.high)} of your results come from ${row.channel}.`}
            </p>
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="grid gap-1">
              <Label htmlFor={`${id}-lo`} className="text-xs font-normal text-muted-foreground">
                From{mode === "share" ? " (%)" : ""}
              </Label>
              <Input
                id={`${id}-lo`}
                type="number"
                inputMode="decimal"
                step={limits.step * scale}
                value={Math.round(row.low * scale * 100) / 100}
                onChange={(e) => onChange({ ...row, low: Number(e.target.value) / scale })}
                className="h-8 w-20"
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor={`${id}-hi`} className="text-xs font-normal text-muted-foreground">
                To{mode === "share" ? " (%)" : ""}
              </Label>
              <Input
                id={`${id}-hi`}
                type="number"
                inputMode="decimal"
                step={limits.step * scale}
                value={Math.round(row.high * scale * 100) / 100}
                onChange={(e) => onChange({ ...row, high: Number(e.target.value) / scale })}
                className="h-8 w-20"
              />
            </div>
            <div className="grid gap-1">
              <Label htmlFor={`${id}-cred`} className="text-xs font-normal text-muted-foreground">
                How sure?
              </Label>
              <NativeSelect id={`${id}-cred`} value={row.credibility} onChange={(e) => onChange({ ...row, credibility: Number(e.target.value) })} className="h-8 w-44">
                {CREDIBILITY_CHOICES.map((c) => (
                  <option key={c.value} value={c.value}>
                    {c.label}
                  </option>
                ))}
              </NativeSelect>
            </div>
          </div>
        </>
      ) : (
        <p className="text-[13px] text-muted-foreground md:col-span-2">No view on this channel: the model learns it from the data alone.</p>
      )}
      {error ? (
        <p role="alert" className="text-xs font-medium text-destructive md:col-span-3">
          {error}
        </p>
      ) : null}
    </li>
  );
}

function LiftRow({
  row,
  n,
  channels,
  geos,
  onChange,
  onRemove,
  errors,
}: {
  row: LiftTestRow;
  n: number;
  channels: string[];
  geos: string[];
  onChange: (r: LiftTestRow) => void;
  onRemove: () => void;
  errors: Record<string, string>;
}) {
  const id = useId();
  const field = (key: keyof LiftTestRow, label: string, input: ReactNode) => (
    <div className="grid gap-1">
      <Label htmlFor={`${id}-${key}`} className="text-xs font-normal text-muted-foreground">
        {label}
      </Label>
      {input}
      {errors[key] ? (
        <p role="alert" className="text-xs text-destructive">
          {errors[key]}
        </p>
      ) : null}
    </div>
  );
  const text = (key: "spendDelta" | "kpiDelta" | "se", placeholder: string) => (
    <Input id={`${id}-${key}`} inputMode="decimal" value={row[key]} placeholder={placeholder} onChange={(e) => onChange({ ...row, [key]: e.target.value })} className="h-8" aria-invalid={errors[key] ? true : undefined} />
  );
  return (
    <li className="grid gap-3 rounded-lg border p-4" aria-label={`Experiment ${n}`}>
      <div className="flex items-center justify-between">
        <p className="text-[13px] font-semibold">Experiment {n}</p>
        <Button variant="ghost" size="sm" onClick={onRemove} aria-label={`Remove experiment ${n}`}>
          <Trash2 /> Remove
        </Button>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {field(
          "channel",
          "Channel tested",
          <NativeSelect id={`${id}-channel`} value={row.channel} onChange={(e) => onChange({ ...row, channel: e.target.value })} className="h-8" aria-invalid={errors.channel ? true : undefined}>
            <option value="">Choose…</option>
            {channels.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </NativeSelect>,
        )}
        {field(
          "geo",
          "Market",
          <NativeSelect id={`${id}-geo`} value={row.geo} onChange={(e) => onChange({ ...row, geo: e.target.value })} className="h-8">
            <option value="">All markets</option>
            {geos.map((g) => (
              <option key={g} value={g}>
                {g}
              </option>
            ))}
          </NativeSelect>,
        )}
        {field("start", "First day", <Input id={`${id}-start`} type="date" value={row.start} onChange={(e) => onChange({ ...row, start: e.target.value })} className="h-8" aria-invalid={errors.start ? true : undefined} />)}
        {field("end", "Last day", <Input id={`${id}-end`} type="date" value={row.end} onChange={(e) => onChange({ ...row, end: e.target.value })} className="h-8" aria-invalid={errors.end ? true : undefined} />)}
        {field("spendDelta", "Spend change", text("spendDelta", "-120000"))}
        {field("kpiDelta", "Measured change in results", text("kpiDelta", "-260000"))}
        {field("se", "± uncertainty (standard error)", text("se", "70000"))}
      </div>
    </li>
  );
}

function PreviewPanel({ result, form }: { result: PriorTranslationResult; form: PriorsForm }) {
  const preview = result.preview ?? {};
  const stated = new Map(form.channels.filter((c) => c.enabled).map((c) => [c.channel, [c.low, c.high] as [number, number]]));
  const roiItems = Object.entries(preview.roi ?? {}).map(([label, summary]) => ({ label, summary, stated: form.mode === "roi" ? (stated.get(label) ?? null) : null }));
  const shareItems = Object.entries(preview.contribution_share ?? {}).map(([label, summary]) => ({ label, summary, stated: form.mode === "share" ? (stated.get(label) ?? null) : null }));
  const roiPlot = useMemo(() => (roiItems.length ? distributionPlot(roiItems, { axisTitle: "Return per 1 spent", format: ".2~f" }) : null), [result]); // eslint-disable-line react-hooks/exhaustive-deps
  const sharePlot = useMemo(() => (shareItems.length ? distributionPlot(shareItems, { axisTitle: "Share of results", format: ".0%" }) : null), [result]); // eslint-disable-line react-hooks/exhaustive-deps
  const fan = useMemo(() => (preview.kpi_fan ? fanPlot(preview.kpi_fan, { title: "Results the model expects" }) : null), [result]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <section aria-labelledby="preview-h" className="grid gap-4">
      <div className="grid gap-1">
        <h2 id="preview-h" className="text-[15px] font-semibold tracking-tight">
          What your beliefs mean, before seeing the data
        </h2>
        <p className="text-[13px] text-muted-foreground">The shaded bar is the range you gave; the line is where 90% of what the model now believes falls, the thick part the middle half.</p>
      </div>
      <ul className="grid gap-1 text-[13px]">
        {form.channels
          .filter((c) => c.enabled)
          .map((c) => {
            const s = coverageSentence(result.coverage?.[c.channel], c.credibility);
            return s ? (
              <li key={c.channel}>
                <span className="font-medium">{c.channel}:</span> {s}
              </li>
            ) : null;
          })}
      </ul>
      <div className="grid gap-4 xl:grid-cols-2">
        {roiPlot ? <PlotFrame kind="vega-lite" spec={roiPlot.spec} datasets={roiPlot.datasets} title="Prior return on spend per channel" height={60 + roiItems.length * 36} exportName="prior-roi" /> : null}
        {sharePlot ? <PlotFrame kind="vega-lite" spec={sharePlot.spec} datasets={sharePlot.datasets} title="Prior share of results per channel" height={60 + shareItems.length * 36} exportName="prior-share" /> : null}
        {fan ? (
          <figure className="grid gap-1 xl:col-span-2">
            <PlotFrame kind="vega-lite" spec={fan.spec} datasets={fan.datasets} title="Results the model expects before fitting" height={300} exportName="prior-kpi-fan" />
            <figcaption className="text-xs text-muted-foreground">If the band is far from your usual results, a belief may be off. Fitting then lets the data correct it.</figcaption>
          </figure>
        ) : null}
      </div>
      {preview.baseline_share ? (
        <p className="text-[13px] text-muted-foreground">
          Results without any media: {(() => {
            const b = band(preview.baseline_share.quantiles);
            return b ? `between ${share(b[0])} and ${share(b[1])}` : share(preview.baseline_share.mean);
          })()}
          .
        </p>
      ) : null}
    </section>
  );
}

export function PriorsStep({ session }: { session: GuidedSession }) {
  const wide = wideVarOf(session.template);
  const channels = wide ? coordValues(session, wide.dim) : [];
  if (session.datasetVersion?.status !== "ready" || !channels.length) {
    return (
      <div className="grid gap-5">
        <IngestStatus version={session.datasetVersion} />
        {session.datasetVersion?.status === "ready" ? <p className="text-[13px] text-muted-foreground">This data has no channels to state beliefs about.</p> : null}
      </div>
    );
  }
  return <BeliefsEditor key={`${session.latest.revision}:${channels.join(",")}`} session={session} channels={channels} geos={coordValues(session, geoDim(session, wide?.dim ?? null) ?? "")} />;
}

function BeliefsEditor({ session, channels, geos }: { session: GuidedSession; channels: string[]; geos: string[] }) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const scope = useProjectScope();
  const grants = useGrants();
  const calibration = valueAt(session.document, "/calibration");
  const [form, setForm] = useState<PriorsForm>(() => formFromCalibration(calibration, channels));
  const savedRun = typeof (calibration as { prior_translation_run?: unknown } | undefined)?.prior_translation_run === "string" ? (calibration as { prior_translation_run: string }).prior_translation_run : null;
  const [runId, setRunId] = useState<string | null>(savedRun);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const start = useStartPriorTranslation();
  const translation = usePriorTranslation(runId);
  const lifts = hasTask(session.template, "lift_tests");

  const bp = toBusinessPriors(form, { kpi: null, spend: null });
  const problems = beliefProblems(form);
  const lift = toLiftTests(form.liftTests, channels);
  const key = beliefsKey(bp, lift.tests);
  const translationFits = translation.data?.done && translation.data.result && (previewKey === key || (previewKey === null && runId === savedRun));
  const blocking = problems.length > 0 || lift.problems.length > 0;
  const canSave = can(grants, "update", "spec");
  const canPreview = can(grants, "create", "run");

  const update = (i: number, r: ChannelBeliefForm) => setForm((f) => ({ ...f, channels: f.channels.map((c, j) => (j === i ? r : c)) }));
  const updateLift = (k: string, r: LiftTestRow) => setForm((f) => ({ ...f, liftTests: f.liftTests.map((x) => (x.key === k ? r : x)) }));

  const preview = async () => {
    if (!bp) return;
    setError(null);
    try {
      const res = await start.mutateAsync({
        key: newIdempotencyKey(),
        body: { spec_id: session.spec.id, revision: session.latest.revision, business_priors: bp, lift_tests: lift.tests, draws: 500 },
      });
      setPreviewKey(key);
      setRunId(res.run.id);
    } catch (e) {
      setError(errorMessage(e));
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const translated = translationFits && translation.data?.result && runId ? { result: translation.data.result, runId } : null;
      const doc = withCalibration(session.document, bp, lift.tests, translated);
      const changed = JSON.stringify(doc) !== JSON.stringify(session.document);
      if (changed) {
        await unwrap(
          api.POST("/v1/specs/{spec_id}/revisions", {
            params: { path: { spec_id: session.spec.id } },
            body: { yaml: stringify(doc), parent_revision: session.latest.revision, message: `${GUIDED_MESSAGE} ${bp ? "business priors" : "no business priors"}${lift.tests.length ? ` and ${lift.tests.length} lift test${lift.tests.length === 1 ? "" : "s"}` : ""}` },
            headers: scope.headers,
          }),
        );
        await Promise.all([qc.invalidateQueries({ queryKey: qk.spec(scope.key, session.spec.id) }), qc.invalidateQueries({ queryKey: qk.specRevisions(scope.key, session.spec.id) })]);
      }
      void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "run" } });
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const channelError = (c: string) => problems.find((p) => p.where === `channels/${c}`)?.message ?? null;
  const liftErrors = (k: string) =>
    Object.fromEntries(lift.problems.filter((p) => p.where.startsWith(`lift/${k}/`)).map((p) => [p.where.split("/")[2] ?? "", p.message.replace(/^Experiment \d+: /, "").replace(/^./, (c) => c.toUpperCase())]));

  return (
    <div className="grid gap-6">
      <Card>
        <CardHeader>
          <div className="grid gap-1">
            <CardTitle>Your channels</CardTitle>
            <CardDescription>
              Tell the model what you already believe, as a range you are fairly sure of. Leave a channel unticked if you have no view; the data decides it alone. Everything here is optional.
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className="grid gap-4">
          <div className="grid gap-1.5">
            <span className="text-[13px] font-medium">Describe each channel by</span>
            <SegmentedControl<BeliefMode>
              label="Describe each channel by"
              value={form.mode}
              onValueChange={(m) => setForm((f) => withMode(f, m))}
              options={[
                { value: "roi", label: "Return on spend" },
                { value: "share", label: "Share of results" },
              ]}
              className="justify-self-start"
            />
          </div>
          <ul className="grid gap-3" aria-label="Beliefs per channel">
            {form.channels.map((row, i) => (
              <ChannelRow key={row.channel} row={row} mode={form.mode} onChange={(r) => update(i, r)} error={channelError(row.channel)} />
            ))}
          </ul>
          <details className="rounded-lg border px-4 py-3" open={form.baseline.enabled}>
            <summary className="cursor-pointer text-[13px] font-medium">Results without any media (optional)</summary>
            <div className="grid gap-3 pt-3">
              <div className="flex items-center gap-2">
                <Checkbox id="baseline-on" checked={form.baseline.enabled} onCheckedChange={(v) => setForm((f) => ({ ...f, baseline: { ...f.baseline, enabled: v === true } }))} />
                <Label htmlFor="baseline-on">I have a view on the share of results that happen without any media</Label>
              </div>
              {form.baseline.enabled ? (
                <div className="grid gap-1.5">
                  <RangeSlider label="Baseline share" value={[form.baseline.low, form.baseline.high]} min={0} max={1} step={0.01} format={(v) => share(v)} onValueChange={([low, high]) => setForm((f) => ({ ...f, baseline: { ...f.baseline, low, high } }))} />
                  <p className="text-xs text-muted-foreground">
                    Between {share(form.baseline.low)} and {share(form.baseline.high)} of results happen anyway.
                  </p>
                  {problems.find((p) => p.where === "baseline") ? (
                    <p role="alert" className="text-xs font-medium text-destructive">
                      {problems.find((p) => p.where === "baseline")?.message}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          </details>
          {problems.filter((p) => p.where === "").map((p) => (
            <p key={p.message} role="alert" className="text-[13px] font-medium text-destructive">
              {p.message}
            </p>
          ))}
        </CardContent>
      </Card>

      {lifts ? (
        <Card>
          <CardHeader>
            <div className="grid gap-1">
              <CardTitle>
                <span className="inline-flex items-center gap-2">
                  <FlaskConical aria-hidden className="size-4 text-muted-foreground" /> Experiments you have run (optional)
                </span>
              </CardTitle>
              <CardDescription>
                A lift test or geo holdout measured what a channel really adds: for example, pausing TV in one market for four weeks. The model treats each result as extra evidence.
              </CardDescription>
            </div>
          </CardHeader>
          <CardContent className="grid gap-3">
            {form.liftTests.length ? (
              <ul className="grid gap-3">
                {form.liftTests.map((r, i) => (
                  <LiftRow
                    key={r.key}
                    row={r}
                    n={i + 1}
                    channels={channels}
                    geos={geos}
                    errors={liftErrors(r.key)}
                    onChange={(x) => updateLift(r.key, x)}
                    onRemove={() => setForm((f) => ({ ...f, liftTests: f.liftTests.filter((x) => x.key !== r.key) }))}
                  />
                ))}
              </ul>
            ) : null}
            <Button variant="outline" size="sm" className="justify-self-start" onClick={() => setForm((f) => ({ ...f, liftTests: [...f.liftTests, newLiftRow(channels[0] ?? "")] }))}>
              <Plus /> Add an experiment
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardContent className="grid gap-4 pt-5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="max-w-[60ch] text-[13px] text-muted-foreground">
              See what your beliefs imply before fitting: a quick, secure simulation of the results they predict.
            </p>
            <Button variant="outline" onClick={() => void preview()} disabled={!bp || blocking || start.isPending || !canPreview}>
              {start.isPending || (translation.data && !translation.data.done) ? <Spinner /> : <Eye />} Preview what this means
            </Button>
          </div>
          {translation.data && !translation.data.done ? (
            <p className="flex items-center gap-2 text-[13px]" aria-live="polite">
              <Spinner /> Simulating what your beliefs imply…
            </p>
          ) : null}
          {translation.isError ? <ErrorNotice error={translation.error} title="The preview could not be loaded" /> : null}
          {translation.data?.done && translation.data.run.state !== "succeeded" ? (
            <ErrorNotice error={new Error(translation.data.run.problem?.detail ?? "The preview run did not finish.")} title="The preview did not work" />
          ) : null}
          {translation.data?.done && translation.data.result ? (
            <>
              {!translationFits ? <p className="text-xs text-warning">You changed your beliefs since this preview. Preview again to see the new ones.</p> : null}
              <PreviewPanel result={translation.data.result} form={form} />
            </>
          ) : null}
        </CardContent>
      </Card>

      {error ? (
        <p role="alert" className="text-[13px] text-destructive">
          {error}
        </p>
      ) : null}
      <StepActions>
        <Button variant="ghost" onClick={() => void navigate({ to: "/guided/$specId/$step", params: { specId: session.spec.id, step: "data" } })}>
          Back
        </Button>
        <div className="flex flex-wrap items-center gap-3">
          {!canSave ? <span className="text-xs text-muted-foreground">Your role cannot change this analysis.</span> : null}
          <Button onClick={() => void save()} disabled={blocking || saving || !canSave}>
            {saving ? <Spinner /> : <ArrowRight />} {bp || lift.tests.length ? "Save and continue" : "Continue without beliefs"}
          </Button>
        </div>
      </StepActions>
    </div>
  );
}
