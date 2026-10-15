import { useQuery } from "@tanstack/react-query";
import { Grid3x3, Search } from "lucide-react";
import { useMemo, useState } from "react";

import { datasetArtifactLink } from "@/api/queries";
import type { CoordSummary, DataSchema, DatasetVersion } from "@/api/types";
import { EmptyState, ErrorNotice, LoadingRows, Mono } from "@/components/common";
import { Badge, Input, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { missingnessHeatmap } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { fetchArtifactJson } from "@/lib/artifacts";
import { formatNumber } from "@/lib/format";
import { parsePreview, parseProfile, type PreviewView, type ProfileView } from "@/lib/views";

export function SchemaTable({ schema }: { schema: DataSchema }) {
  const dims = Object.entries(schema.dims ?? {});
  const vars = Object.entries(schema.vars ?? {});
  return (
    <div className="grid gap-6">
      <div>
        <h3 className="mb-2 text-[13px] font-semibold">Dims</h3>
        <Table>
          <THead>
            <TR>
              <TH>Name</TH>
              <TH>Kind</TH>
              <TH>Score-time policy</TH>
              <TH>Frequency</TH>
              <TH>New levels</TH>
            </TR>
          </THead>
          <TBody>
            {dims.map(([name, d]) => (
              <TR key={name}>
                <TD><Mono>{name}</Mono></TD>
                <TD>{d.kind}</TD>
                <TD><Badge variant="outline">{d.policy ?? "fixed"}</Badge></TD>
                <TD>{d.freq ?? "—"}</TD>
                <TD>{d.policy === "new_levels" ? (d.new_level_strategy ?? "error") : "—"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
      <div>
        <h3 className="mb-2 text-[13px] font-semibold">Variables</h3>
        <Table>
          <THead>
            <TR>
              <TH>Name</TH>
              <TH>Dims</TH>
              <TH>Type</TH>
              <TH>Role</TH>
              <TH>Units</TH>
              <TH>Missing</TH>
            </TR>
          </THead>
          <TBody>
            {vars.map(([name, v]) => (
              <TR key={name}>
                <TD><Mono>{name}</Mono></TD>
                <TD><Mono className="text-muted-foreground">({(v.dims ?? []).join(", ")})</Mono></TD>
                <TD>{v.dtype}</TD>
                <TD><Badge variant={v.role === "target" ? "default" : "secondary"}>{v.role ?? "covariate"}</Badge></TD>
                <TD>{v.units ?? "—"}</TD>
                <TD>{v.missing ?? "forbid"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </div>
      {schema.ragged?.length ? (
        <p className="text-xs text-muted-foreground">
          Ragged: {schema.ragged.map((r) => `${r.within_dim} within ${r.group_dim} (${r.form})`).join("; ")}
        </p>
      ) : null}
    </div>
  );
}

function CoordValues({ name, c }: { name: string; c: CoordSummary }) {
  const [needle, setNeedle] = useState("");
  const values = (c.values ?? []).map(String);
  const filtered = needle ? values.filter((v) => v.toLowerCase().includes(needle.toLowerCase())) : values;
  return (
    <div className="grid gap-2 rounded-lg border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="font-semibold">{name}</Mono>
        <Badge variant="outline">{c.kind}</Badge>
        <span className="text-xs text-muted-foreground tabular">{c.size.toLocaleString("en")} values</span>
        {c.freq ? <Badge variant="secondary">{c.freq}</Badge> : null}
        {c.contiguous === false ? <Badge variant="warning">gaps</Badge> : null}
      </div>
      {c.start != null || c.end != null ? (
        <p className="text-xs text-muted-foreground">
          <Mono>{String(c.start ?? "?")}</Mono> to <Mono>{String(c.end ?? "?")}</Mono>
          {c.step != null ? ` step ${c.step}` : ""}
        </p>
      ) : null}
      {values.length ? (
        <>
          {values.length > 12 ? (
            <div className="relative max-w-56">
              <Search className="pointer-events-none absolute top-2 left-2 size-3.5 text-muted-foreground" />
              <Input aria-label={`Search ${name}`} value={needle} onChange={(e) => setNeedle(e.target.value)} className="h-7 pl-7 text-xs" />
            </div>
          ) : null}
          <div className="flex max-h-36 flex-wrap gap-1 overflow-y-auto">
            {filtered.slice(0, 500).map((v) => (
              <span key={v} className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px]">{v}</span>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}

export function CoordBrowser({ version }: { version: DatasetVersion }) {
  const coords = Object.entries(version.summary?.coords ?? {});
  if (!coords.length) return <p className="text-muted-foreground">No coordinate summary for this version.</p>;
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {coords.map(([name, c]) => <CoordValues key={name} name={name} c={c} />)}
    </div>
  );
}

function useDatasetView<T>(version: DatasetVersion, name: "profile.json" | "preview.json", parse: (raw: unknown) => T, available: boolean) {
  return useQuery({
    queryKey: ["datasets", version.dataset_id, "versions", version.version, "view", name],
    enabled: available && version.status === "ready",
    staleTime: Infinity,
    queryFn: async ({ signal }) => {
      const link = await datasetArtifactLink(version.dataset_id, version.version, name);
      return parse(await fetchArtifactJson(link, { signal }));
    },
  });
}

export function MissingnessPanel({ version }: { version: DatasetVersion }) {
  const profile = useDatasetView<ProfileView>(version, "profile.json", parseProfile, Boolean(version.profile));
  const plot = useMemo(() => (profile.data?.missingness ? missingnessHeatmap(profile.data.missingness) : null), [profile.data]);
  if (!version.profile) return <EmptyState icon={<Grid3x3 />} title="No profile for this version">The ingest run did not write a profile.</EmptyState>;
  if (profile.isPending) return <LoadingRows rows={3} />;
  if (profile.isError) return <ErrorNotice error={profile.error} title="The profile could not be loaded" />;
  const vars = Object.entries(profile.data.vars);
  return (
    <div className="grid gap-6">
      {plot ? (
        <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title="Missingness heatmap" exportName="missingness" height={260} />
      ) : (
        <p className="text-muted-foreground">No missing-value grid in the profile.</p>
      )}
      <Table>
        <THead>
          <TR>
            <TH>Variable</TH>
            <TH>Type</TH>
            <TH className="text-right">Values</TH>
            <TH className="text-right">Missing</TH>
            <TH className="text-right">Min</TH>
            <TH className="text-right">Mean</TH>
            <TH className="text-right">Max</TH>
            <TH className="text-right">SD</TH>
          </TR>
        </THead>
        <TBody>
          {vars.map(([name, v]) => (
            <TR key={name}>
              <TD><Mono>{name}</Mono></TD>
              <TD>{v.dtype}</TD>
              <TD className="text-right tabular">{v.count.toLocaleString("en")}</TD>
              <TD className={v.missing > 0 ? "text-right text-warning tabular" : "text-right tabular"}>
                {v.missing.toLocaleString("en")}
                {v.count + v.missing > 0 ? ` (${Math.round((100 * v.missing) / (v.count + v.missing))}%)` : ""}
              </TD>
              <TD className="text-right tabular">{formatNumber(v.min)}</TD>
              <TD className="text-right tabular">{formatNumber(v.mean)}</TD>
              <TD className="text-right tabular">{formatNumber(v.max)}</TD>
              <TD className="text-right tabular">{formatNumber(v.std)}</TD>
            </TR>
          ))}
        </TBody>
      </Table>
    </div>
  );
}

export function PreviewPanel({ version }: { version: DatasetVersion }) {
  const preview = useDatasetView<PreviewView>(version, "preview.json", parsePreview, Boolean(version.preview));
  if (!version.preview) return <p className="text-muted-foreground">No preview for this version.</p>;
  if (preview.isPending) return <LoadingRows rows={5} />;
  if (preview.isError) return <ErrorNotice error={preview.error} title="The preview could not be loaded" />;
  const p = preview.data;
  return (
    <div className="grid gap-2">
      <p className="text-xs text-muted-foreground">
        {p.rows.length.toLocaleString("en")} sample rows{p.total_rows != null ? ` of ${p.total_rows.toLocaleString("en")}` : ""}, long format.
      </p>
      <div className="max-h-[28rem] overflow-auto rounded-lg border">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-card">
            <tr className="border-b">
              {p.columns.map((c) => (
                <th key={c.name} className="px-2 py-2 text-left font-medium whitespace-nowrap">
                  <span className={c.role === "dim" ? "text-primary" : ""}>{c.name}</span>
                  <span className="ml-1 font-normal text-muted-foreground">{c.dtype}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {p.rows.map((r, i) => (
              <tr key={i} className="border-b last:border-0">
                {r.map((v, j) => (
                  <td key={j} className="px-2 py-1 font-mono whitespace-nowrap tabular">
                    {v === null ? <span className="text-muted-foreground">NaN</span> : String(v)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
