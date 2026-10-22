/**
 * What an input gave back, as the server measured it (the web recomputes nothing, plan §3.3):
 * the predictions of tried cases, the summary of a scored file with its download, and the
 * accuracy of uploaded actuals with the history over published readouts. Loaded lazily with
 * the heavy panels (`readout-extra`).
 */
import { Download } from "lucide-react";

import { InlineMessage, StatGrid, StatTile } from "@/components/ods";
import { fixed, grouped, MISSING } from "@/components/ods/format";
import { Bars, Calibration, ConfusionGrid, MultiLine, SmallTable } from "@/components/readout-charts-extra";
import { Button } from "@/components/ui/button";

import { dashboardErrorMessage, problemOf, useDownloadPredictions } from "../../client";
import type { AccuracyDetail, AccuracyPoint, BatchSummary, ClientDashboard, DashboardClient, PredictionResult, Readout } from "../../types";
import { asClientV2, plainNumber, unitText } from "../panel-model";
import { accuracyDetailLines, accuracyMeasures, bandNote, batchLines, classWords, HISTORY_METRIC_LABEL, historyMetric, predictionLines, predictionsFileName } from "./results-model";
import { Section } from "../parts";

/** Save a downloaded Blob as a file (a same-origin Blob URL; the type stays CSV text). */
function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(new Blob([blob], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename.replace(/[^A-Za-z0-9_.-]+/g, "_");
  a.rel = "noopener";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function outputLabel(dashboard: ClientDashboard, name: string): string {
  return dashboard.subject && dashboard.subject.variable === name ? dashboard.subject.label : name;
}

function inputText(v: string | number | boolean | null | undefined): string {
  if (v === null || v === undefined || v === "") return MISSING;
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (typeof v === "number") return plainNumber(v);
  return v;
}

/** The predictions of tried cases: one card per case, the values it used and what the model gives. */
export function PredictionView({ dashboard, result, labels }: { dashboard: ClientDashboard; result: PredictionResult; labels?: Record<string, string> }) {
  const rows = result.rows ?? [];
  const note = bandNote(result.band);
  return (
    <div className="grid gap-4" data-readout="prediction">
      {rows.map((row, i) => (
        <div key={i} className="grid gap-3 rounded-lg border p-4" data-case={i + 1}>
          <h3 className="text-sm font-semibold text-foreground">{rows.length > 1 ? `Case ${i + 1}` : "Your case"}</h3>
          <StatGrid role="list" aria-label={rows.length > 1 ? `Predictions of case ${i + 1}` : "Predictions"}>
            {predictionLines(result, i, dashboard).map((l) => (
              <div role="listitem" key={l.output} className="min-w-0">
                <StatTile
                  label={outputLabel(dashboard, l.output)}
                  value={l.label ? l.label : l.centre}
                  caption={[l.label && l.probability ? `Probability ${l.probability}.` : null, l.range ? `${l.range}.` : null].filter(Boolean).join(" ")}
                  className="h-full"
                />
              </div>
            ))}
          </StatGrid>
          {Object.keys(row.inputs ?? {}).length ? (
            <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-6 gap-y-1 text-xs">
              {Object.entries(row.inputs ?? {}).map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-hint">{labels?.[k] ?? k}</dt>
                  <dd className="text-foreground tabular">{inputText(v)}</dd>
                </div>
              ))}
            </dl>
          ) : null}
        </div>
      ))}
      {note ? <p className="text-xs text-hint">{note}</p> : null}
      {result.approximate ? <p className="text-xs text-hint">Some values are approximate: the model gives one value per case without a range.</p> : null}
    </div>
  );
}

/**
 * Whether a failed download means the predictions expired (`410 DASHBOARD_DOWNLOAD_GONE`). Any other
 * 410 (a share link that was revoked or ended: `LINK_EXPIRED`) is not: its own message says why.
 */
export function downloadGone(err: unknown): boolean {
  const p = problemOf(err);
  return p?.status === 410 && p.problem?.code === "DASHBOARD_DOWNLOAD_GONE";
}

