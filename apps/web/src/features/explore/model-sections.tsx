/**
 * The Model Explorer sections (M3.5) of one model version, each rendered from its view:
 * parameters, diagnostics, fit against actuals, residual analysis, contributions, response
 * curves, impulse responses, variance decompositions, forecasts and backtests. The plain
 * reading comes first; the statistics are open in Expert mode.
 */
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, CheckCircle2, Info, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { KeyValues } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import { fanPlot } from "@/features/guided/plots";
import { cn } from "@/lib/utils";

import { backtestReading, decompositionReading, diagnosticsReading, fitReading, paramsReading } from "./plain";
import { fevdPlot, fitPlot, forestPlot, histogramPlot, irfPlot, lagPlot, linesPlot, qqPlot, responseCurvesPlot, stackedAreaPlot } from "./plots";
import { ExplorerPlot, num, pct, Picker, PlainSummary, pValue, Statistics, StatTable, TestsTable, useOptions, ViewNotes } from "./ui";
import type {
  BacktestView,
  DecompositionView,
  FevdView,
  FitVsActualView,
  IrfView,
  ModelDiagnosticsView,
  ModelForecastView,
  ParamRow,
  ParamTableView,
  ResidualDiagnosticsView,
  ResponseCurvesView,
  StatTestResult,
} from "./views";
import { facetLabel } from "./views";

interface P<V> {
  view: V;
  expert: boolean;
}

const INTERVAL_LABEL: Record<string, string> = {
  hdi_90: "90% HDI",
  hdi_95: "95% HDI",
  ci_90: "90% CI",
  ci_95: "95% CI",
};

export function paramLabel(r: Pick<ParamRow, "name" | "coords">): string {
  const coords = Object.values(r.coords ?? {});
  return coords.length ? `${r.name}[${coords.map(String).join(", ")}]` : r.name;
}

// ------------------------------------------------------------------------------ parameters

