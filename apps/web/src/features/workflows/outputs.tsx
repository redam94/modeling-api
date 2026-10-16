/**
 * Node results and outputs in readable form: test verdicts, tables, scores, train/test plans,
 * reports, governance evidence, plots (in the sandbox) and file outputs (signed links).
 * Anything unrecognised is shown as JSON text, never interpreted.
 */
import type { JsonObject, Row } from "@mapi/plot-sandbox/protocol";
import { Download, FileJson } from "lucide-react";
import { useState } from "react";

import { errorMessage } from "@/api/client";
import { useProjectScope } from "@/api/queries";
import { JsonBlock, KeyValues, Mono } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { CheckResultList, InlineTable } from "@/features/governance/evidence";
import { useGovernanceChecks } from "@/features/governance/api";
import type { CheckResult } from "@/features/governance/types";
import { PlotFrame } from "@/features/plots/plot-frame";
import { fetchArtifactBytes } from "@/lib/artifacts";
import { saveBytes } from "@/lib/download";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

import { nodeOutputLink } from "./api";
import { PORT_TYPE_META } from "./ports";
import type { NodeOutputRef } from "./types";

type Obj = Record<string, unknown>;

function obj(v: unknown): Obj | null {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Obj) : null;
}

/** A statistical test's verdict (`mapi_spec.stattests.StatTestResult`). */
export function TestVerdict({ result }: { result: Obj }) {
  const verdict = typeof result.verdict_plain === "string" ? result.verdict_plain : null;
  const perSeries = Array.isArray(result.per_series) ? result.per_series : [];
  return (
    <div className="grid gap-2" data-testid="test-verdict">
      {verdict ? <p className="text-[14px] font-medium">{verdict}</p> : null}
      {typeof result.null_plain === "string" ? <p className="text-xs text-muted-foreground">The question it asks: {result.null_plain}</p> : null}
      <KeyValues
        items={[
          ["Decision", typeof result.decision === "string" ? <Badge variant={result.reject ? "info" : "secondary"}>{result.decision.replace(/_/g, " ")}</Badge> : null],
          ["p-value", typeof result.p_value === "number" ? <span className="tabular">{formatNumber(result.p_value)}</span> : null],
          ["Tested at", typeof result.alpha === "number" ? <span className="tabular">{result.alpha}</span> : null],
          ["Statistic", typeof result.statistic === "number" ? <span className="tabular">{formatNumber(result.statistic)}</span> : null],
          ["Observations", typeof result.n_obs === "number" ? result.n_obs : null],
          ...(typeof result.lags === "number" ? ([["Lags", result.lags]] as [string, number][]) : []),
        ]}
      />
      {perSeries.length ? (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">Per series ({perSeries.length})</summary>
          <InlineTable value={perSeries} />
        </details>
      ) : null}
      {obj(result.critical_values) && Object.keys(obj(result.critical_values)!).length ? (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">Critical values</summary>
          <InlineTable value={result.critical_values} />
        </details>
      ) : null}
    </div>
  );
}

function TablePayload({ value }: { value: Obj }) {
  const columns = Array.isArray(value.columns) ? [...new Set(value.columns.map(String))] : [];
  const rows = Array.isArray(value.rows) ? (value.rows as Obj[]) : [];
  const shaped = rows.map((r) => Object.fromEntries((columns.length ? columns : Object.keys(r)).map((c) => [c, r[c]])));
  return (
    <div className="grid gap-1">
      {typeof value.title === "string" && value.title ? <p className="text-[13px] font-medium">{value.title}</p> : null}
      <InlineTable value={shaped} />
    </div>
  );
}

function MetricsPayload({ value }: { value: Obj }) {
  const candidates = Array.isArray(value.candidates) ? (value.candidates as Obj[]) : [];
  const metrics = [...new Set(candidates.flatMap((c) => Object.keys(obj(c.metrics) ?? {})))];
  const rows = candidates.map((c) => ({ name: c.name, kind: c.kind, folds: c.n_folds, ...Object.fromEntries(metrics.map((m) => [m, obj(c.metrics)?.[m]])) }));
  return (
    <div className="grid gap-1">
      {typeof value.note_plain === "string" ? <p className="text-xs text-muted-foreground">{value.note_plain}</p> : null}
      <InlineTable value={rows} />
    </div>
  );
}

function SplitPlanPayload({ value }: { value: Obj }) {
  const folds = Array.isArray(value.folds) ? value.folds : [];
  return (
    <div className="grid gap-1">
      <p className="text-[13px]">
        {folds.length} fold{folds.length === 1 ? "" : "s"}
        {typeof value.n === "number" ? ` over ${value.n} periods` : ""}
        {typeof value.dim === "string" ? ` along ${value.dim}` : ""}.
      </p>
      <InlineTable value={folds} />
    </div>
  );
}

