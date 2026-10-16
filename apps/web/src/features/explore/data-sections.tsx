/**
 * The thirteen Data Explorer sections (M3.5), each rendered from its view: the plain reading
 * first, then pickers, an interactive plot (plot sandbox) and the statistics (open in Expert
 * mode, one click away in Guided mode).
 */
import { AlertOctagon, AlertTriangle, Info } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import {
  acfReading,
  ccfReading,
  correlationPairs,
  correlationReading,
  distributionReading,
  groupsReading,
  leadReading,
  missingnessReading,
  outliersReading,
  qualityReading,
  qualityScoreReading,
  stationarityReading,
  stlReading,
  strengthLabel,
  strengthWord,
  suggestionKey,
  vifReading,
} from "./plain";
import { barsPlot, heatmapPlot, histogramPlot, lagPlot, linesPlot, stlPlot, xType } from "./plots";
import { ExplorerPlot, num, pct, Picker, PlainSummary, pValue, Statistics, StatTable, TestDecision, useOptions, ViewNotes } from "./ui";
import type {
  AcfPacfView,
  CorrelationView,
  CrossCorrelationView,
  DataOverviewView,
  DistributionsView,
  GroupComparisonView,
  MissingnessView,
  OutliersView,
  QualityView,
  StationarityView,
  StlView,
  TimeSeriesView,
  VifView,
} from "./views";
import { facetLabel, seriesLabel } from "./views";

interface P<V> {
  view: V;
  expert: boolean;
}

// ------------------------------------------------------------------------------ overview

export function OverviewSection({ view, expert }: P<DataOverviewView>) {
  const score = qualityScoreReading(view.quality_score);
  const vars = view.vars ?? [];
  const plot = useMemo(() => {
    const all = view.vars ?? [];
    return all.some((v) => (v.missing_share ?? 0) > 0) ? barsPlot(all.map((v) => ({ label: v.name, value: v.missing_share ?? 0, group: v.role ?? "other" })), { title: "Share missing", format: ".1~%" }) : null;
  }, [view.vars]);
  return (
    <>
      <PlainSummary tone={score?.tone ?? "neutral"}>
        <p>{view.summary_plain}</p>
        {score ? <p className="text-[13px] text-muted-foreground">{score.text}</p> : null}
      </PlainSummary>
      <ViewNotes warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Dimensions and variables">
        <StatTable
          label="Dimensions"
          rows={view.dims ?? []}
          rowKey={(d) => d.name}
          columns={[
            { key: "name", header: "Dimension", cell: (d) => <span className="font-mono text-xs">{d.name}</span> },
            { key: "kind", header: "Kind", cell: (d) => d.kind },
            { key: "size", header: "Size", numeric: true, cell: (d) => num(d.size) },
            { key: "range", header: "From – to", cell: (d) => (d.start != null ? `${String(d.start)} – ${String(d.end ?? "")}` : "—") },
            { key: "freq", header: "Frequency", cell: (d) => d.freq ?? "—" },
          ]}
        />
        <StatTable
          label="Variables"
          rows={vars}
          rowKey={(v) => v.name}
          columns={[
            { key: "name", header: "Variable", cell: (v) => <span className="font-mono text-xs">{v.name}</span> },
            { key: "role", header: "Role", cell: (v) => (v.role ? <Badge variant="outline">{v.role}</Badge> : "—") },
            { key: "dims", header: "Dims", cell: (v) => <span className="font-mono text-xs text-muted-foreground">{(v.dims ?? []).join(" × ")}</span> },
            { key: "units", header: "Units", cell: (v) => v.units ?? "—" },
            { key: "missing", header: "Missing", numeric: true, cell: (v) => pct(v.missing_share ?? 0, 1) },
            { key: "distinct", header: "Distinct", numeric: true, cell: (v) => num(v.distinct) },
            { key: "constant", header: "", cell: (v) => (v.constant ? <Badge variant="warning">constant</Badge> : null) },
          ]}
        />
        <p className="text-xs text-muted-foreground">{num(view.n_cells)} cells in all.</p>
      </Statistics>
      {plot ? <ExplorerPlot plot={plot} title="Share missing by variable" /> : null}
    </>
  );
}

// ------------------------------------------------------------------------------ distributions

