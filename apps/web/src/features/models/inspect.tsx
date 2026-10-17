import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { AlertTriangle, CheckCircle2, ChevronRight, FolderTree, ShieldCheck, XCircle } from "lucide-react";
import { useMemo, useState } from "react";

import { modelArtifactLink, qk, useProjectScope } from "@/api/queries";
import type { Capabilities, Diagnostic, ModelVersion, Scorecard } from "@/api/types";
import { EmptyState, ErrorNotice, JsonBlock, KeyValues, LoadingRows, Mono } from "@/components/common";
import { DataTable } from "@/components/data-table";
import { Badge, Card, CardContent, CardHeader, CardTitle, TBody, TD, TH, THead, TR, Table } from "@/components/ui/primitives";
import { stateArrayPlot } from "@/features/plots/builtin-specs";
import { PlotFrame } from "@/features/plots/plot-frame";
import { fetchArtifactJson } from "@/lib/artifacts";
import { absoluteTime, formatBytes, formatNumber, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import { parsePreprocess, parseTreeIndex, type StateArray, type TreeGroup } from "@/lib/views";

// ----------------------------------------------------------------------- scorecard

const STATUS_ICON = {
  pass: <CheckCircle2 className="size-4 text-success" />,
  warn: <AlertTriangle className="size-4 text-warning" />,
  fail: <XCircle className="size-4 text-destructive" />,
} as const;

export function ScorecardView({ scorecard }: { scorecard: Scorecard | null | undefined }) {
  if (!scorecard) return <p className="text-muted-foreground">This version has no scorecard.</p>;
  return (
    <ul className="grid gap-3">
      {(scorecard.items ?? []).map((item) => (
        <li key={item.code} className="grid grid-cols-[1.25rem_1fr_auto] gap-3 rounded-lg border p-3">
          <span className="pt-0.5">{STATUS_ICON[item.status]}</span>
          <div className="min-w-0">
            <p className="font-medium">{item.title}</p>
            <p className="text-muted-foreground">{item.message_expert ?? item.message_plain}</p>
            {item.suggested_action ? <p className="mt-1 text-xs">{item.suggested_action}</p> : null}
          </div>
          {item.value != null ? (
            <div className="text-right text-xs tabular">
              <p className="font-mono text-[13px]">{formatNumber(item.value)}</p>
              {item.threshold != null ? <p className="text-muted-foreground">limit {formatNumber(item.threshold)}</p> : null}
              {item.metric ? <p className="text-muted-foreground">{item.metric}</p> : null}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

// ----------------------------------------------------------------------- diagnostics

const SEVERITY_VARIANT = { info: "info", warning: "warning", error: "danger" } as const;

const diagnosticColumns: ColumnDef<Diagnostic, unknown>[] = [
  { accessorKey: "severity", header: "Severity", cell: ({ row }) => <Badge variant={SEVERITY_VARIANT[row.original.severity ?? "warning"]}>{row.original.severity ?? "warning"}</Badge> },
  { accessorKey: "code", header: "Code", cell: ({ getValue }) => <Mono>{String(getValue())}</Mono> },
  { accessorKey: "var", header: "Variable", cell: ({ getValue }) => <Mono>{(getValue() as string | null) ?? "—"}</Mono> },
  {
    id: "coords",
    header: "Coords",
    accessorFn: (d) => Object.entries(d.coords ?? {}).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", "),
    cell: ({ getValue }) => <span className="font-mono text-xs text-muted-foreground">{String(getValue()) || "—"}</span>,
  },
  { accessorKey: "metric", header: "Metric", cell: ({ getValue }) => (getValue() as string | null) ?? "—" },
  { accessorKey: "value", header: "Value", cell: ({ getValue }) => <span className="tabular">{formatNumber(getValue() as number | null)}</span> },
  { accessorKey: "threshold", header: "Threshold", cell: ({ getValue }) => <span className="tabular text-muted-foreground">{formatNumber(getValue() as number | null)}</span> },
  { accessorKey: "message_expert", header: "Message", cell: ({ getValue }) => <span className="line-clamp-2">{String(getValue())}</span> },
];

/** Per-variable diagnostics (one row per variable/coord; never an aggregated label). */
export function DiagnosticsTable({ diagnostics, capabilities }: { diagnostics: Diagnostic[]; capabilities: Capabilities | null | undefined }) {
  return (
    <div className="grid gap-3">
      {capabilities && capabilities.loo_valid === false ? (
        <div className="flex items-start gap-2 rounded-lg border border-warning/40 bg-warning/8 p-3 text-[13px]">
          <AlertTriangle className="mt-0.5 size-4 text-warning" />
          <span>
            LOO is not valid for this model{capabilities.loo_invalid_reason ? `: ${capabilities.loo_invalid_reason}` : "."} The app does not show Pareto-k and elpd_loo.
          </span>
        </div>
      ) : null}
      <DataTable
        label="Diagnostics"
        data={diagnostics}
        columns={diagnosticColumns}
        filterPlaceholder="Filter by code, variable or message"
        initialSorting={[{ id: "severity", desc: true }]}
        empty="This version has no diagnostics."
      />
    </div>
  );
}

// ----------------------------------------------------------------------- views

function useBundleView<T>(mv: ModelVersion, name: string, parse: (raw: unknown) => T) {
  const present = (mv.artifacts ?? []).some((a) => a.name === name);
  const scope = useProjectScope();
  return {
    present,
    query: useQuery({
      queryKey: [...qk.modelVersion(scope.key, mv.model_id, mv.version), "view", name],
      enabled: present,
      staleTime: Infinity,
      queryFn: async ({ signal }) => parse(await fetchArtifactJson(await modelArtifactLink(mv.model_id, mv.version, name), { signal })),
    }),
  };
}

function GroupDetail({ group }: { group: TreeGroup }) {
  const vars = Object.entries(group.vars);
  const coords = Object.entries(group.coords);
  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap gap-1">
        {Object.entries(group.dims).map(([d, n]) => (
          <span key={d} className="rounded border bg-muted/60 px-1.5 py-0.5 font-mono text-xs">
            {d} <span className="text-muted-foreground">{n}</span>
          </span>
        ))}
      </div>
      {vars.length ? (
        <Table>
          <THead>
            <TR>
              <TH>Variable</TH>
              <TH>Dims</TH>
              <TH>Shape</TH>
              <TH>Type</TH>
              <TH>Units</TH>
            </TR>
          </THead>
          <TBody>
            {vars.map(([name, v]) => (
              <TR key={name}>
                <TD>
                  <Mono className="font-medium">{name}</Mono>
                  {v.description ? <span className="block text-xs text-muted-foreground">{v.description}</span> : null}
                </TD>
                <TD><Mono className="text-muted-foreground">({v.dims.join(", ")})</Mono></TD>
                <TD className="tabular">{v.shape.join(" × ") || "scalar"}</TD>
                <TD>{v.dtype}</TD>
                <TD>{v.units ?? "—"}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      ) : null}
      {coords.length ? (
        <div className="grid gap-1">
          <h4 className="text-xs font-medium text-muted-foreground">Coordinates</h4>
          {coords.map(([name, c]) => (
            <p key={name} className="text-xs">
              <Mono className="font-medium">{name}</Mono> <span className="text-muted-foreground">({c.dims.join(", ")}) {c.dtype}, {c.size} values</span>
              {c.values?.length ? <span className="ml-1 font-mono text-muted-foreground">{c.values.slice(0, 8).map(String).join(", ")}{c.values.length > 8 ? ", …" : ""}</span> : null}
            </p>
          ))}
        </div>
      ) : null}
      {Object.keys(group.attrs).length ? <JsonBlock value={group.attrs} maxHeight="12rem" /> : null}
    </div>
  );
}

/** DataTree browser over `ui/tree_index.json` (metadata only; arrays stay in the bundle). */
export function TreeBrowser({ mv }: { mv: ModelVersion }) {
  const { present, query } = useBundleView(mv, "ui/tree_index.json", parseTreeIndex);
  const [selected, setSelected] = useState<string | null>(null);
  if (!present) return <EmptyState icon={<FolderTree />} title="This bundle has no tree index">The runner writes ui/tree_index.json for fitted bundles.</EmptyState>;
  if (query.isPending) return <LoadingRows rows={5} />;
  if (query.isError) return <ErrorNotice error={query.error} title="Cannot load the tree index" />;
  const groups = query.data.groups;
  const current = groups.find((g) => g.path === selected) ?? groups[0];
  return (
    <div className="grid gap-5 lg:grid-cols-[16rem_1fr]">
      <nav aria-label="DataTree groups" className="grid content-start gap-0.5 rounded-lg border p-2">
        {groups.map((g) => {
          const depth = g.path.split("/").length - 2;
          return (
            <button
              key={g.path}
              type="button"
              onClick={() => setSelected(g.path)}
              aria-current={current?.path === g.path ? "true" : undefined}
              className={cn("flex items-center gap-1 rounded px-2 py-1 text-left font-mono text-xs hover:bg-accent", current?.path === g.path && "bg-accent font-semibold")}
              style={{ paddingLeft: `${0.5 + Math.max(0, depth) * 0.9}rem` }}
            >
              <ChevronRight className="size-3 text-muted-foreground" />
              {g.path.split("/").filter(Boolean).at(-1) ?? "/"}
              <span className="ml-auto text-muted-foreground">{Object.keys(g.vars).length}</span>
            </button>
          );
        })}
      </nav>
      {current ? (
        <div className="min-w-0">
          <Mono className="mb-3 block text-[13px] font-semibold">{current.path}</Mono>
          <GroupDetail group={current} />
        </div>
      ) : null}
    </div>
  );
}

function StateArrayView({ array }: { array: StateArray }) {
  const plot = useMemo(() => stateArrayPlot(array), [array]);
  return (
    <div className="grid gap-1">
      <p className="text-xs">
        <Mono className="font-medium">{array.name}</Mono>
        <span className="text-muted-foreground"> ({array.dims.join(", ")}){array.units ? `, ${array.units}` : ""}</span>
      </p>
      <PlotFrame kind="vega-lite" spec={plot.spec} datasets={plot.datasets} title={`${array.name} state`} exportName={array.name} height={220} />
    </div>
  );
}

/** Learned preprocessing state per step (e.g. the `scale(geo, channel)` heatmap). */
export function PreprocessViewer({ mv }: { mv: ModelVersion }) {
  const { present, query } = useBundleView(mv, "ui/preprocess.json", parsePreprocess);
  if (!present) return <p className="text-muted-foreground">This bundle has no preprocessing view (ui/preprocess.json).</p>;
  if (query.isPending) return <LoadingRows rows={4} />;
  if (query.isError) return <ErrorNotice error={query.error} title="Cannot load the preprocessing state" />;
  if (!query.data.steps.length) return <p className="text-muted-foreground">This model has no preprocessing steps.</p>;
  return (
    <div className="grid gap-4">
      {query.data.steps.map((s) => (
        <Card key={s.id}>
          <CardHeader>
            <div>
              <CardTitle><Mono>{s.id}</Mono></CardTitle>
              <p className="text-xs text-muted-foreground">{s.use}</p>
            </div>
            <div className="flex gap-1">
              {s.kind ? <Badge variant="outline">{s.kind}</Badge> : null}
              {s.stateful ? <Badge variant="secondary">stateful</Badge> : <Badge variant="outline">stateless</Badge>}
            </div>
          </CardHeader>
          <CardContent className="grid gap-4">
            {Object.keys(s.learned).length ? <JsonBlock value={s.learned} maxHeight="10rem" /> : null}
            {s.learnedTruncated ? <p className="text-xs text-muted-foreground">Some learned values are too large to show here. They are in the bundle.</p> : null}
            {s.arrays.map((a) => <StateArrayView key={a.name} array={a} />)}
            {s.omitted.length ? (
              <p className="text-xs text-muted-foreground">
                Not shown because they are too large or have more than two dimensions: <Mono>{s.omitted.join(", ")}</Mono>
              </p>
            ) : null}
            {!s.arrays.length && !s.omitted.length && !Object.keys(s.learned).length ? <p className="text-muted-foreground">No learned state.</p> : null}
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

// ----------------------------------------------------------------------- provenance

export function ProvenancePanel({ mv }: { mv: ModelVersion }) {
  const m = mv.manifest;
  const l = mv.lineage;
  const objects = Object.entries(m.objects ?? {});
  return (
    <div className="grid gap-5 xl:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Signature</CardTitle>
          {m.signature ? <Badge variant="success"><ShieldCheck /> signed</Badge> : <Badge variant="danger">unsigned</Badge>}
        </CardHeader>
        <CardContent>
          <KeyValues
            items={[
              ["Algorithm", m.signature?.alg ?? "—"],
              ["Key id", m.signature ? <Mono key="k">{m.signature.key_id}</Mono> : "—"],
              ["Signed at", absoluteTime(m.signed_at)],
              ["Status", m.status],
              ["Spec hash", <Mono key="s" title={m.spec_hash}>{shortId(m.spec_hash)}</Mono>],
              ["Data hash", m.data_hash ? <Mono key="d" title={m.data_hash}>{shortId(m.data_hash)}</Mono> : "—"],
              ["Catalog digest", <Mono key="c" title={m.catalog_digest}>{shortId(m.catalog_digest)}</Mono>],
              ["Base image", m.base_image ?? "—"],
              ["Overlay", m.overlay_hash ? <Mono key="o">{shortId(m.overlay_hash)}</Mono> : "none"],
            ]}
          />
          <p className="mt-3 text-xs text-muted-foreground">
            The server checks this signature, the tenant and every object generation before it issues any download link.
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Lineage</CardTitle>
        </CardHeader>
        <CardContent>
          <KeyValues
            items={[
              ["Fit run", <Mono key="r">{shortId(l.run_id)} (attempt {l.attempt})</Mono>],
              ["Plugin", l.plugin ?? "—"],
              ["Engine", l.engine ?? "—"],
              ["Seed", l.seed ?? "—"],
              ["Executor", l.executor ?? "—"],
              ["Class", l.resource_class ?? "—"],
              ["Dataset", l.dataset_sha ? <Mono key="ds" title={l.dataset_sha}>{shortId(l.dataset_sha)}</Mono> : "—"],
              ["Side tables", Object.keys(l.side_tables ?? {}).join(", ") || "—"],
              ["Evaluation of", l.evaluation_of ? <Mono key="e">{shortId(l.evaluation_of)}</Mono> : "—"],
              ["Libraries", Object.entries(l.lib_versions ?? {}).map(([k, v]) => `${k} ${v}`).join(", ") || "—"],
            ]}
          />
        </CardContent>
      </Card>
      <Card className="xl:col-span-2">
        <CardHeader>
          <CardTitle>Bundle objects</CardTitle>
          <span className="text-xs text-muted-foreground">{objects.length} objects, pinned by generation</span>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <TR>
                <TH>Name</TH>
                <TH className="text-right">Size</TH>
                <TH>CRC32C</TH>
                <TH className="text-right">Generation</TH>
              </TR>
            </THead>
            <TBody>
              {objects.map(([name, o]) => (
                <TR key={name}>
                  <TD><Mono>{name}</Mono></TD>
                  <TD className="text-right tabular">{formatBytes(o.size)}</TD>
                  <TD><Mono className="text-muted-foreground">{o.crc32c}</Mono></TD>
                  <TD className="text-right tabular">{o.generation}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
