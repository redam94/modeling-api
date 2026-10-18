/**
 * Check results with their evidence: what was measured in plain words, what to do about a
 * warning or failure, the value against its threshold, and the evidence items (numbers, small
 * tables, plots). The shuffled-result test's scores are drawn as a distribution with the real
 * model's score marked, in the plot sandbox like every chart.
 */
import type { JsonObject, Row } from "@mapi/plot-sandbox/protocol";
import { AlertTriangle, CheckCircle2, CircleSlash, Lightbulb, XCircle } from "lucide-react";
import type { ReactNode } from "react";

import { JsonBlock } from "@/components/common";
import { Badge, Table, TBody, TD, TH, THead, TR } from "@/components/ui/primitives";
import { PlotFrame } from "@/features/plots/plot-frame";
import type { BuiltPlot } from "@/features/plots/builtin-specs";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/utils";

import { CHECK_META, SEVERITY_META, TONE_BADGE } from "./status";
import type { CheckResult, EvidenceItem, GovernanceCheckDef } from "./types";

const HEADING_TAG = { 3: "h3", 4: "h4" } as const;

const STATUS_ICON: Record<CheckResult["status"], ReactNode> = {
  pass: <CheckCircle2 className="size-4 text-success" aria-hidden />,
  warn: <AlertTriangle className="size-4 text-warning" aria-hidden />,
  fail: <XCircle className="size-4 text-destructive" aria-hidden />,
  error: <XCircle className="size-4 text-destructive" aria-hidden />,
  skipped: <CircleSlash className="size-4 text-muted-foreground" aria-hidden />,
};

/** "≤ 0.05 passes" / "≥ 0.05 passes" / "within 0.1 of 0.9" for a check definition. */
export function thresholdText(def: GovernanceCheckDef | undefined, threshold: number | null | undefined): string | null {
  if (!def) return threshold == null ? null : `threshold ${formatNumber(threshold)}`;
  const t = threshold ?? def.warn;
  if (def.direction === "min") return `passes at ${formatNumber(t)} or more`;
  if (def.direction === "band") return `passes within ${formatNumber(t)} of ${formatNumber(def.target ?? 0)}`;
  return `passes at ${formatNumber(t)} or less`;
}

/** A histogram of the shuffled-result scores with the real model's score marked. */
export function nullDistributionPlot(inline: unknown): BuiltPlot | null {
  if (!inline || typeof inline !== "object" || Array.isArray(inline)) return null;
  const v = inline as Record<string, unknown>;
  const shuffled = Array.isArray(v.shuffled) ? v.shuffled.filter((x): x is number => typeof x === "number" && Number.isFinite(x)) : [];
  if (typeof v.real !== "number" || shuffled.length < 2) return null;
  const metric = typeof v.metric === "string" ? v.metric.toUpperCase() : "Error";
  // v0.5 (M3.5 review): time-series scores are a skill (higher is better), named by `axis`
  const axis = typeof v.axis === "string" ? v.axis : `${metric} on held-out data`;
  const rows: Row[] = shuffled.map((score) => ({ score }));
  const spec: JsonObject = {
    height: 180,
    layer: [
      {
        data: { name: "shuffled" },
        mark: { type: "bar", opacity: 0.75, cornerRadiusTopLeft: 2, cornerRadiusTopRight: 2 },
        encoding: {
          x: { field: "score", type: "quantitative", bin: { maxbins: 15 }, title: axis },
          y: { aggregate: "count", type: "quantitative", title: "Shuffled fits" },
          tooltip: [{ aggregate: "count", type: "quantitative", title: "Fits" }],
        },
      },
      {
        data: { name: "real" },
        mark: { type: "rule", strokeWidth: 2.5 },
        encoding: { x: { field: "score", type: "quantitative" }, color: { value: "#c2417a" }, tooltip: [{ field: "score", title: "Your model", format: ".3g" }] },
      },
      {
        data: { name: "real" },
        mark: { type: "text", align: "left", dx: 4, dy: -6, fontWeight: "bold" },
        encoding: { x: { field: "score", type: "quantitative" }, y: { value: 8 }, text: { value: "Your model" }, color: { value: "#c2417a" } },
      },
    ],
  };
  return { spec, datasets: { shuffled: rows, real: [{ score: v.real }] } };
}