export function DistributionsSection({ view, expert }: P<DistributionsView>) {
  const items = useMemo(() => view.items ?? [], [view.items]);
  const labels = useMemo(() => items.map((i) => seriesLabel(i.var, i.facet)), [items]);
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const item = items[labels.indexOf(pick)] ?? items[0];
  const plot = useMemo(() => (item?.histogram ? histogramPlot(item.histogram, { title: seriesLabel(item.var, item.facet) }) : null), [item]);
  if (!item) return <p className="text-muted-foreground">No numeric variables.</p>;
  const reading = distributionReading(item);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Variable" value={pick} options={options} onChange={setPick} />
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ExplorerPlot plot={plot} title={`Distribution of ${seriesLabel(item.var, item.facet)}`} height={260} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Summary statistics"
          rows={items}
          rowKey={(i, k) => `${seriesLabel(i.var, i.facet)}-${k}`}
          columns={[
            { key: "var", header: "Variable", cell: (i) => <span className={cn("font-mono text-xs", i === item && "font-semibold")}>{seriesLabel(i.var, i.facet)}</span> },
            { key: "n", header: "n", numeric: true, cell: (i) => num(i.n) },
            { key: "mean", header: "Mean", numeric: true, cell: (i) => num(i.mean) },
            { key: "sd", header: "Std. dev.", numeric: true, cell: (i) => num(i.sd) },
            { key: "skew", header: "Skew", numeric: true, cell: (i) => num(i.skew, 3) },
            { key: "kurt", header: "Kurtosis", numeric: true, cell: (i) => num(i.kurtosis, 3) },
            { key: "zero", header: "Zeros", numeric: true, cell: (i) => pct(i.zero_share, 1) },
            ...["0.05", "0.25", "0.5", "0.75", "0.95"].map((qk) => ({
              key: `q${qk}`,
              header: `${Number(qk) * 100}%`,
              numeric: true,
              cell: (i: (typeof items)[number]) => num(Object.entries(i.quantiles ?? {}).find(([k]) => Math.abs(Number(k) - Number(qk)) < 1e-9)?.[1]),
            })),
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ time series

export function TimeSeriesSection({ view, expert }: P<TimeSeriesView>) {
  const series = useMemo(() => view.series ?? [], [view.series]);
  const vars = useOptions(useMemo(() => series.map((s) => s.var), [series]));
  const facets = useOptions(useMemo(() => series.map((s) => facetLabel(s.facet)), [series]));
  const [variable, setVariable] = useState(vars[0]?.value ?? "");
  const [facet, setFacet] = useState("__all");
  const shown = useMemo(() => series.filter((s) => s.var === variable && (facet === "__all" || facetLabel(s.facet) === facet)), [series, variable, facet]);
  const plot = useMemo(
    () => linesPlot(shown.map((s) => ({ label: facetLabel(s.facet) === "all" ? s.var : facetLabel(s.facet), coords: s.coords, values: s.values })), { xTitle: view.dim, yTitle: variable, height: 300 }),
    [shown, view.dim, variable],
  );
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Variable" value={variable} options={vars} onChange={setVariable} />
        {facets.length > 1 ? <Picker label={view.facet_by?.length ? `${view.facet_by.join(" × ")}` : "Series"} value={facet} options={[{ value: "__all", label: "All series" }, ...facets]} onChange={setFacet} /> : null}
      </div>
      <PlainSummary>
        <p>
          {variable} over {view.dim}
          {view.facet_by?.length ? `, one line per ${view.facet_by.join(" and ")}` : ""}. Breaks, level shifts and gaps are easier to see here than to test.
        </p>
      </PlainSummary>
      <ExplorerPlot plot={plot} title={`${variable} over time`} height={330} />
      <ViewNotes caption={view.caption_plain} warnings={[...(view.warnings_plain ?? []), ...(shown.some((s) => s.downsampled) ? ["Long series are thinned evenly to keep the plot fast."] : []), ...(view.omitted_series ? [`${view.omitted_series} more series are not shown.`] : [])]} />
      {expert ? <p className="text-xs text-muted-foreground">{series.length} series in the view.</p> : null}
    </>
  );
}

// ------------------------------------------------------------------------------ missingness

export function MissingnessSection({ view, expert }: P<MissingnessView>) {
  const reading = missingnessReading(view);
  const grid = view.grid;
  const plot = useMemo(() => {
    if (!grid) return null;
    const cells = grid.labels.flatMap((label, i) => grid.coords.map((c, j) => ({ x: c, y: label, value: grid.fraction[i]?.[j] ?? 0 })));
    return heatmapPlot(cells, { xTitle: grid.along, legend: "Missing", domain: [0, 1], scheme: "oranges", format: ".0%", yOrder: grid.labels, temporal: xType(grid.coords) === "temporal" });
  }, [grid]);
  const rows = Object.entries(view.share_by_var ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      {plot ? <ExplorerPlot plot={plot} title="Missing values by variable and period" caption="Darker cells: a larger share of the values in that period is missing." /> : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Missing values per variable"
          rows={rows}
          rowKey={([k]) => k}
          columns={[
            { key: "var", header: "Variable", cell: ([k]) => <span className="font-mono text-xs">{k}</span> },
            { key: "share", header: "Missing", numeric: true, cell: ([, s]) => pct(s, 1) },
            { key: "gap", header: "Longest gap", numeric: true, cell: ([k]) => num(view.longest_gap?.[k] ?? 0) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ outliers

export function OutliersSection({ view, expert }: P<OutliersView>) {
  const items = useMemo(() => view.items ?? [], [view.items]);
  const reading = outliersReading(view);
  const withPoints = items.filter((i) => i.count > 0);
  const [pick, setPick] = useState(withPoints[0]?.var ?? items[0]?.var ?? "");
  const item = items.find((i) => i.var === pick);
  const anyOutliers = withPoints.length > 0;
  const plot = useMemo(() => (anyOutliers ? barsPlot(items.map((i) => ({ label: i.var, value: i.count })), { title: "Unusual values" }) : null), [items, anyOutliers]);
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      {plot ? <ExplorerPlot plot={plot} title="Unusual values per variable" height={220} /> : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      {withPoints.length ? (
        <div className="grid gap-3">
          <Picker label="Show the values of" value={pick} options={withPoints.map((i) => ({ value: i.var, label: `${i.var} (${i.count})` }))} onChange={setPick} />
          <StatTable
            label={`Unusual values of ${pick}`}
            rows={item?.points ?? []}
            rowKey={(p, i) => `${JSON.stringify(p.coords)}-${i}`}
            columns={[
              { key: "where", header: "Where", cell: (p) => <span className="font-mono text-xs">{Object.entries(p.coords ?? {}).map(([k, v]) => `${k}=${String(v)}`).join(", ")}</span> },
              { key: "value", header: "Value", numeric: true, cell: (p) => num(p.value) },
              { key: "score", header: "How unusual", numeric: true, cell: (p) => num(p.score, 3) },
            ]}
          />
        </div>
      ) : null}
      {expert ? (
        <p className="text-xs text-muted-foreground">
          Method {view.method ?? "mad"}, threshold {num(view.threshold)} (robust scores on a standard-normal scale).
        </p>
      ) : null}
    </>
  );
}

// ------------------------------------------------------------------------------ correlation

export function CorrelationSection({ view, expert }: P<CorrelationView>) {
  const reading = correlationReading(view);
  const pairs = correlationPairs(view);
  const plot = useMemo(() => {
    const cells = view.vars.flatMap((a, i) => view.vars.map((b, j) => ({ x: a, y: b, value: view.matrix[i]?.[j] ?? null })));
    return heatmapPlot(cells, { legend: "Correlation", domain: [-1, 1], scheme: "redblue", text: view.vars.length <= 14, xOrder: view.vars, yOrder: view.vars });
  }, [view]);
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ExplorerPlot plot={plot} title="Correlation matrix" caption={`${view.method === "spearman" ? "Spearman (rank)" : "Pearson"} correlations: +1 move together, −1 move opposite, 0 unrelated.`} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Strongest pairs">
        <StatTable
          label="Correlated pairs"
          rows={pairs.slice(0, 30)}
          rowKey={(p) => `${p.a}|${p.b}`}
          columns={[
            { key: "pair", header: "Pair", cell: (p) => <span className="font-mono text-xs">{p.a} · {p.b}</span> },
            { key: "r", header: "r", numeric: true, cell: (p) => num(p.r, 3) },
            { key: "p", header: "p-value", numeric: true, cell: (p) => pValue(p.p) },
            { key: "plain", header: "In plain words", cell: (p) => <span className="text-muted-foreground">{`${strengthWord(p.r)} ${p.r >= 0 ? "together" : "opposite"}`}</span> },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ cross-correlation

export function CcfSection({ view, expert, unit }: P<CrossCorrelationView> & { unit?: string }) {
  const items = view.items ?? [];
  const labels = items.map((i) => seriesLabel(i.var, i.facet));
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const item = items[labels.indexOf(pick)] ?? items[0];
  const reading = ccfReading(view);
  const plot = useMemo(() => (item ? lagPlot(item, { yTitle: "Correlation", xTitle: "Lag (positive: the driver leads)" }) : null), [item]);
  return (
    <>
      <PlainSummary tone={reading.tone}>
        <p>{reading.text}</p>
        {item ? <p className="text-[13px]">{leadReading(item, view.kpi, unit)}</p> : null}
      </PlainSummary>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Driver" value={pick} options={options} onChange={setPick} />
        {view.prewhitened ? <Badge variant="info">prewhitened</Badge> : null}
      </div>
      <ExplorerPlot plot={plot} title={`Correlation of ${pick} with ${view.kpi} by lag`} caption="Bars beyond the dashed band are more than chance." />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Peak lag of every driver">
        <StatTable
          label="Peak lags"
          rows={items}
          rowKey={(i, k) => `${seriesLabel(i.var, i.facet)}-${k}`}
          columns={[
            { key: "var", header: "Driver", cell: (i) => <span className="font-mono text-xs">{seriesLabel(i.var, i.facet)}</span> },
            { key: "peak", header: "Peak lag", numeric: true, cell: (i) => num(i.peak_lag) },
            { key: "r", header: "Correlation there", numeric: true, cell: (i) => num(i.peak_lag != null ? i.values[i.lags.indexOf(i.peak_lag)] : null, 3) },
            { key: "bound", header: "Band", numeric: true, cell: (i) => (i.bound != null ? `±${num(i.bound, 3)}` : "—") },
            { key: "sig", header: "Significant lags", cell: (i) => <span className="font-mono text-xs">{(i.significant_lags ?? []).join(", ") || "none"}</span> },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ ACF / PACF

export function AcfPacfSection({ view, expert }: P<AcfPacfView>) {
  const acf = view.acf ?? [];
  const labels = acf.map((i) => seriesLabel(i.var, i.facet));
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const idx = Math.max(0, labels.indexOf(pick));
  const a = acf[idx];
  const p = view.pacf?.[idx];
  const suggested = a ? view.suggested?.[suggestionKey(a.var, a.facet)] : undefined;
  const reading = acfReading(a, suggested);
  const acfPlot = useMemo(() => (a ? lagPlot(a, { yTitle: "Autocorrelation" }) : null), [a]);
  const pacfPlot = useMemo(() => (p ? lagPlot(p, { yTitle: "Partial autocorrelation" }) : null), [p]);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Series" value={pick} options={options} onChange={setPick} />
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <div className="grid gap-4 xl:grid-cols-2">
        <ExplorerPlot plot={acfPlot} title={`Autocorrelation of ${pick}`} caption="How each value relates to the value k periods earlier." />
        <ExplorerPlot plot={pacfPlot} title={`Partial autocorrelation of ${pick}`} caption="The same, once the shorter lags are accounted for (a guide to the AR order)." />
      </div>
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Suggested orders for every series">
        <StatTable
          label="Suggested ARMA orders"
          rows={Object.entries(view.suggested ?? {})}
          rowKey={([k]) => k}
          columns={[
            { key: "series", header: "Series", cell: ([k]) => <span className="font-mono text-xs">{k}</span> },
            { key: "p", header: "AR (p)", numeric: true, cell: ([, o]) => num(o.p ?? null) },
            { key: "q", header: "MA (q)", numeric: true, cell: ([, o]) => num(o.q ?? null) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ stationarity

const VERDICT_BADGE = {
  stable: { label: "Stable", variant: "success" },
  trend_stable: { label: "Stable around a trend", variant: "info" },
  difference: { label: "Wanders", variant: "warning" },
  unclear: { label: "Unclear", variant: "outline" },
} as const;

export function StationaritySection({ view, expert }: P<StationarityView>) {
  const rows = view.rows ?? [];
  const reading = stationarityReading(view);
  return (
    <>
      <PlainSummary tone={reading.tone}>
        <p>{reading.text}</p>
        <p className="text-[13px] text-muted-foreground">A series that wanders has no level to return to: model its changes (difference it) or use a model built for that.</p>
      </PlainSummary>
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <StatTable
        label="Stationarity by series"
        rows={rows}
        rowKey={(r, i) => `${seriesLabel(r.var, r.facet)}-${i}`}
        columns={[
          { key: "series", header: "Series", cell: (r) => <span className="font-mono text-xs">{seriesLabel(r.var, r.facet)}</span> },
          { key: "verdict", header: "Verdict", cell: (r) => <Badge variant={VERDICT_BADGE[r.verdict].variant}>{VERDICT_BADGE[r.verdict].label}</Badge> },
          { key: "plain", header: "What to do", cell: (r) => <span className="text-muted-foreground">{r.verdict_plain}</span> },
          ...(expert
            ? [
                { key: "adf", header: "ADF stat (p)", numeric: true, cell: (r: (typeof rows)[number]) => (r.adf ? `${num(r.adf.statistic, 3)} (${pValue(r.adf.p_value)})` : "—") },
                { key: "adfd", header: "ADF", cell: (r: (typeof rows)[number]) => (r.adf ? <TestDecision test={r.adf} /> : "—") },
                { key: "kpss", header: "KPSS stat (p)", numeric: true, cell: (r: (typeof rows)[number]) => (r.kpss ? `${num(r.kpss.statistic, 3)} (${pValue(r.kpss.p_value)})` : "—") },
                { key: "kpssd", header: "KPSS", cell: (r: (typeof rows)[number]) => (r.kpss ? <TestDecision test={r.kpss} /> : "—") },
              ]
            : []),
        ]}
      />
      {expert ? <p className="text-xs text-muted-foreground">ADF asks "does it wander?" (rejecting means stable); KPSS asks the opposite (rejecting means it wanders). α = {num(view.alpha ?? 0.05)}.</p> : null}
    </>
  );
}

// ------------------------------------------------------------------------------ STL

export function StlSection({ view, expert }: P<StlView>) {
  const items = view.items ?? [];
  const labels = items.map((i) => seriesLabel(i.var, i.facet));
  const options = useOptions(labels);
  const [pick, setPick] = useState(labels[0] ?? "");
  const item = items[labels.indexOf(pick)] ?? items[0];
  const plot = useMemo(() => (item ? stlPlot(item) : null), [item]);
  if (!item) return <p className="text-muted-foreground">No series were decomposed.</p>;
  const reading = stlReading(item);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Series" value={pick} options={options} onChange={setPick} />
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ExplorerPlot plot={plot} title={`Trend, season and remainder of ${pick}`} height={460} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Strengths of every series">
        <StatTable
          label="Seasonal and trend strength"
          rows={items}
          rowKey={(i, k) => `${seriesLabel(i.var, i.facet)}-${k}`}
          columns={[
            { key: "series", header: "Series", cell: (i) => <span className="font-mono text-xs">{seriesLabel(i.var, i.facet)}</span> },
            { key: "period", header: "Season length", numeric: true, cell: (i) => num(i.period) },
            { key: "season", header: "Seasonal strength", numeric: true, cell: (i) => `${num(i.seasonal_strength, 2)} · ${strengthLabel(i.seasonal_strength)}` },
            { key: "trend", header: "Trend strength", numeric: true, cell: (i) => `${num(i.trend_strength, 2)} · ${strengthLabel(i.trend_strength)}` },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ VIF

export function VifSection({ view, expert }: P<VifView>) {
  const reading = vifReading(view);
  const flagged = useMemo(() => new Set(view.flagged ?? []), [view.flagged]);
  const entries = useMemo(() => Object.entries(view.vif ?? {}).sort((a, b) => (b[1] ?? 0) - (a[1] ?? 0)), [view.vif]);
  const plot = useMemo(
    () => barsPlot(entries.map(([k, v]) => ({ label: k, value: v, group: flagged.has(k) ? "Too much overlap" : "Fine" })), { title: "Variance inflation", ruleAt: view.limit ?? 10, groups: { domain: ["Fine", "Too much overlap"], range: ["#1f8a70", "#d9822b"] } }),
    [entries, flagged, view.limit],
  );
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ExplorerPlot plot={plot} title="Overlap between drivers (variance inflation)" caption={`The dashed line is the limit (${num(view.limit ?? 10)}): above it a driver's separate effect is poorly identified.`} height={240} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Variance inflation factors"
          rows={entries}
          rowKey={([k]) => k}
          columns={[
            { key: "driver", header: "Driver", cell: ([k]) => <span className="font-mono text-xs">{k}</span> },
            { key: "vif", header: "VIF", numeric: true, cell: ([, v]) => num(v, 3) },
            { key: "flag", header: "", cell: ([k]) => (flagged.has(k) ? <Badge variant="warning">above the limit</Badge> : null) },
          ]}
        />
        {view.condition_number != null ? <p className="text-xs text-muted-foreground">Condition number {num(view.condition_number, 4)} (above 30 signals collinearity).</p> : null}
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ groups

export function GroupsSection({ view, expert }: P<GroupComparisonView>) {
  const items = view.items ?? [];
  const options = useOptions(items.map((i) => i.var));
  const [pick, setPick] = useState(options[0]?.value ?? "");
  const item = items.find((i) => i.var === pick) ?? items[0];
  const reading = groupsReading(view);
  const plot = useMemo(
    () => (item ? barsPlot((item.groups ?? []).map((g) => ({ label: String(g.group), value: g.mean ?? null, lo: g.mean != null && g.sd != null ? g.mean - g.sd : null, hi: g.mean != null && g.sd != null ? g.mean + g.sd : null })), { title: `Mean ${item.var} (± one std. dev.)`, sort: "none" }) : null),
    [item],
  );
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker label="Variable" value={pick} options={options} onChange={setPick} />
      </div>
      <PlainSummary tone={reading.tone}>
        <p>{reading.text}</p>
        {item?.p_value != null ? (
          <p className="text-[13px] text-muted-foreground">
            {item.differs ? `${item.var} differs between the ${view.by} groups` : `${item.var} looks similar across the ${view.by} groups`} ({item.test === "kruskal" ? "Kruskal-Wallis" : "ANOVA"} p {pValue(item.p_value)}).
          </p>
        ) : null}
      </PlainSummary>
      <ExplorerPlot plot={plot} title={`${pick} by ${view.by}`} height={240} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label={`${pick} by ${view.by}`}
          rows={item?.groups ?? []}
          rowKey={(g) => String(g.group)}
          columns={[
            { key: "group", header: view.by, cell: (g) => <span className="font-mono text-xs">{String(g.group)}</span> },
            { key: "n", header: "n", numeric: true, cell: (g) => num(g.n) },
            { key: "mean", header: "Mean", numeric: true, cell: (g) => num(g.mean) },
            { key: "median", header: "Median", numeric: true, cell: (g) => num(g.median) },
            { key: "sd", header: "Std. dev.", numeric: true, cell: (g) => num(g.sd) },
            { key: "share", header: "Share of total", numeric: true, cell: (g) => pct(g.share_of_total, 1) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ quality

const SEVERITY = {
  error: { icon: AlertOctagon, className: "text-destructive", label: "Fix first" },
  warning: { icon: AlertTriangle, className: "text-warning", label: "Look at it" },
  info: { icon: Info, className: "text-info", label: "Good to know" },
} as const;

export function QualitySection({ view }: P<QualityView>) {
  const reading = qualityReading(view);
  const order = { error: 0, warning: 1, info: 2 } as const;
  const items = [...(view.items ?? [])].sort((a, b) => order[a.severity ?? "warning"] - order[b.severity ?? "warning"]);
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      {items.length ? (
        <ul className="grid gap-2" aria-label="Data-quality warnings">
          {items.map((w, i) => {
            const meta = SEVERITY[w.severity ?? "warning"];
            const Icon = meta.icon;
            return (
              <li key={`${w.code}-${w.var ?? ""}-${i}`} className="grid grid-cols-[1.25rem_1fr] gap-3 rounded-lg border p-3" data-severity={w.severity ?? "warning"}>
                <Icon aria-hidden className={cn("mt-0.5 size-4", meta.className)} />
                <div className="grid min-w-0 gap-1">
                  <p className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{w.message_plain}</span>
                    <span className={cn("text-xs font-medium", meta.className)}>{meta.label}</span>
                  </p>
                  {w.suggested_action ? <p className="text-[13px] text-muted-foreground">{w.suggested_action}</p> : null}
                  <p className="font-mono text-[11px] text-muted-foreground">
                    {w.code}
                    {w.var ? ` · ${w.var}` : ""}
                    {Object.keys(w.coords ?? {}).length ? ` · ${Object.entries(w.coords ?? {}).map(([k, v]) => `${k}=${String(v)}`).join(", ")}` : ""}
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
    </>
  );
}
