import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, FileJson, Table2 } from "lucide-react";
import { useMemo, useState } from "react";

import { errorMessage } from "@/api/client";
import { useRunPlots } from "@/api/queries";
import type { ArtifactLink } from "@/api/types";
import { ErrorNotice, JsonBlock, LoadingRows, Mono } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { quickChart } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { outputLinks, RunPlot } from "@/features/runs/run-plots";
import { decodeArrow, type DecodedTable, type Row } from "@/lib/arrow";
import { fetchArtifactBytes, fetchArtifactJson, MAX_ARTIFACT_BYTES } from "@/lib/artifacts";
import { saveBytes } from "@/lib/download";
import { formatBytes, formatNumber, formatTimestamp, relativeTime } from "@/lib/format";
import { pivotStats } from "@/lib/summary";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

import { categoricalColumns, cellLevels, facetsFor, MAX_CELLS, numericColumns, quickChartChoice, quickChartRows, timeColumn } from "./quick-chart";

function isArrow(link: ArtifactLink): boolean {
  return /\.(arrow|feather|ipc)$/i.test(link.name) || (link.content_type ?? "").includes("arrow");
}

function isJson(link: ArtifactLink): boolean {
  return /\.json$/i.test(link.name) || (link.content_type ?? "").includes("json");
}

function ArrowOutput({ link }: { link: ArtifactLink }) {
  const q = useQuery({
    queryKey: ["artifact", link.url, "arrow"],
    staleTime: Infinity,
    queryFn: async ({ signal }) => pivotStats(decodeArrow(await fetchArtifactBytes(link, { signal }))),
  });
  if (q.isPending) return <LoadingRows rows={4} />;
  if (q.isError) return <ErrorNotice error={q.error} title={`Cannot open ${link.name}`} />;
  return <ResultTable table={q.data} name={link.name} />;
}

function ResultTable({ table, name }: { table: DecodedTable; name: string }) {
  const columns = useMemo<ColumnDef<Row, unknown>[]>(
    () =>
      table.columns.map((c) => {
        const temporal = /timestamp|date/i.test(c.type);
        return {
          accessorKey: c.name,
          header: c.name,
          cell: ({ getValue }) => {
            const v = getValue() as Row[string];
            if (typeof v === "number") return <span className="tabular">{formatNumber(v)}</span>;
            if (v === null) return <span className="text-muted-foreground">—</span>;
            return temporal && typeof v === "string" ? <span className="tabular whitespace-nowrap">{formatTimestamp(v)}</span> : String(v);
          },
        };
      }),
    [table.columns],
  );
  const numeric = numericColumns(table);
  const all = table.columns.map((c) => c.name);
  const timeLike = timeColumn(table);
  const categorical = categoricalColumns(table);
  const initial = useMemo(() => quickChartChoice(table), [table]);
  const [x, setX] = useState(initial.x);
  const [y, setY] = useState(initial.y);
  const [band, setBand] = useState(initial.band);
  const [color, setColor] = useState(initial.color);
  // A table with more dims than x and the colour (a grid: quarter × row × col) is drawn one cell at a time.
  const facets = useMemo(() => facetsFor(table, x, color), [table, x, color]);
  const levels = useMemo(() => cellLevels(table.rows, facets, y), [table.rows, facets, y]);
  const [pickedCell, setCell] = useState<string | null>(null);
  const cell = pickedCell !== null && levels.cells.includes(pickedCell) ? pickedCell : levels.first;
  const [lower, upper] = band ? band.split("|") : [undefined, undefined];
  const plot = useMemo(() => {
    if (!x || !y) return null;
    const rows = quickChartRows(table.rows, { x, y, band, color, facets }, facets.length ? cell : null);
    return quickChart(rows, { x, y, lower, upper, color: color || undefined }, Boolean(timeLike && x === timeLike));
  }, [table.rows, x, y, band, lower, upper, color, facets, cell, timeLike]);
  return (
    <div className="grid gap-4">
      <p className="text-xs text-muted-foreground">
        {table.rows.length.toLocaleString("en")} rows{table.truncated ? ` of ${table.totalRows.toLocaleString("en")} (truncated)` : ""}
      </p>
      {numeric.length ? (
        <div className="grid gap-3 rounded-lg border p-3">
          <div className="flex flex-wrap gap-3">
            <Field label="x" htmlFor="qc-x">
              <NativeSelect id="qc-x" value={x} onChange={(e) => setX(e.target.value)} className="h-8 w-40">
                {all.map((c) => <option key={c}>{c}</option>)}
              </NativeSelect>
            </Field>
            <Field label="y" htmlFor="qc-y">
              <NativeSelect id="qc-y" value={y} onChange={(e) => setY(e.target.value)} className="h-8 w-40">
                {numeric.map((c) => <option key={c}>{c}</option>)}
              </NativeSelect>
            </Field>
            <Field label="Band" htmlFor="qc-band">
              <NativeSelect id="qc-band" value={band} onChange={(e) => setBand(e.target.value)} className="h-8 w-48">
                <option value="">none</option>
                {numeric.flatMap((lo) => numeric.filter((hi) => hi !== lo && hi > lo).map((hi) => <option key={`${lo}|${hi}`} value={`${lo}|${hi}`}>{lo} – {hi}</option>))}
              </NativeSelect>
            </Field>
            <Field label="Colour by" htmlFor="qc-color">
              <NativeSelect id="qc-color" value={color} onChange={(e) => setColor(e.target.value)} className="h-8 w-36">
                <option value="">none</option>
                {categorical.map((c) => <option key={c}>{c}</option>)}
              </NativeSelect>
            </Field>
            {facets.length ? (
              <Field label={`Cell (${facets.join(" · ")})`} htmlFor="qc-cell">
                <NativeSelect id="qc-cell" value={cell} onChange={(e) => setCell(e.target.value)} className="h-8 w-40">
                  {levels.cells.map((c) => <option key={c}>{c}</option>)}
                </NativeSelect>
              </Field>
            ) : null}
          </div>
          {facets.length ? (
            <p className="text-xs text-muted-foreground">
              The table has one series for each {facets.join(" · ")} cell. The chart shows one cell.
              {levels.total > MAX_CELLS ? ` The list shows the first ${MAX_CELLS} of ${levels.total.toLocaleString("en")} cells.` : ""}
            </p>
          ) : null}
          {plot ? <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={`${name} chart`} exportName={name.replace(/\.[a-z]+$/i, "")} /> : null}
        </div>
      ) : null}
      <DataTable label={name} data={table.rows} columns={columns} maxHeight={420} />
    </div>
  );
}