function isScalar(v: unknown): v is string | number | boolean | null {
  return v === null || typeof v === "string" || typeof v === "number" || typeof v === "boolean";
}

function cell(v: unknown): string {
  if (typeof v === "number") return formatNumber(v);
  if (v === null || v === undefined) return "—";
  return typeof v === "string" ? v : JSON.stringify(v);
}

/** Small JSON tables as a table: records, a mapping of records, or a flat mapping. */
export function InlineTable({ value, caption }: { value: unknown; caption?: string | null | undefined }) {
  let columns: string[] = [];
  let rows: { key: string; cells: unknown[] }[] = [];
  if (Array.isArray(value) && value.every((r) => r && typeof r === "object" && !Array.isArray(r))) {
    columns = [...new Set(value.flatMap((r) => Object.keys(r as object)))];
    rows = value.map((r, i) => ({ key: String(i), cells: columns.map((c) => (r as Record<string, unknown>)[c]) }));
  } else if (value && typeof value === "object" && !Array.isArray(value)) {
    const entries = Object.entries(value as Record<string, unknown>);
    if (entries.length && entries.every(([, v]) => v && typeof v === "object" && !Array.isArray(v))) {
      columns = ["", ...new Set(entries.flatMap(([, v]) => Object.keys(v as object)))];
      rows = entries.map(([k, v]) => ({ key: k, cells: [k, ...columns.slice(1).map((c) => (v as Record<string, unknown>)[c])] }));
    } else if (entries.every(([, v]) => isScalar(v) || (Array.isArray(v) && v.length <= 12 && v.every(isScalar)))) {
      columns = ["", "Value"];
      rows = entries.map(([k, v]) => ({ key: k, cells: [k, Array.isArray(v) ? v.map(cell).join(", ") : v] }));
    }
  }
  if (!columns.length) return <JsonBlock value={value} maxHeight="14rem" />;
  return (
    <Table>
      {caption ? <caption className="mt-1 text-left text-xs text-muted-foreground">{caption}</caption> : null}
      <THead>
        <TR>
          {columns.map((c, i) => (
            <TH key={`${c}-${i}`}>{c}</TH>
          ))}
        </TR>
      </THead>
      <TBody>
        {rows.slice(0, 200).map((r) => (
          <TR key={r.key}>
            {r.cells.map((v, i) => (
              <TD key={i} className={cn(typeof v === "number" && "tabular text-right", i === 0 && columns[0] === "" && "font-mono text-xs")}>
                {cell(v)}
              </TD>
            ))}
          </TR>
        ))}
      </TBody>
    </Table>
  );
}

export function EvidenceItemView({ item, check }: { item: EvidenceItem; check: string }) {
  const caption = item.caption_plain ?? null;
  if (item.kind === "metric") {
    return (
      <p className="text-[13px]">
        <span className="font-mono text-xs text-muted-foreground">{item.name}</span> <span className="font-medium tabular">{formatNumber(item.value ?? null)}</span>
        {caption ? <span className="block text-xs text-muted-foreground">{caption}</span> : null}
      </p>
    );
  }
  if (item.kind === "text") return <p className="text-[13px] text-muted-foreground">{caption ?? item.name}</p>;
  if (item.kind === "artifact") {
    return (
      <p className="text-[13px]">
        {caption ?? item.name}: <code className="font-mono text-xs">{item.ref}</code>
      </p>
    );
  }
  if (item.kind === "table" && item.name === "permutation_scores") {
    const plot = nullDistributionPlot(item.inline);
    return (
      <figure className="grid gap-2">
        {plot ? <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={`Shuffled-result scores (${check})`} height={220} exportName={`${check}-null-distribution`} /> : null}
        <figcaption className="text-xs text-muted-foreground">{caption ?? "Scores of the fits on shuffled data"}. The real model must be clearly on the better side of the bars.</figcaption>
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground">The numbers</summary>
          <InlineTable value={item.inline} />
        </details>
      </figure>
    );
  }
  if (item.kind === "plot") {
    const inline = item.inline as { spec?: unknown; datasets?: unknown } | null;
    if (inline && typeof inline === "object" && inline.spec && typeof inline.spec === "object") {
      return <PlotFrame kind="vega-lite" spec={inline.spec as JsonObject} datasets={(inline.datasets ?? {}) as Record<string, Row[]>} title={caption ?? item.name} height={240} />;
    }
  }
  return (
    <div className="grid gap-1">
      <p className="font-mono text-xs text-muted-foreground">{item.name}</p>
      <InlineTable value={item.inline} caption={caption} />
    </div>
  );
}