export function ParamsSection({ view, expert }: P<ParamTableView>) {
  const rows = useMemo(() => view.rows ?? [], [view.rows]);
  const interval = INTERVAL_LABEL[view.interval ?? "hdi_90"] ?? view.interval ?? "interval";
  const names = useOptions(useMemo(() => rows.map((r) => r.name), [rows]));
  const [group, setGroup] = useState(names[0]?.value ?? "");
  const reading = paramsReading(view);
  const plot = useMemo(
    () => forestPlot(rows.filter((r) => r.name === group).slice(0, 60).map((r) => ({ label: paramLabel(r), mean: r.mean ?? null, lower: r.lower ?? null, upper: r.upper ?? null })), { title: `Estimate (${interval})` }),
    [rows, group, interval],
  );
  const bayes = rows.some((r) => r.r_hat != null);
  const columns = useMemo((): ColumnDef<ParamRow, unknown>[] => {
    const numCell = (digits = 4) => ({ getValue }: { getValue: () => unknown }) => <span className="tabular">{num(getValue() as number | null, digits)}</span>;
    const cols: ColumnDef<ParamRow, unknown>[] = [
      { id: "param", header: "Parameter", accessorFn: (r) => paramLabel(r), cell: ({ getValue }) => <span className="font-mono text-xs">{String(getValue())}</span> },
      { id: "mean", header: bayes ? "Mean" : "Estimate", accessorFn: (r) => r.mean ?? null, cell: numCell(), sortUndefined: "last" },
      { id: "sd", header: bayes ? "Std. dev." : "Std. error", accessorFn: (r) => r.sd ?? null, cell: numCell(3) },
      { id: "lower", header: `${interval} low`, accessorFn: (r) => r.lower ?? null, cell: numCell() },
      { id: "upper", header: `${interval} high`, accessorFn: (r) => r.upper ?? null, cell: numCell() },
      {
        id: "zero",
        header: "Excludes 0",
        accessorFn: (r) => (r.lower != null && r.upper != null ? (r.lower > 0 || r.upper < 0 ? 1 : 0) : -1),
        cell: ({ getValue }) => (getValue() === 1 ? <CheckCircle2 aria-label="yes" className="size-3.5 text-success" /> : getValue() === 0 ? <span className="text-muted-foreground">no</span> : "—"),
      },
    ];
    if (rows.some((r) => r.p_value != null)) cols.push({ id: "p", header: "p-value", accessorFn: (r) => r.p_value ?? null, cell: ({ getValue }) => <span className="tabular">{pValue(getValue() as number | null)}</span> });
    if (bayes) {
      cols.push({ id: "rhat", header: "R-hat", accessorFn: (r) => r.r_hat ?? null, cell: ({ getValue }) => <span className={cn("tabular", ((getValue() as number | null) ?? 0) > 1.01 && "font-medium text-warning")}>{num(getValue() as number | null, 4)}</span> });
      cols.push({ id: "ess", header: "ESS (bulk)", accessorFn: (r) => r.ess_bulk ?? null, cell: ({ getValue }) => <span className={cn("tabular", ((getValue() as number | null) ?? 1e9) < 400 && "text-warning")}>{num(getValue() as number | null, 3)}</span> });
    }
    if (rows.some((r) => r.prior)) cols.push({ id: "prior", header: "Prior", accessorFn: (r) => r.prior ?? "", cell: ({ getValue }) => <span className="font-mono text-[11px] text-muted-foreground">{String(getValue())}</span> });
    return cols;
  }, [rows, bayes, interval]);
  return (
    <>
      <PlainSummary tone={reading.tone}>
        <p>{reading.text}</p>
        <p className="text-[13px] text-muted-foreground">Each dot is the model&apos;s best estimate and each line its {interval}: the range the value most likely lies in. A line that crosses zero means the model cannot tell the effect from none.</p>
      </PlainSummary>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Parameter" value={group} options={names} onChange={setGroup} />
      </div>
      <ExplorerPlot plot={plot} title={`Estimates of ${group}`} height={Math.min(520, 60 + 20 * rows.filter((r) => r.name === group).length)} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Every parameter">
        <DataTable label="Parameters" data={rows} columns={columns} getRowId={(r, i) => `${paramLabel(r)}-${i}`} filterPlaceholder="Filter parameters" maxHeight={480} rowHeight={34} />
        {view.omitted ? <p className="text-xs text-muted-foreground">{view.omitted} more parameter cells are not shown (raise max_params in an explore run).</p> : null}
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ diagnostics

const SEV_ICON = { info: Info, warning: AlertTriangle, error: XCircle } as const;
const SEV_CLASS = { info: "text-info", warning: "text-warning", error: "text-destructive" } as const;

type Item = Record<string, unknown>;

function isTest(i: Item): boolean {
  return i.kind === "test" || (typeof i.test === "string" && typeof i.decision === "string");
}

export function DiagnosticsSection({ view, expert }: P<ModelDiagnosticsView>) {
  const reading = diagnosticsReading(view);
  const items = (view.items ?? []) as Item[];
  const diagnostics = items.filter((i) => typeof i.code === "string");
  const fitStats = items.filter((i) => i.kind === "fit_stats");
  const tests = items.filter(isTest) as unknown as StatTestResult[];
  const stats = Object.entries(view.stats ?? {});
  const statCols = [...new Set(fitStats.flatMap((r) => Object.keys(r)).filter((k) => !["kind", "coords"].includes(k)))];
  return (
    <>
      <PlainSummary tone={reading.tone}>
        <p>{reading.text}</p>
        {diagnostics.length ? <p className="text-[13px] text-muted-foreground">{diagnostics.length} note{diagnostics.length === 1 ? "" : "s"} from the fit below.</p> : null}
      </PlainSummary>
      {diagnostics.length ? (
        <ul className="grid gap-2" aria-label="Fit diagnostics">
          {diagnostics.map((d, k) => {
            const sev = (d.severity as keyof typeof SEV_ICON | undefined) ?? "warning";
            const Icon = SEV_ICON[sev] ?? AlertTriangle;
            return (
              <li key={`${String(d.code)}-${k}`} className="grid grid-cols-[1.25rem_1fr] gap-3 rounded-lg border p-3">
                <Icon aria-hidden className={cn("mt-0.5 size-4", SEV_CLASS[sev])} />
                <div className="grid min-w-0 gap-0.5 text-[13px]">
                  <p className="font-medium">{[d.message_plain, d.message_expert, d.code].find((x): x is string => typeof x === "string")}</p>
                  {typeof d.suggested_action === "string" ? <p className="text-muted-foreground">{d.suggested_action}</p> : null}
                  {expert ? (
                    <p className="font-mono text-[11px] text-muted-foreground">
                      {typeof d.code === "string" ? d.code : ""}
                      {typeof d.message_expert === "string" ? ` · ${d.message_expert}` : ""}
                    </p>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        {stats.length ? <KeyValues items={stats.map(([k, v]) => [<span key={k} className="font-mono text-xs">{k}</span>, <span key="v" className="tabular">{num(v)}</span>])} /> : null}
        {fitStats.length ? (
          <StatTable
            label="Fit statistics per series"
            rows={fitStats}
            rowKey={(r, i) => `${JSON.stringify(r.coords)}-${i}`}
            columns={[
              { key: "series", header: "Series", cell: (r) => <span className="font-mono text-xs">{facetLabel(r.coords as Record<string, string>)}</span> },
              ...statCols.map((c) => ({ key: c, header: c, numeric: true, cell: (r: Item) => num(r[c] as number | null) })),
            ]}
          />
        ) : null}
        {tests.length ? <TestsTable tests={tests} label="The model's own tests" /> : null}
        <p className="text-xs text-muted-foreground">Inference: {view.inference}.</p>
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ fit

export function FitSection({ view, expert }: P<FitVsActualView>) {
  const series = useMemo(() => view.series ?? [], [view.series]);
  const labels = useMemo(() => series.map((s, i) => facetLabel(view.facets?.[i] ?? s.facet)), [series, view.facets]);
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const idx = Math.max(0, labels.indexOf(pick));
  const s = series[idx];
  const reading = fitReading(view);
  const plot = useMemo(() => (s ? fitPlot({ coords: s.coords, values: s.values }, view.fitted?.[idx] ?? null, view.split_at, { yTitle: s.var }) : null), [s, view.fitted, view.split_at, idx]);
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      {options.length > 1 ? (
        <div className="flex flex-wrap items-end gap-3">
          <Picker label="Series" value={pick} options={options} onChange={setPick} />
        </div>
      ) : null}
      <ExplorerPlot plot={plot} title={`Actual and fitted ${s?.var ?? ""} (${pick})`} caption="Dots: what happened. Line and band: what the model fitted, with its range. Periods after the dashed line were held out of the fit." />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      {expert ? <p className="text-xs text-muted-foreground">{series.length} series along {view.dim}.</p> : null}
    </>
  );
}

// ------------------------------------------------------------------------------ residuals

export function ResidualsSection({ view, expert }: P<ResidualDiagnosticsView>) {
  const series = useMemo(() => view.series ?? [], [view.series]);
  const labels = useMemo(() => series.map((s) => facetLabel(s.facet)), [series]);
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const idx = Math.max(0, labels.indexOf(pick));
  const s = series[idx];
  const acf = view.acf?.[idx] ?? view.acf?.[0];
  const tests = view.tests ?? [];
  const failing = tests.filter((t) => t.reject && (t.status ?? "ok") === "ok");
  const line = useMemo(() => (s ? linesPlot([{ label: "residual", coords: s.coords, values: s.values }], { yTitle: "Actual − fitted", height: 200 }) : null), [s]);
  const acfPlot = useMemo(() => (acf ? lagPlot(acf, { yTitle: "Autocorrelation", height: 200 }) : null), [acf]);
  const hist = useMemo(() => (view.histogram ? histogramPlot(view.histogram, { title: "Residual" }) : null), [view.histogram]);
  const qq = useMemo(() => (view.qq ? qqPlot(view.qq.theoretical, view.qq.sample) : null), [view.qq]);
  return (
    <>
      <PlainSummary tone={failing.length ? "warn" : tests.length ? "good" : "neutral"}>
        {tests.length ? (
          <ul className="grid gap-1">
            {tests.map((t, i) => (
              <li key={`${t.test}-${i}`} className="flex items-start gap-2">
                {t.reject ? <AlertTriangle aria-hidden className="mt-1 size-3.5 shrink-0 text-warning" /> : <CheckCircle2 aria-hidden className="mt-1 size-3.5 shrink-0 text-success" />}
                <span>{t.verdict_plain}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p>The errors the model leaves over time.</p>
        )}
      </PlainSummary>
      {options.length > 1 ? <Picker label="Series" value={pick} options={options} onChange={setPick} /> : null}
      <div className="grid gap-4 xl:grid-cols-2">
        <ExplorerPlot plot={line} title="Residuals over time" height={230} caption="Errors should look like noise around zero, with no runs or trend." />
        <ExplorerPlot plot={acfPlot} title="Autocorrelation of the residuals" height={230} caption="Bars beyond the band: a pattern the model missed." />
        <ExplorerPlot plot={hist} title="Distribution of the residuals" height={230} />
        <ExplorerPlot plot={qq} title="Normal QQ plot of the residuals" height={260} caption="Points on the dashed line: errors are close to normal." />
      </div>
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="The tests">
        <TestsTable tests={tests} label="Residual tests" />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ decomposition

export function DecompositionSection({ view, expert }: P<DecompositionView>) {
  const reading = decompositionReading(view);
  const plot = useMemo(() => stackedAreaPlot(view.coords, view.components ?? {}, { yTitle: "Contribution" }), [view]);
  const shares = Object.entries(view.shares ?? {}).sort((a, b) => Math.abs(b[1] ?? 0) - Math.abs(a[1] ?? 0));
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ExplorerPlot plot={plot} title="Contributions over time" caption={view.scale === "multiplicative" ? "Parts multiply rather than add up." : "The parts add up to the model's prediction."} height={320} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Shares of the total">
        <StatTable
          label="Shares"
          rows={shares}
          rowKey={([k]) => k}
          columns={[
            { key: "part", header: "Part", cell: ([k]) => <span className="font-mono text-xs">{k}</span> },
            { key: "share", header: "Share", numeric: true, cell: ([, v]) => pct(v, 1) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ response curves

export function ResponseCurvesSection({ view, expert }: P<ResponseCurvesView>) {
  const [shown, setShown] = useState<string[]>(view.channels);
  const plot = useMemo(() => responseCurvesPlot(view, shown), [view, shown]);
  const rows = view.channels.map((ch, c) => {
    const spend = view.spend[c] ?? [];
    const mean = view.mean[c] ?? [];
    const cur = view.current_spend?.[c] ?? null;
    let at = -1;
    if (cur !== null) for (let i = 0; i < spend.length; i++) if ((spend[i] ?? 0) <= cur) at = i;
    const slope = at >= 0 && at + 1 < spend.length ? ((mean[at + 1] ?? 0) - (mean[at] ?? 0)) / Math.max(1e-12, (spend[at + 1] ?? 0) - (spend[at] ?? 0)) : null;
    const first = spend.length > 1 ? ((mean[1] ?? 0) - (mean[0] ?? 0)) / Math.max(1e-12, (spend[1] ?? 0) - (spend[0] ?? 0)) : null;
    return { channel: ch, current: cur, contribution: at >= 0 ? (mean[at] ?? null) : null, slope, saturation: slope !== null && first ? slope / first : null };
  });
  const saturated = rows.filter((r) => r.saturation !== null && r.saturation < 0.3).map((r) => r.channel);
  return (
    <>
      <PlainSummary tone={saturated.length ? "warn" : "neutral"}>
        <p>How much each channel adds as its spend grows; the dot is what you spend today.</p>
        {saturated.length ? <p className="text-[13px]">{saturated.join(", ")} {saturated.length === 1 ? "is" : "are"} close to saturation: the next unit of money adds far less than the first ones did.</p> : null}
      </PlainSummary>
      <fieldset className="flex flex-wrap gap-1.5">
        <legend className="sr-only">Channels to show</legend>
        {view.channels.map((ch) => (
          <label key={ch} className={cn("inline-flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs", shown.includes(ch) && "border-primary/60 bg-accent")}>
            <input type="checkbox" className="accent-primary" checked={shown.includes(ch)} onChange={(e) => setShown((s) => (e.target.checked ? [...s, ch] : s.filter((x) => x !== ch)))} />
            {ch}
          </label>
        ))}
      </fieldset>
      <ExplorerPlot plot={plot} title="Response curves" height={320} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Channels at today's spend"
          rows={rows}
          rowKey={(r) => r.channel}
          columns={[
            { key: "ch", header: "Channel", cell: (r) => <span className="font-mono text-xs">{r.channel}</span> },
            { key: "cur", header: "Spend today", numeric: true, cell: (r) => num(r.current) },
            { key: "contrib", header: "Contribution there", numeric: true, cell: (r) => num(r.contribution) },
            { key: "slope", header: "Return of the next unit", numeric: true, cell: (r) => num(r.slope, 3) },
            { key: "sat", header: "vs. the first units", numeric: true, cell: (r) => pct(r.saturation) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ IRF / FEVD

export function IrfSection({ view, expert }: P<IrfView>) {
  const vars = view.variables.map((v) => ({ value: v, label: v }));
  const [response, setResponse] = useState(view.variables[0] ?? "");
  const [shock, setShock] = useState(view.variables[1] ?? view.variables[0] ?? "");
  const ri = Math.max(0, view.variables.indexOf(response));
  const si = Math.max(0, view.variables.indexOf(shock));
  const plot = useMemo(() => irfPlot(view, ri, si), [view, ri, si]);
  const values = view.values[ri]?.[si] ?? [];
  const peakAt = values.reduce((best, v, h) => (Math.abs(v) > Math.abs(values[best] ?? 0) ? h : best), 0);
  const settled = values.length > 2 && Math.abs(values.at(-1) ?? 0) < 0.1 * Math.abs(values[peakAt] ?? 1);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="A shock to" value={shock} options={vars} onChange={setShock} />
        <Picker label="Response of" value={response} options={vars} onChange={setResponse} />
        {view.orthogonalized ? <Badge variant="outline">orthogonalised</Badge> : null}
      </div>
      <PlainSummary>
        <p>
          After an unexpected rise in {shock}, {response} {(values[peakAt] ?? 0) >= 0 ? "rises" : "falls"} most {peakAt === 0 ? "at once" : `after ${peakAt} period${peakAt === 1 ? "" : "s"}`}
          {settled ? " and the effect fades within the horizon." : ", and the effect has not faded by the end of the horizon."}
        </p>
      </PlainSummary>
      <ExplorerPlot plot={plot} title={`Response of ${response} to a shock in ${shock}`} caption="The band is the uncertainty; where it covers zero the response could be none." />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      {expert ? <p className="text-xs text-muted-foreground">{view.horizons} periods; {view.variables.length} variables.</p> : null}
    </>
  );
}

export function FevdSection({ view, expert }: P<FevdView>) {
  const vars = view.variables.map((v) => ({ value: v, label: v }));
  const [variable, setVariable] = useState(view.variables[0] ?? "");
  const vi = Math.max(0, view.variables.indexOf(variable));
  const shares = useMemo(() => view.shares[vi] ?? [], [view.shares, vi]);
  const plot = useMemo(() => fevdPlot(view.variables, shares), [view.variables, shares]);
  const last = shares.at(-1) ?? [];
  const own = last[vi] ?? 0;
  const top = last.map((s, j) => [view.variables[j] ?? "", s] as const).filter(([, s]) => s > 0).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <Picker label="Variable" value={variable} options={vars} onChange={setVariable} />
      <PlainSummary>
        <p>
          {view.horizons} periods ahead, {pct(own)} of what is uncertain about {variable} comes from its own surprises
          {top.filter(([n]) => n !== variable).length ? `; the largest outside source is ${top.filter(([n]) => n !== variable)[0]![0]} (${pct(top.filter(([n]) => n !== variable)[0]![1])}).` : "."}
        </p>
      </PlainSummary>
      <ExplorerPlot plot={plot} title={`Forecast-error variance of ${variable}`} height={280} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      {expert ? (
        <StatTable
          label="Shares at the last horizon"
          rows={top}
          rowKey={([n]) => n}
          columns={[
            { key: "shock", header: "Shock", cell: ([n]) => <span className="font-mono text-xs">{n}</span> },
            { key: "share", header: "Share", numeric: true, cell: ([, s]) => pct(s, 1) },
          ]}
        />
      ) : null}
    </>
  );
}

// ------------------------------------------------------------------------------ forecast

export function ForecastSection({ view, expert }: P<ModelForecastView>) {
  const items = useMemo(() => view.forecasts ?? [], [view.forecasts]);
  const labels = useMemo(() => items.map((f, i) => (items.length > 1 ? `${f.kpi} (${i + 1})` : f.kpi)), [items]);
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const f = items[Math.max(0, labels.indexOf(pick))];
  const plot = useMemo(() => (f ? fanPlot(f.fan, { title: f.kpi, splitAt: f.split_at ?? null }) : null), [f]);
  if (!f) return <p className="text-muted-foreground">No forecast.</p>;
  const total = f.total;
  const q = (k: string) => Object.entries(total?.quantiles ?? {}).find(([key]) => Math.abs(Number(key) - Number(k)) < 1e-9)?.[1];
  return (
    <>
      {options.length > 1 ? <Picker label="Forecast" value={pick} options={options} onChange={setPick} /> : null}
      <PlainSummary>
        <p>
          {total
            ? `Over the forecast, ${f.kpi} adds up to about ${num(total.mean, 3)}${f.units ? ` ${f.units}` : ""}${q("0.05") !== undefined && q("0.95") !== undefined ? ` (90% likely between ${num(q("0.05"), 3)} and ${num(q("0.95"), 3)})` : ""}.`
            : `The forecast of ${f.kpi} after the history, with its likely range.`}
        </p>
        {f.summed_over?.length ? <p className="text-[13px] text-muted-foreground">Summed over {f.summed_over.join(" and ")}.</p> : null}
      </PlainSummary>
      <ExplorerPlot plot={plot} title={`Forecast of ${f.kpi}`} caption="Dots: history. After the dashed line: the forecast, its middle line and the 50% and 90% bands." height={300} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      {expert ? <p className="text-xs text-muted-foreground">{f.fan.coords.length - (f.history ?? 0)} periods ahead along {f.dim}; {f.kind ?? "expected"} values.</p> : null}
    </>
  );
}

// ------------------------------------------------------------------------------ backtest

export function BacktestSection({ view, expert }: P<BacktestView>) {
  const reading = backtestReading(view);
  const folds = useMemo(() => view.folds ?? [], [view.folds]);
  const metrics = [...new Set([...Object.keys(view.summary ?? {}), ...Object.values(view.benchmarks ?? {}).flatMap((m) => Object.keys(m))])];
  const [metric, setMetric] = useState(metrics.find((m) => ["mape", "wape", "mae", "rmse"].includes(m)) ?? metrics[0] ?? "");
  const plot = useMemo(
    () => linesPlot([{ label: "model", coords: folds.map((f) => f.fold.index + 1), values: folds.map((f) => f.metrics?.[metric] ?? null) }], { xTitle: "Fold", yTitle: metric.toUpperCase(), height: 200 }),
    [folds, metric],
  );
  const rows = [
    { name: "This model", values: view.summary ?? {} },
    ...Object.entries(view.benchmarks ?? {}).map(([name, values]) => ({ name: name.replaceAll("_", " "), values })),
  ];
  const horizons = [...new Set(folds.flatMap((f) => Object.keys(f.by_horizon ?? {})))].sort((a, b) => Number(a) - Number(b));
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <StatTable
        label="Backtest summary"
        rows={rows}
        rowKey={(r) => r.name}
        columns={[
          { key: "name", header: "Forecaster", cell: (r) => <span className={cn(r.name === "This model" && "font-semibold")}>{r.name}</span> },
          ...metrics.map((m) => ({ key: m, header: m.toUpperCase(), numeric: true, cell: (r: (typeof rows)[number]) => num(r.values[m] ?? null) })),
        ]}
      />
      {metrics.length ? <Picker label="Metric by fold" value={metric} options={metrics.map((m) => ({ value: m, label: m.toUpperCase() }))} onChange={setMetric} /> : null}
      <ExplorerPlot plot={plot} title={`${metric.toUpperCase()} by fold`} height={220} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Every fold">
        <StatTable
          label="Folds"
          rows={folds}
          rowKey={(f) => String(f.fold.index)}
          columns={[
            { key: "fold", header: "Fold", numeric: true, cell: (f) => f.fold.index + 1 },
            { key: "train", header: "Trained on", cell: (f) => (f.fold.train_start != null ? `${f.fold.train_start}–${f.fold.train_stop ?? ""}` : "—") },
            { key: "test", header: "Tested on", cell: (f) => (f.fold.test_start != null ? `${f.fold.test_start}–${f.fold.test_stop ?? ""}` : "—") },
            ...metrics.slice(0, 5).map((m) => ({ key: m, header: m.toUpperCase(), numeric: true, cell: (f: (typeof folds)[number]) => num(f.metrics?.[m] ?? null) })),
          ]}
        />
        {horizons.length ? (
          <StatTable
            label="By horizon"
            rows={horizons}
            rowKey={(h) => h}
            columns={[
              { key: "h", header: "Periods ahead", numeric: true, cell: (h) => h },
              ...folds.map((f) => ({ key: `f${f.fold.index}`, header: `Fold ${f.fold.index + 1} ${metric.toUpperCase()}`, numeric: true, cell: (h: string) => num(f.by_horizon?.[h]?.[metric] ?? null) })),
            ]}
          />
        ) : null}
        <p className="text-xs text-muted-foreground">Plan: {JSON.stringify(view.plan ?? {})}</p>
      </Statistics>
    </>
  );
}