function JsonOutput({ link }: { link: ArtifactLink }) {
  const q = useQuery({
    queryKey: ["artifact", link.url, "json"],
    staleTime: Infinity,
    queryFn: ({ signal }) => fetchArtifactJson(link, { signal }),
  });
  if (q.isPending) return <LoadingRows rows={3} />;
  if (q.isError) return <ErrorNotice error={q.error} title={`Cannot open ${link.name}`} />;
  return <JsonBlock value={q.data} />;
}

/**
 * Plots the score run rendered, fetched sanitized from the API and drawn in the sandbox; the
 * ids in `hide` are left out (the page shows those outputs another way).
 */
function RunPlots({ runId, outputs, hide }: { runId: string; outputs: ArtifactLink[]; hide: readonly string[] }) {
  const plots = useRunPlots(runId);
  if (plots.isError) return <ErrorNotice error={plots.error} title="Cannot load the plots of the run" />;
  const items = (plots.data?.items ?? []).filter((p) => !hide.includes(p.id));
  if (!items.length) return null;
  return (
    <section aria-label="Run plots" className="grid gap-4">
      {items.map((p) => (
        <RunPlot key={p.id} runId={runId} plot={p} resolveLink={outputLinks(outputs)} linksKey={outputs.map((o) => o.url).join("|")} />
      ))}
    </section>
  );
}

/**
 * Download one output. The signed URL is on the storage origin, where `<a download>` is
 * ignored and the tab would navigate away (losing the in-memory results), so the bytes are
 * fetched, checked against the CRC32C, and saved from a Blob. Outputs too large to buffer
 * open in a new tab instead.
 */
function OutputDownload({ link }: { link: ArtifactLink }) {
  const [busy, setBusy] = useState(false);
  const filename = link.name.split("/").at(-1) || "output";
  const label = `Download ${filename}`;
  if (link.size > MAX_ARTIFACT_BYTES) {
    return (
      <Button size="sm" variant="ghost" asChild>
        <a href={link.url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" aria-label={label}>
          <Download /> Download
        </a>
      </Button>
    );
  }
  return (
    <Button
      size="sm"
      variant="ghost"
      aria-label={label}
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          saveBytes(await fetchArtifactBytes(link), filename);
        } catch (e) {
          toast.error(`Cannot download ${filename}`, errorMessage(e));
        } finally {
          setBusy(false);
        }
      }}
    >
      <Download /> {busy ? "Downloading…" : "Download"}
    </Button>
  );
}

/**
 * Score outputs: the run's own plots (forecast fan, counterfactual band) first, then the list
 * of signed links; Arrow outputs open as a table and a quick chart. `hidePlots` names run
 * plots to leave out.
 */
export function ScoreResults({ outputs, runId, hidePlots = [] }: { outputs: ArtifactLink[]; runId?: string | undefined; hidePlots?: readonly string[] }) {
  const [open, setOpen] = useState<string | null>(outputs.find(isArrow)?.name ?? outputs[0]?.name ?? null);
  const current = outputs.find((o) => o.name === open) ?? null;
  if (!outputs.length) return <p className="text-muted-foreground">The run finished, but it did not make output files.</p>;
  return (
    <div className="grid gap-4">
      {runId ? <RunPlots runId={runId} outputs={outputs} hide={hidePlots} /> : null}
      <ul className="grid gap-1">
        {outputs.map((o) => (
          <li key={o.name} className={cn("flex flex-wrap items-center gap-3 rounded-md border px-3 py-2", o.name === open && "border-primary/50 bg-accent/40")}>
            {isArrow(o) ? <Table2 className="size-4 text-muted-foreground" /> : <FileJson className="size-4 text-muted-foreground" />}
            <Mono>{o.name}</Mono>
            <span className="text-xs text-muted-foreground">{formatBytes(o.size)}</span>
            <span className="text-xs text-muted-foreground">link expires {relativeTime(o.expires_at)}</span>
            <span className="ml-auto flex gap-1">
              {isArrow(o) || isJson(o) ? (
                <Button size="sm" variant="ghost" onClick={() => setOpen(o.name)}>
                  Open
                </Button>
              ) : null}
              <OutputDownload link={o} />
            </span>
          </li>
        ))}
      </ul>
      {current ? isArrow(current) ? <ArrowOutput key={current.name} link={current} /> : isJson(current) ? <JsonOutput key={current.name} link={current} /> : null : null}
    </div>
  );
}
