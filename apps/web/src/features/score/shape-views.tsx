/**
 * "Results by output": one view per score output, chosen by its shape (`shape.ts`). An output
 * on a grid is a map with a slider over its frames (the runner's `ui/fields.json`, drawn by the
 * Model Explorer's field viewer), an output on areas or of a classifier the run's own plots
 * that draw it (area map; ROC/PR, confusion and calibration), an output over time a line with
 * its band, an output by category an interval per cell, and a single value tiles. Charts render
 * in the plot sandbox.
 */
import { useQuery } from "@tanstack/react-query";
import { ChartSpline } from "lucide-react";
import { useCallback, useMemo, useState } from "react";

import { useRunPlots } from "@/api/queries";
import type { ArtifactLink, ScoreSummary } from "@/api/types";
import { ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { FieldViewer } from "@/features/explore/field-viewer";
import { forestPlot } from "@/features/explore/plots";
import { ExplorerPlot, Picker, ViewNotes } from "@/features/explore/ui";
import { facetLabel, parseView, type FieldEntry, type FieldsView } from "@/features/explore/views";
import { quickChart } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { outputLinks, RunPlot } from "@/features/runs/run-plots";
import { fetchArtifactJson } from "@/lib/artifacts";
import { formatNumber } from "@/lib/format";
import type { Row } from "@mapi/plot-sandbox/protocol";

import { intervalRows, isDateDim, SCORE_FIELDS_VIEW, SHAPE_LABEL, shapesOf, statsOf, tableDims, tableRows, type OutputShape, type Table } from "./shape";

/** The most cells the interval view draws (the table below the chart has them all). */
const MAX_INTERVALS = 50;
/** The most facet levels offered in the picker of an output over time. */
const MAX_FACETS = 200;

export function ScoreShapeViews({ summary, outputs, runId }: { summary?: ScoreSummary | null | undefined; outputs: ArtifactLink[]; runId?: string | undefined }) {
  const link = outputs.find((o) => o.name === SCORE_FIELDS_VIEW);
  const fields = useQuery({
    queryKey: ["artifact", link?.url ?? "", "fields"],
    enabled: Boolean(link),
    staleTime: Infinity,
    queryFn: async ({ signal }) => parseView("fields", await fetchArtifactJson(link!, { signal })),
  });
  const shapes = useMemo(() => shapesOf(summary, fields.data), [summary, fields.data]);
  const [pick, setPick] = useState<string | null>(null);
  const shape =
    shapes.find((s) => s.variable === pick) ??
    (["grid", "areal", "classification", "time"] as const).map((k) => shapes.find((s) => s.kind === k)).find(Boolean) ??
    shapes[0];
  const table: Table | undefined = (summary?.tables ?? []).find((t) => t.variable === shape?.variable);

  return (
    <section aria-label="Results by output" className="grid gap-3 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <ChartSpline className="size-4 text-primary" aria-hidden />
        <h3 className="text-[13px] font-semibold">Results by output</h3>
      </div>
      {shapes.length > 1 ? (
        <Field label="Output" htmlFor="shape-output">
          <NativeSelect id="shape-output" className="w-64" value={shape?.variable ?? ""} onChange={(e) => setPick(e.target.value)}>
            {shapes.map((s) => (
              <option key={s.variable} value={s.variable}>
                {s.variable} ({SHAPE_LABEL[s.kind]})
              </option>
            ))}
          </NativeSelect>
        </Field>
      ) : shape ? (
        <Mono className="text-xs text-muted-foreground">
          {shape.variable} ({SHAPE_LABEL[shape.kind]})
        </Mono>
      ) : null}
      {fields.isError ? <ErrorNotice error={fields.error} title={`Cannot open ${SCORE_FIELDS_VIEW}`} /> : null}
      {!shape ? (
        link && fields.isPending ? <LoadingRows rows={4} /> : null
      ) : shape.kind === "grid" ? (
        <GridView key={shape.variable} fields={shape.fields} view={fields.data} />
      ) : shape.kind === "areal" || shape.kind === "classification" ? (
        <RunPlotsView key={shape.variable} ids={shape.plots} runId={runId} outputs={outputs} />
      ) : table ? (
        <TableView key={shape.variable} shape={shape} table={table} />
      ) : null}
      {summary?.truncated ? <p className="text-xs text-muted-foreground">The summary is too large to send in full, so some outputs or cells are left out. Download the outputs below for all of them.</p> : null}
    </section>
  );
}

function GridView({ fields, view }: { fields: FieldEntry[]; view: FieldsView | undefined }) {
  const [pick, setPick] = useState(0);
  const field = fields[Math.min(pick, fields.length - 1)];
  if (!field) return null;
  return (
    <div className="grid gap-3">
      {fields.length > 1 ? <Picker label="Facet" value={String(pick)} options={fields.map((f, i) => ({ value: String(i), label: facetLabel(f.facet) }))} onChange={(v) => setPick(Number(v))} /> : null}
      {field.description ? <p className="text-[13px] text-muted-foreground">{field.description}</p> : null}
      <FieldViewer key={`${pick}:${facetLabel(field.facet)}`} field={field} />
      <ViewNotes warnings={view?.warnings_plain} />
    </div>
  );
}

/** The run's own plots of an output (an area map, a classifier's curves), each with its caption. */
function RunPlotsView({ ids, runId, outputs }: { ids: readonly string[]; runId: string | undefined; outputs: ArtifactLink[] }) {
  const plots = useRunPlots(runId);
  const resolveLink = useMemo(() => outputLinks(outputs), [outputs]);
  const linksKey = useMemo(() => outputs.map((o) => o.url).join("|"), [outputs]);
  if (plots.isError) return <ErrorNotice error={plots.error} title="Cannot load the plots of the run" />;
  if (runId && plots.isPending) return <LoadingRows rows={4} />;
  const items = plots.data?.items ?? [];
  return (
    <div className="grid gap-5">
      {ids.map((id) => {
        const plot = items.find((p) => p.id === id);
        return plot ? (
          <RunPlot key={id} runId={runId ?? ""} plot={plot} resolveLink={resolveLink} linksKey={linksKey} />
        ) : (
          <p key={id} className="text-[13px] text-muted-foreground">
            <Mono>{id}</Mono>: The run did not make this plot.
          </p>
        );
      })}
    </div>
  );
}

function TableView({ shape, table }: { shape: Extract<OutputShape, { kind: "time" | "categorical" | "scalar" }>; table: Table }) {
  if (shape.kind === "time") return <TimeView table={table} time={shape.time} series={shape.series} />;
  if (shape.kind === "scalar") return <ScalarView table={table} />;
  return <IntervalsView table={table} dims={shape.dims} />;
}

/** A line with its band along the time dim; coloured by a small second dim, else one facet at a time. */
function TimeView({ table, time, series }: { table: Table; time: string; series?: string | undefined }) {
  const facets = useMemo(() => tableDims(table).filter((d) => d !== time && d !== series), [table, time, series]);
  const rows = useMemo(() => tableRows(table), [table]);
  const facetOf = useCallback((r: Row) => facets.map((f) => String(r[f] ?? "")).join(" · "), [facets]);
  const options = useMemo(() => [...new Set(rows.map(facetOf))].slice(0, MAX_FACETS), [rows, facetOf]);
  const [facet, setFacet] = useState(options[0] ?? "");
  const { center, band } = statsOf(table);
  const plot = useMemo(() => {
    if (!center) return null;
    const shown = facets.length ? rows.filter((r) => facetOf(r) === facet) : rows;
    return quickChart(shown, { x: time, y: center, lower: band?.lower, upper: band?.upper, color: series }, isDateDim(table, time));
  }, [rows, facets, facetOf, facet, center, band, time, series, table]);
  if (!plot) return <p className="text-[13px] text-muted-foreground">The summary has no mean, median or point value to draw.</p>;
  return (
    <div className="grid gap-3">
      {facets.length ? <Picker label={facets.join(" · ")} value={facet} options={options.map((o) => ({ value: o, label: o }))} onChange={setFacet} /> : null}
      <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={`${table.variable} over ${time}`} exportName={`${table.variable}-over-${time}`} height={280} />
      {band ? <p className="text-xs text-muted-foreground">The band is the {band.label} interval.</p> : null}
    </div>
  );
}

/** An estimate with its interval per cell (the first {@link MAX_INTERVALS}). */
function IntervalsView({ table, dims }: { table: Table; dims: string[] }) {
  const rows = useMemo(() => intervalRows(table, dims), [table, dims]);
  const plot = useMemo(() => (rows.some((r) => r.mean !== null) ? forestPlot(rows.slice(0, MAX_INTERVALS), { title: table.variable }) : null), [rows, table.variable]);
  if (!plot) return <p className="text-[13px] text-muted-foreground">The summary has no mean, median or point value to draw.</p>;
  return (
    <ExplorerPlot
      plot={plot}
      title={`${table.variable} by ${dims.join(" · ")}`}
      caption={rows.length > MAX_INTERVALS ? `The first ${MAX_INTERVALS} of ${rows.length.toLocaleString("en")} cells. The outputs below have them all.` : undefined}
    />
  );
}

/** One value: the central statistic, its band and any other statistic, as tiles. */
function ScalarView({ table }: { table: Table }) {
  const row = table.rows[0] ?? [];
  const { center, band } = statsOf(table);
  const at = (name: string) => row[table.columns.indexOf(name)] ?? null;
  const fmt = (v: Table["rows"][number][number]) => (typeof v === "number" ? formatNumber(v) : v === null ? "—" : String(v));
  const tiles: [string, string][] = [];
  if (center) tiles.push([center, fmt(at(center))]);
  if (band) tiles.push([band.label, `${fmt(at(band.lower))} – ${fmt(at(band.upper))}`]);
  for (const c of table.columns) if (c !== center && c !== band?.lower && c !== band?.upper) tiles.push([c, fmt(at(c))]);
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label={`${table.variable}: summary`}>
      {tiles.map(([label, value]) => (
        <div key={label} className="grid gap-0.5 rounded-md border bg-muted/30 px-3 py-2">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="text-[15px] font-semibold tabular">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
