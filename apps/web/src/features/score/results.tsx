import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, FileJson, Table2 } from "lucide-react";
import { useMemo, useState } from "react";

import { errorMessage } from "@/api/client";
import { useRunPlots } from "@/api/queries";
import type { ArtifactLink, PlotSpec } from "@/api/types";
import { ErrorNotice, JsonBlock, LoadingRows, Mono } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { quickChart } from "@/features/plots/builtin-specs";
import { resolvePlotDatasets } from "@/features/plots/plot-data";
import { PlotFrame } from "@/features/plots/plot-frame";
import type { JsonObject } from "@mapi/plot-sandbox/protocol";
import { decodeArrow, type DecodedTable, type Row } from "@/lib/arrow";
import { fetchArtifactBytes, fetchArtifactJson, MAX_ARTIFACT_BYTES } from "@/lib/artifacts";
import { saveBytes } from "@/lib/download";
import { formatBytes, formatNumber, formatTimestamp, relativeTime } from "@/lib/format";
import { distinctOn, pivotStats } from "@/lib/summary";
import { cn } from "@/lib/utils";
import { toast } from "@/stores/toast";

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
  if (q.isError) return <ErrorNotice error={q.error} title={`${link.name} could not be opened`} />;
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
  const numeric = table.columns.filter((c) => /float|int|decimal/i.test(c.type)).map((c) => c.name);
  const all = table.columns.map((c) => c.name);
  const timeLike = table.columns.find((c) => /timestamp|date/i.test(c.type))?.name;
  const [x, setX] = useState(timeLike ?? all[0] ?? "");
  const [y, setY] = useState(
    numeric.find((n) => /^y_hat_mean$/i.test(n)) ?? numeric.find((n) => /mean|median|value|y_hat/i.test(n)) ?? numeric[0] ?? "",
  );
  const [band, setBand] = useState(() => {
    // Prefer the interval of the chosen variable (y_hat_hdi_90_lower / _upper).
    const stem = y.replace(/_(mean|median)$/i, "");
    const pick = (re: RegExp) => numeric.find((n) => n.startsWith(stem) && re.test(n)) ?? numeric.find((n) => re.test(n));
    const lo = pick(/(lower|lo|hdi.*low|q0?5)$/i);
    const hi = pick(/(upper|hi|hdi.*high|q95)$/i);
    return lo && hi ? `${lo}|${hi}` : "";
  });
  const categorical = table.columns.filter((c) => /utf8|string|dictionary|bool/i.test(c.type)).map((c) => c.name);
  const [color, setColor] = useState(() => categorical.find((c) => c !== x) ?? "");
  const [lower, upper] = band ? band.split("|") : [undefined, undefined];
  const plot = useMemo(() => {
    if (!x || !y) return null;
    const c = { x, y, lower, upper, color: color || undefined };
    const rows = distinctOn(table.rows, [x, c.color, y, lower, upper]);
    return quickChart(rows, c, Boolean(timeLike && x === timeLike));
  }, [table.rows, x, y, lower, upper, color, timeLike]);
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
          </div>
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
  if (q.isError) return <ErrorNotice error={q.error} title={`${link.name} could not be opened`} />;
  return <JsonBlock value={q.data} />;
}

function RunPlot({ plot, outputs }: { plot: PlotSpec; outputs: ArtifactLink[] }) {
  const data = useQuery({
    queryKey: ["run-plot", plot.id, outputs.map((o) => o.url).join("|")],
    staleTime: Infinity,
    queryFn: ({ signal }) =>
      resolvePlotDatasets(
        plot,
        (path) => {
          const link = outputs.find((o) => o.name === path);
          return link ? Promise.resolve(link) : Promise.reject(new Error(`the run has no output named ${path}`));
        },
        signal,
      ),
  });
  return (
    <figure className="grid gap-2">
      <figcaption>
        <span className="text-[13px] font-medium">{plot.title}</span>
        {plot.caption_plain ? <span className="block text-xs text-muted-foreground">{plot.caption_plain}</span> : null}
      </figcaption>
      {data.isError ? (
        <ErrorNotice error={data.error} title="The plot data could not be loaded" />
      ) : data.data ? (
        <PlotFrame kind={plot.kind} spec={plot.spec as JsonObject} datasets={data.data} title={plot.title} exportName={plot.id} />
      ) : (
        <LoadingRows rows={3} />
      )}
    </figure>
  );
}

/** Plots the score run rendered, fetched sanitized from the API and drawn in the sandbox. */
function RunPlots({ runId, outputs }: { runId: string; outputs: ArtifactLink[] }) {
  const plots = useRunPlots(runId);
  if (plots.isError) return <ErrorNotice error={plots.error} title="The run's plots could not be loaded" />;
  const items = plots.data?.items ?? [];
  if (!items.length) return null;
  return (
    <section aria-label="Run plots" className="grid gap-4">
      {items.map((p) => (
        <RunPlot key={p.id} plot={p} outputs={outputs} />
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
          toast.error(`${filename} could not be downloaded`, errorMessage(e));
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
 * of signed links; Arrow outputs open as a table and a quick chart.
 */
export function ScoreResults({ outputs, runId }: { outputs: ArtifactLink[]; runId?: string | undefined }) {
  const [open, setOpen] = useState<string | null>(outputs.find(isArrow)?.name ?? outputs[0]?.name ?? null);
  const current = outputs.find((o) => o.name === open) ?? null;
  if (!outputs.length) return <p className="text-muted-foreground">The run finished without output files.</p>;
  return (
    <div className="grid gap-4">
      {runId ? <RunPlots runId={runId} outputs={outputs} /> : null}
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
