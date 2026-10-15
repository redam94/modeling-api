import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Download, FileJson, Table2 } from "lucide-react";
import { useMemo, useState } from "react";

import type { ArtifactLink } from "@/api/types";
import { ErrorNotice, JsonBlock, LoadingRows, Mono } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Button } from "@/components/ui/button";
import { Field, NativeSelect } from "@/components/ui/primitives";
import { quickChart } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { decodeArrow, type DecodedTable, type Row } from "@/lib/arrow";
import { fetchArtifactBytes, fetchArtifactJson } from "@/lib/artifacts";
import { formatBytes, formatNumber, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

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
    queryFn: async ({ signal }) => decodeArrow(await fetchArtifactBytes(link, { signal })),
  });
  if (q.isPending) return <LoadingRows rows={4} />;
  if (q.isError) return <ErrorNotice error={q.error} title={`${link.name} could not be opened`} />;
  return <ResultTable table={q.data} name={link.name} />;
}

function ResultTable({ table, name }: { table: DecodedTable; name: string }) {
  const columns = useMemo<ColumnDef<Row, unknown>[]>(
    () =>
      table.columns.map((c) => ({
        accessorKey: c.name,
        header: c.name,
        cell: ({ getValue }) => {
          const v = getValue() as Row[string];
          return typeof v === "number" ? <span className="tabular">{formatNumber(v)}</span> : v === null ? <span className="text-muted-foreground">—</span> : String(v);
        },
      })),
    [table.columns],
  );
  const numeric = table.columns.filter((c) => /float|int|decimal/i.test(c.type)).map((c) => c.name);
  const all = table.columns.map((c) => c.name);
  const timeLike = table.columns.find((c) => /timestamp|date/i.test(c.type))?.name;
  const [x, setX] = useState(timeLike ?? all[0] ?? "");
  const [y, setY] = useState(numeric.find((n) => /mean|median|value|y_hat/i.test(n)) ?? numeric[0] ?? "");
  const [band, setBand] = useState(() => {
    const lo = numeric.find((n) => /(lower|lo|hdi.*low|q0?5)$/i.test(n));
    const hi = numeric.find((n) => /(upper|hi|hdi.*high|q95)$/i.test(n));
    return lo && hi ? `${lo}|${hi}` : "";
  });
  const [color, setColor] = useState("");
  const [lower, upper] = band ? band.split("|") : [undefined, undefined];
  const plot = useMemo(
    () => (x && y ? quickChart(table.rows, { x, y, lower, upper, color: color || undefined }, Boolean(timeLike && x === timeLike)) : null),
    [table.rows, x, y, lower, upper, color, timeLike],
  );
  const categorical = table.columns.filter((c) => /utf8|string|dictionary|bool/i.test(c.type)).map((c) => c.name);
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

/** Score outputs: list of signed links; Arrow outputs open as a table and a quick chart. */
export function ScoreResults({ outputs }: { outputs: ArtifactLink[] }) {
  const [open, setOpen] = useState<string | null>(outputs.find(isArrow)?.name ?? outputs[0]?.name ?? null);
  const current = outputs.find((o) => o.name === open) ?? null;
  if (!outputs.length) return <p className="text-muted-foreground">The run finished without output files.</p>;
  return (
    <div className="grid gap-4">
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
              <Button size="sm" variant="ghost" asChild>
                <a href={o.url} download={o.name.split("/").at(-1)} rel="noopener noreferrer" referrerPolicy="no-referrer">
                  <Download /> Download
                </a>
              </Button>
            </span>
          </li>
        ))}
      </ul>
      {current ? isArrow(current) ? <ArrowOutput key={current.name} link={current} /> : isJson(current) ? <JsonOutput key={current.name} link={current} /> : null : null}
    </div>
  );
}
