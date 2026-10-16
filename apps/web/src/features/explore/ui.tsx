/**
 * Building blocks shared by the Data Explorer, the Model Explorer and the comparison page:
 * the plain-first layout (Guided shows the plain summary, Expert every statistic), small
 * statistic tables, pickers, plot panels and number formatting. View strings come from user
 * data and are rendered as text only.
 */
import { AlertTriangle, ChevronDown, Info } from "lucide-react";
import { useId, useMemo, type ReactNode } from "react";

import type { BuiltPlot } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, NativeSelect } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import type { StatTestResult } from "./views";

// ------------------------------------------------------------------------------ numbers

const fixed = (digits: number) => new Intl.NumberFormat("en", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 2 });

/** A statistic: 4 significant digits, compact for large values, "—" when missing. */
export function num(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e5) return compact.format(v);
  if (abs !== 0 && abs < 1e-3) return v.toExponential(2);
  return Number(v.toPrecision(digits)).toLocaleString("en", { maximumFractionDigits: 6 });
}

export function pct(v: number | null | undefined, digits = 0): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `${fixed(digits).format(v * 100)}%`;
}

/** p-values: "< 0.001" for tiny ones. */
export function pValue(p: number | null | undefined): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return "—";
  return p < 0.001 ? "< 0.001" : fixed(3).format(p);
}

// ------------------------------------------------------------------------------ layout

/**
 * The plain-language part of a section: always visible, first. `tone` colours the left rule
 * (what the reader should take away: fine, look closer, act).
 */
export function PlainSummary({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "good" | "warn" | "bad" }) {
  return (
    <div
      className={cn(
        "grid gap-1.5 rounded-lg border-l-[3px] bg-muted/30 px-4 py-3 text-[14px] leading-relaxed",
        tone === "neutral" && "border-l-primary/60",
        tone === "good" && "border-l-success",
        tone === "warn" && "border-l-warning",
        tone === "bad" && "border-l-destructive",
      )}
      data-testid="plain-summary"
    >
      {children}
    </div>
  );
}

/**
 * The statistics behind a plain summary: open in Expert mode, one click away in Guided mode
 * (keyboard: the summary is a native disclosure).
 */
export function Statistics({ expert, children, label = "Show the statistics" }: { expert: boolean; children: ReactNode; label?: string }) {
  if (expert) return <div className="grid gap-4">{children}</div>;
  return (
    <details className="group rounded-lg border">
      <summary className="flex cursor-pointer items-center gap-2 px-4 py-2.5 text-[13px] font-medium select-none hover:bg-accent/40">
        <ChevronDown aria-hidden className="size-4 text-muted-foreground transition-transform group-open:rotate-180" /> {label}
      </summary>
      <div className="grid gap-4 border-t p-4">{children}</div>
    </details>
  );
}