function ReportPayload({ value }: { value: Obj }) {
  const sections = Array.isArray(value.sections) ? (value.sections as Obj[]) : [];
  return (
    <article className="grid gap-3" data-testid="workflow-report">
      <header>
        <h4 className="text-[15px] font-semibold">{typeof value.title === "string" ? value.title : "Report"}</h4>
        {typeof value.summary_plain === "string" ? <p className="text-[13px]">{value.summary_plain}</p> : null}
      </header>
      <ol className="grid gap-2">
        {sections.map((s, i) => (
          <li key={`${String(s.node_id)}-${i}`} className="grid gap-1 rounded-md border p-2">
            <p className="flex flex-wrap items-center gap-2 text-[13px] font-medium">
              {String(s.title)}
              {typeof s.status === "string" ? <Badge variant="outline">{s.status.replace(/_/g, " ")}</Badge> : null}
            </p>
            {typeof s.text === "string" && s.text ? <p className="text-[13px] text-muted-foreground">{s.text}</p> : null}
            {obj(s.table) ? <TablePayload value={obj(s.table)!} /> : null}
          </li>
        ))}
      </ol>
    </article>
  );
}

function EvidencePayload({ value }: { value: Obj }) {
  const checks = useGovernanceChecks();
  const results = Array.isArray(value.results) ? (value.results as unknown as CheckResult[]) : [];
  return (
    <div className="grid gap-2">
      {typeof value.message_plain === "string" ? <p className="text-[13px] font-medium">{value.message_plain}</p> : null}
      <CheckResultList results={results} checks={checks.map} />
    </div>
  );
}

function PlotPayload({ value, title }: { value: Obj; title: string }) {
  const spec = obj(value.spec);
  const datasets = obj(value.datasets) ?? {};
  const inline = Object.values(datasets).every((d) => Array.isArray(d));
  if (!spec || !inline) return <p className="text-[13px] text-muted-foreground">This chart's data is stored as a file: use the download link.</p>;
  return <PlotFrame kind={value.kind === "plotly" ? "plotly" : "vega-lite"} spec={spec as JsonObject} datasets={datasets as Record<string, Row[]>} title={title} height={260} />;
}

/** One inline value of a known payload format, or JSON text. */
export function PayloadView({ value, title }: { value: unknown; title: string }) {
  const o = obj(value);
  if (!o) return <JsonBlock value={value} maxHeight="16rem" />;
  switch (o.format) {
    case "mapi.table/v1":
      return <TablePayload value={o} />;
    case "mapi.metrics/v1":
      return <MetricsPayload value={o} />;
    case "mapi.split-plan/v1":
      return <SplitPlanPayload value={o} />;
    case "mapi.report/v1":
      return <ReportPayload value={o} />;
    case "mapi.evidence/v1":
      return <EvidencePayload value={o} />;
    default:
      if (typeof o.verdict_plain === "string" && typeof o.test === "string") return <TestVerdict result={o} />;
      if (o.spec && (o.kind === "vega-lite" || o.kind === "plotly")) return <PlotPayload value={o} title={title} />;
      return <JsonBlock value={value} maxHeight="16rem" />;
  }
}

/** A node output: inline values rendered, references shown, files fetched by a signed link. */
export function OutputView({ runId, nodeId, port, out }: { runId: string; nodeId: string; port: string; out: NodeOutputRef }) {
  const scope = useProjectScope();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const meta = PORT_TYPE_META[out.type];
  const fetchLink = async () => {
    setBusy(true);
    setError(null);
    try {
      const link = await nodeOutputLink(scope, runId, nodeId, port);
      // Saved from a Blob so the app stays where it is (a cross-origin download would navigate).
      const bytes = await fetchArtifactBytes(link);
      saveBytes(bytes, link.name.split("/").at(-1) ?? `${nodeId}-${port}`);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const hasInline = out.inline !== undefined && out.inline !== null;
  return (
    <section className="grid gap-2 rounded-md border p-3" aria-label={`Output ${port}`}>
      <header className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <Mono className="font-medium">{port}</Mono>
          <Badge variant="outline">
            <span aria-hidden className="size-2 rounded-full" style={{ background: meta.color }} />
            {meta.plain}
          </Badge>
        </span>
        {out.object || (!hasInline && !out.ref) ? (
          <Button variant="outline" size="sm" onClick={() => void fetchLink()} disabled={busy}>
            <Download /> Download
          </Button>
        ) : null}
      </header>
      {out.ref ? (
        <p className="text-[13px]">
          <FileJson className="mr-1 inline size-3.5 text-muted-foreground" aria-hidden />
          <Mono>{out.ref}</Mono>
        </p>
      ) : null}
      {hasInline ? <PayloadView value={out.inline} title={`${nodeId}.${port}`} /> : null}
      {error ? (
        <p role="alert" className={cn("text-xs text-destructive")}>
          {error}
        </p>
      ) : null}
    </section>
  );
}