/** The summary of a scored file, and "Download predictions" for the viewer who sent it. */
export function BatchView({ client, dashboard, readout, summary }: { client: DashboardClient; dashboard: ClientDashboard; readout: Pick<Readout, "seq">; summary: BatchSummary }) {
  const v2 = asClientV2(client);
  const download = useDownloadPredictions(v2 ?? (client as never));
  const unit = dashboard.subject?.unit ?? dashboard.kpi.unit;
  const preview = summary.preview ?? [];
  const cols = preview.length ? Object.keys(preview[0] ?? {}) : [];
  const gone = download.isError && downloadGone(download.error);
  const save = () =>
    download.mutate(readout.seq, {
      onSuccess: (blob) => saveBlob(blob, predictionsFileName(dashboard.title, readout.seq)),
    });
  return (
    <div className="grid gap-4" data-readout="batch">
      <ul className="grid gap-1 text-sm text-foreground">
        {batchLines(summary, dashboard).map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
      {summary.histogram?.length ? (
        <Bars
          label="How the predictions spread"
          rows={summary.histogram.map((h) => ({ label: `${unitText(h.lo, unit, dashboard)} to ${unitText(h.hi, unit, dashboard)}`, value: h.count }))}
          format={(v) => grouped(v, 0)}
        />
      ) : null}
      {summary.class_counts?.length ? <Bars label="The most probable result of each case" rows={summary.class_counts.map((c) => ({ label: c.label, value: c.count }))} format={(v) => grouped(v, 0)} /> : null}
      {preview.length ? <SmallTable label="The first cases of the file" columns={cols} rows={preview.map((r) => cols.map((c) => r[c] ?? null))} /> : null}
      {v2 && summary.download?.available ? (
        <div className="grid gap-2">
          <div>
            <Button variant="secondary" onClick={save} disabled={download.isPending}>
              <Download /> Download predictions (.csv)
            </Button>
          </div>
          <p className="text-xs text-hint">
            {grouped(summary.download.rows ?? summary.n_scored)} rows. Only you and the analysts can download this file.
          </p>
        </div>
      ) : null}
      {gone ? <InlineMessage tone="warning">The predictions are no longer kept. Score the file again to get them.</InlineMessage> : download.isError ? <InlineMessage tone="error">{dashboardErrorMessage(download.error)}</InlineMessage> : null}
    </div>
  );
}

/** The accuracy of one upload (the server's measures) with its charts. */
export function AccuracyDetailView({ dashboard, detail }: { dashboard: ClientDashboard; detail: AccuracyDetail }) {
  const measures = accuracyMeasures(detail, dashboard);
  return (
    <div className="grid gap-4" data-readout="accuracy" data-kind={detail.kind}>
      <ul className="grid gap-1 text-sm text-foreground">
        {accuracyDetailLines(detail, dashboard).map((l) => (
          <li key={l}>{l}</li>
        ))}
      </ul>
      {measures.length ? (
        <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-6 gap-y-1 text-xs" aria-label="Measures">
          {measures.map((m) => (
            <div key={m.label} className="contents">
              <dt className="text-hint">{m.label}</dt>
              <dd className="text-foreground tabular">{m.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {detail.kind === "classification" && detail.bins?.length ? <Calibration label="Are the probabilities right?" bins={detail.bins} /> : null}
      {detail.kind === "classification" && detail.confusion?.labels?.length ? (
        <ConfusionGrid label="Actual and predicted results" labels={classWords(detail.confusion.labels)} counts={detail.confusion.counts ?? []} />
      ) : null}
    </div>
  );
}

/** The accuracy over the published actuals readouts (oldest first). */
export function AccuracyHistoryView({ points }: { points: readonly AccuracyPoint[] }) {
  const metric = historyMetric(points);
  if (!metric || points.length < 2) return null;
  const label = HISTORY_METRIC_LABEL[metric] ?? metric;
  // The server gives these in percent already; accuracy is a share (0..1).
  const inPercent = ["mape", "wape", "smape", "band_hit_rate", "effect_relative"].includes(metric);
  const share = metric === "accuracy";
  return (
    <MultiLine
      label={`${label} over time`}
      series={[{ label, points: points.map((p) => ({ t: p.created_at.slice(0, 10), mean: p.values?.[metric] ?? null })) }]}
      format={inPercent ? (v) => `${fixed(v, 1)}%` : share ? (v) => `${fixed(v * 100, 1)}%` : plainNumber}
    />
  );
}

/** The analyst's labels of the inputs (name → label), so a tried case never shows raw names. */
export function inputLabels(dashboard: Pick<ClientDashboard, "scenario_form">): Record<string, string> {
  const form = dashboard.scenario_form;
  const out: Record<string, string> = Object.fromEntries((form?.inputs ?? []).map((i) => [i.name, i.label]));
  if (form?.horizon && !("horizon" in out)) out.horizon = "Periods ahead";
  return out;
}

/** Accuracy, prediction and batch results of a readout snapshot, each when it has one. */
export function ReadoutResults({ client, dashboard, readout }: { client: DashboardClient; dashboard: ClientDashboard; readout: Readout }) {
  const snap = readout.snapshot;
  if (!snap) return null;
  return (
    <>
      {snap.prediction ? <PredictionView dashboard={dashboard} result={snap.prediction} labels={inputLabels(dashboard)} /> : null}
      {snap.batch ? <BatchView client={client} dashboard={dashboard} readout={readout} summary={snap.batch} /> : null}
      {snap.accuracy_detail ? <AccuracyDetailView dashboard={dashboard} detail={snap.accuracy_detail} /> : null}
    </>
  );
}

/** "How accurate the model is": the latest upload's accuracy and the history. */
export function AccuracySection({ dashboard, detail, history }: { dashboard: ClientDashboard; detail: AccuracyDetail | null; history: readonly AccuracyPoint[] }) {
  if (!detail && history.length < 2) return null;
  return (
    <Section title="How accurate the model is" description="From the actual numbers sent before. The analyst publishes each upload.">
      {detail ? <AccuracyDetailView dashboard={dashboard} detail={detail} /> : null}
      <AccuracyHistoryView points={history} />
    </Section>
  );
}