export function CheckResultCard({ result, def, headingLevel = 3 }: { result: CheckResult; def?: GovernanceCheckDef | undefined; headingLevel?: 3 | 4 }) {
  const meta = CHECK_META[result.status];
  const H = HEADING_TAG[headingLevel];
  const threshold = thresholdText(def, result.threshold);
  return (
    <article className={cn("grid gap-2 rounded-lg border bg-card p-3", result.status === "fail" || result.status === "error" ? "border-destructive/40" : result.status === "warn" ? "border-warning/40" : "")} data-check={result.check} data-status={result.status}>
      <header className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 items-start gap-2">
          <span className="mt-0.5">{STATUS_ICON[result.status]}</span>
          <div className="min-w-0">
            <H className="text-[14px] font-semibold">{def?.title ?? result.check}</H>
            <code className="font-mono text-[11px] text-muted-foreground">{result.check}</code>
          </div>
        </div>
        <span className="flex flex-wrap gap-1">
          <Badge variant={TONE_BADGE[meta.tone]}>{meta.label}</Badge>
          <Badge variant="outline" title={SEVERITY_META[result.severity].plain}>
            {SEVERITY_META[result.severity].label}
          </Badge>
        </span>
      </header>
      <p className="text-[13px]">{result.message_plain}</p>
      {result.suggested_action ? (
        <p className="flex items-start gap-1.5 rounded-md bg-muted/50 px-2 py-1.5 text-[13px]">
          <Lightbulb className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden />
          <span>
            <span className="font-medium">What to do: </span>
            {result.suggested_action}
          </span>
        </p>
      ) : null}
      {result.value != null ? (
        <p className="text-xs text-muted-foreground">
          Measured <span className="font-medium text-foreground tabular">{formatNumber(result.value)}</span>
          {result.metric ? ` (${result.metric.replace(/_/g, " ")})` : ""}
          {threshold ? `; ${threshold}` : ""}.
        </p>
      ) : null}
      {result.evidence?.length || result.message_expert ? (
        <details className="group">
          <summary className="cursor-pointer text-xs text-muted-foreground">Evidence{result.evidence?.length ? ` (${result.evidence.length})` : ""}</summary>
          <div className="mt-2 grid gap-3">
            {result.message_expert ? <p className="text-xs text-muted-foreground">{result.message_expert}</p> : null}
            {(result.evidence ?? []).map((e, i) => (
              <EvidenceItemView key={`${e.name}-${i}`} item={e} check={result.check} />
            ))}
          </div>
        </details>
      ) : null}
    </article>
  );
}

/** Results ordered the way a reviewer reads them: failures first, then warnings, passes, skipped. */
export function sortResults(results: readonly CheckResult[]): CheckResult[] {
  const rank: Record<CheckResult["status"], number> = { fail: 0, error: 1, warn: 2, pass: 3, skipped: 4 };
  return [...results].sort((a, b) => rank[a.status] - rank[b.status]);
}

export function CheckResultList({ results, checks }: { results: readonly CheckResult[]; checks: ReadonlyMap<string, GovernanceCheckDef> }) {
  return (
    <div className="grid gap-2">
      {sortResults(results).map((r) => (
        <CheckResultCard key={r.check} result={r} def={checks.get(r.check)} />
      ))}
    </div>
  );
}