/** A view's caption and its plain warnings. */
export function ViewNotes({ caption, warnings }: { caption?: string | null | undefined; warnings?: readonly string[] | undefined }) {
  if (!caption && !warnings?.length) return null;
  return (
    <div className="grid gap-1.5 text-[13px]">
      {caption ? (
        <p className="flex items-start gap-1.5 text-muted-foreground">
          <Info aria-hidden className="mt-0.5 size-3.5 shrink-0" /> {caption}
        </p>
      ) : null}
      {warnings?.length ? (
        <ul className="grid gap-1" aria-label="Warnings">
          {warnings.map((w) => (
            <li key={w} className="flex items-start gap-1.5">
              <AlertTriangle aria-hidden className="mt-0.5 size-3.5 shrink-0 text-warning" /> {w}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** A card around one section. */
export function SectionCard({ title, description, actions, children, id }: { title: string; description?: ReactNode; actions?: ReactNode; children: ReactNode; id?: string }) {
  return (
    <Card id={id}>
      <CardHeader>
        <div className="grid min-w-0 gap-1">
          <CardTitle>{title}</CardTitle>
          {description ? <CardDescription>{description}</CardDescription> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center justify-end gap-2">{actions}</div> : null}
      </CardHeader>
      <CardContent className="grid gap-5">{children}</CardContent>
    </Card>
  );
}

/** A plot in the sandbox with its accessible title and an optional caption. */
export function ExplorerPlot({ plot, title, caption, height, exportName }: { plot: BuiltPlot | null; title: string; caption?: ReactNode; height?: number; exportName?: string }) {
  if (!plot) return null;
  return (
    <div className="grid gap-1.5">
      <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={title} height={height ?? 300} exportName={exportName ?? title.toLowerCase().replace(/[^a-z0-9]+/g, "-")} />
      {caption ? <p className="text-xs text-muted-foreground">{caption}</p> : null}
    </div>
  );
}

// ------------------------------------------------------------------------------ tables

export interface Column<T> {
  key: string;
  header: ReactNode;
  cell: (row: T) => ReactNode;
  /** Numbers right-aligned in tabular figures. */
  numeric?: boolean;
  /** For sorting: the value to compare. */
  sortValue?: (row: T) => number | string | null;
}

/** A compact, scrollable statistic table (plain HTML table; text only). */
export function StatTable<T>({ rows, columns, label, rowKey, maxHeight = "28rem", empty = "Nothing to show." }: { rows: readonly T[]; columns: readonly Column<T>[]; label: string; rowKey: (row: T, i: number) => string; maxHeight?: string; empty?: string }) {
  if (!rows.length) return <p className="text-[13px] text-muted-foreground">{empty}</p>;
  return (
    <div className="overflow-auto rounded-lg border [contain:inline-size]" style={{ maxHeight }}>
      <table className="w-full text-[13px]" aria-label={label}>
        <thead className="sticky top-0 z-[1] bg-card">
          <tr className="border-b">
            {columns.map((c) => (
              <th key={c.key} scope="col" className={cn("h-9 px-3 text-left text-xs font-medium whitespace-nowrap text-muted-foreground", c.numeric && "text-right")}>
                {c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={rowKey(row, i)} className="border-b last:border-0">
              {columns.map((c) => (
                <td key={c.key} className={cn("px-3 py-1.5 align-middle", c.numeric && "text-right tabular whitespace-nowrap")}>
                  {c.cell(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------------------ pickers

/** A labelled select of strings (variables, series, channels). */
export function Picker({ label, value, options, onChange, className }: { label: string; value: string; options: readonly { value: string; label: string }[]; onChange: (v: string) => void; className?: string }) {
  const id = useId();
  return (
    <div className={cn("grid gap-1", className)}>
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">
        {label}
      </label>
      <NativeSelect id={id} value={value} onChange={(e) => onChange(e.target.value)} className="h-8 min-w-40 text-[13px]">
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </NativeSelect>
    </div>
  );
}

/** Unique options from labels, in first-seen order. */
export function useOptions(labels: readonly string[]): { value: string; label: string }[] {
  return useMemo(() => [...new Set(labels)].map((l) => ({ value: l, label: l })), [labels]);
}

// ------------------------------------------------------------------------------ tests

/** A test's decision as a word with an icon (never colour alone). */
export function TestDecision({ test }: { test: Pick<StatTestResult, "reject" | "decision" | "status" | "message_plain"> }) {
  if (test.status && test.status !== "ok") {
    return <span className="text-xs text-muted-foreground" title={test.message_plain ?? undefined}>{test.status}</span>;
  }
  return (
    <span className={cn("inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 font-mono text-[11px]", test.reject ? "border-warning/40 bg-warning/10 text-warning" : "border-border text-muted-foreground")}>
      {test.decision.replaceAll("_", " ")}
    </span>
  );
}

/** One row per statistical test: name, statistic, p-value, decision and the plain verdict. */
export function TestsTable({ tests, label }: { tests: readonly StatTestResult[]; label: string }) {
  return (
    <StatTable
      label={label}
      rows={tests}
      rowKey={(t, i) => `${t.test}-${t.var ?? ""}-${i}`}
      columns={[
        { key: "test", header: "Test", cell: (t) => <span className="font-medium">{testName(t.test)}</span> },
        { key: "stat", header: "Statistic", numeric: true, cell: (t) => num(t.statistic) },
        { key: "p", header: "p-value", numeric: true, cell: (t) => pValue(t.p_value) },
        { key: "decision", header: "Decision", cell: (t) => <TestDecision test={t} /> },
        { key: "verdict", header: "In plain words", cell: (t) => <span className="text-muted-foreground">{t.status && t.status !== "ok" ? (t.message_plain ?? t.verdict_plain) : t.verdict_plain}</span> },
      ]}
    />
  );
}

const TEST_NAMES: Record<string, string> = {
  "test.adf": "ADF (unit root)",
  "test.kpss": "KPSS (stationarity)",
  "test.pp": "Phillips-Perron",
  "test.ljung_box": "Ljung-Box (no pattern left)",
  "test.jarque_bera": "Jarque-Bera (normal errors)",
  "test.breusch_pagan": "Breusch-Pagan (even spread)",
  "test.white": "White (even spread)",
  "test.arch_lm": "ARCH-LM (calm vs. volatile spells)",
  "test.granger": "Granger causality",
  "test.johansen": "Johansen (cointegration rank)",
  "test.engle_granger": "Engle-Granger",
  "test.diebold_mariano": "Diebold-Mariano",
};

export function testName(id: string): string {
  return TEST_NAMES[id] ?? id.replace(/^test\./, "").replaceAll("_", " ");
}
