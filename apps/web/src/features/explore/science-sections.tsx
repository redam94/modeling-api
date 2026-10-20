/**
 * The Data Explorer sections for scientific data (v0.10): the detected structure,
 * images and fields, profiles and spectra, maps of areas, networks, trees (a variable that
 * holds a tree; not a tree dataset), directions and times of day, and points. Each one shows
 * the plain reading first, then pickers, a plot (plot sandbox, or the canvas field viewer for
 * images) and the statistics (open in Expert mode). View strings are user data: text only.
 * v0.14: colour (RGB) images; the gallery of an image stack is in `gallery-section.tsx`.
 */
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/primitives";
import { Button } from "@/components/ui/button";
import { PlotFrame } from "@/features/plots/plot-frame";
import { cn } from "@/lib/utils";

import { FieldViewer } from "./field-viewer";
import { circularMean, circularReading, imageReading, mapReading, networkReading, pointsReading, profileReading, structureReading, treeReading } from "./plain";
import {
  circularPairPlot,
  countsHistogramPlot,
  dendrogramPlot,
  frameStatsPlot,
  mapPlot,
  networkMatrixPlot,
  nodeLinkPlot,
  pointsColours,
  pointsDensityPlot,
  pointsPlot,
  profilesPlot,
  rosePlot,
  structureMatrixPlot,
  type PointsColour,
  textOf,
  type SciPlot,
} from "./science-plots";
import { DATA_SECTION_META } from "./sections-meta";
import { shapeSection, shapeWord } from "./shapes";
import { count, num, pct, Picker, PlainSummary, pValue, Statistics, StatTable, ViewNotes } from "./ui";
import {
  seriesLabel,
  type CircularView,
  type DataSection,
  type FieldEntry,
  type ImageEntry,
  type ImagesView,
  type MapView,
  type NetworkView,
  type PointsView,
  type ProfilesView,
  type StructureView,
  type TreeView,
} from "./views";

interface P<V> {
  view: V;
  expert: boolean;
}

/** A plot of either engine in the sandbox, with its accessible title and an optional caption. */
export function SciPlotView({ plot, title, caption, height }: { plot: SciPlot | null; title: string; caption?: string | undefined; height?: number }) {
  if (!plot) return null;
  return (
    <div className="grid gap-1.5">
      <PlotFrame kind={plot.kind} spec={plot.spec} datasets={plot.datasets} title={title} height={height ?? 300} exportName={title.toLowerCase().replace(/[^a-z0-9]+/g, "-")} />
      {caption ? <p className="text-xs text-muted-foreground">{caption}</p> : null}
    </div>
  );
}

/** A small segmented switch (aria-pressed buttons). */
function Toggle<T extends string>({ label, value, options, onChange }: { label: string; value: T; options: readonly { value: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="grid gap-1">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div className="flex flex-wrap gap-1" role="group" aria-label={label}>
        {options.map((o) => (
          <Button key={o.value} size="sm" variant={o.value === value ? "secondary" : "ghost"} className={cn("h-7 px-2.5", o.value === value && "ring-1 ring-border")} aria-pressed={o.value === value} onClick={() => onChange(o.value)}>
            {o.label}
          </Button>
        ))}
      </div>
    </div>
  );
}

const str = (v: unknown) => (v === null || v === undefined ? "—" : textOf(v));

/** "image: 9 x 12 cells" → "9 x 12 cells" (the chip names the shape already). */
export function shapeDetail(label: string): string {
  const i = label.indexOf(": ");
  return i > 0 && i < 30 ? label.slice(i + 2) : label;
}

// ------------------------------------------------------------------------------ structure

/** The shapes as links to the sections that show them (Overview and Structure). */
export function ShapeChips({ shapes, onSection }: { shapes: readonly { kind: string; label_plain: string; sections?: string[] | undefined }[]; onSection?: ((s: DataSection) => void) | undefined }) {
  if (!shapes.length) return null;
  return (
    <ul className="flex flex-wrap gap-2" aria-label="Shapes in the data" data-testid="shape-chips">
      {shapes.map((s, i) => {
        const target = shapeSection(s);
        const text = (
          <>
            <span className="font-medium">{shapeWord(s.kind)}</span>
            <span className="text-muted-foreground">{shapeDetail(s.label_plain)}</span>
          </>
        );
        return (
          <li key={`${s.kind}-${i}`}>
            {onSection ? (
              <button
                type="button"
                onClick={() => onSection(target)}
                className="flex items-center gap-1.5 rounded-full border bg-card px-3 py-1 text-[12px] hover:bg-accent focus-visible:outline-2 focus-visible:outline-ring"
                title={`Open ${DATA_SECTION_META[target].title}`}
                data-shape={s.kind}
              >
                {text}
              </button>
            ) : (
              <span className="flex items-center gap-1.5 rounded-full border bg-card px-3 py-1 text-[12px]">{text}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function StructureSection({ view, expert, onSection }: P<StructureView> & { onSection?: (s: DataSection) => void }) {
  const reading = structureReading(view);
  const plot = useMemo(() => structureMatrixPlot(view), [view]);
  const notApplicable = Object.entries(view.not_applicable ?? {});
  return (
    <>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <ShapeChips shapes={view.shapes ?? []} onSection={onSection} />
      <SciPlotView plot={plot} title="Variables and their dimensions" caption="Each cell is one dimension of a variable. The colour gives the role of the dimension in the data, the number its size." height={260} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert} label="Dimensions and variables">
        <StatTable
          label="Dimensions"
          rows={view.dims ?? []}
          rowKey={(d) => d.name}
          columns={[
            { key: "name", header: "Dimension", cell: (d) => <span className="font-mono text-xs">{d.name}</span> },
            { key: "size", header: "Size", numeric: true, cell: (d) => count(d.size) },
            { key: "kind", header: "Kind", cell: (d) => d.kind ?? "—" },
            { key: "role", header: "Role in the data", cell: (d) => <Badge variant="outline">{d.role_in_data}</Badge> },
            { key: "range", header: "From – to", cell: (d) => (d.start != null ? `${str(d.start)} – ${str(d.end)}` : "—") },
            { key: "even", header: "Even steps", cell: (d) => (d.evenly_spaced == null ? "—" : d.evenly_spaced ? "yes" : "no") },
          ]}
        />
        <StatTable
          label="Variables"
          rows={view.vars ?? []}
          rowKey={(v) => v.name}
          columns={[
            { key: "name", header: "Variable", cell: (v) => <span className="font-mono text-xs">{v.name}</span> },
            { key: "used", header: "Used as", cell: (v) => <Badge variant={v.used_as === "data" ? "outline" : "secondary"}>{v.used_as}</Badge> },
            { key: "dims", header: "Dims", cell: (v) => <span className="font-mono text-xs text-muted-foreground">{(v.dims ?? []).join(" × ")}</span> },
            { key: "shape", header: "Shape", cell: (v) => <span className="tabular">{(v.shape ?? []).join(" × ")}</span> },
            { key: "dtype", header: "Type", cell: (v) => v.dtype },
            { key: "role", header: "Role", cell: (v) => v.role ?? "—" },
            { key: "units", header: "Units", cell: (v) => v.units ?? "—" },
          ]}
        />
        {notApplicable.length ? (
          <StatTable
            label="Views that do not apply"
            rows={notApplicable}
            rowKey={([s]) => s}
            columns={[
              { key: "s", header: "View", cell: ([s]) => DATA_SECTION_META[s as DataSection]?.title ?? s },
              { key: "why", header: "Why", cell: ([, why]) => <span className="text-muted-foreground">{why}</span> },
            ]}
          />
        ) : null}
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ images

const imageLabel = (e: ImageEntry) => seriesLabel(e.var, e.facet);

/** The group means of an image (e.g. one mean picture per digit class) as frames of a field. */
export function groupField(e: ImageEntry): FieldEntry | null {
  if (!e.groups?.length) return null;
  return { ...e, frames: e.groups.map((g) => ({ label: g.label, mean: g.mean, rgb: g.rgb ?? null })), frame_dim: e.group_by ?? "group", omitted_frames: 0, kind: "scalar" };
}

export function ImagesSection({ view, expert }: P<ImagesView>) {
  const fields = useMemo(() => view.fields ?? [], [view.fields]);
  const labels = useMemo(() => fields.map(imageLabel), [fields]);
  const [pick, setPick] = useState(0);
  const [show, setShow] = useState<"frames" | "groups">("frames");
  const e = fields[pick] ?? fields[0];
  const groups = useMemo(() => (e ? groupField(e) : null), [e]);
  const stats = useMemo(() => (e?.frame_stats ? frameStatsPlot(e.frame_stats, { frameDim: e.frame_dim ?? "frame", title: e.units ? `${e.var} (${e.units})` : e.var }) : null), [e]);
  const hist = useMemo(() => (e?.histogram ? countsHistogramPlot(e.histogram, { title: e.units ? `${e.var} (${e.units})` : e.var, logCount: Boolean(e.log_counts) }) : null), [e]);
  if (!e) return <p className="text-muted-foreground">The data has no images.</p>;
  const reading = imageReading(e);
  const shown = show === "groups" && groups ? groups : e;
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        {fields.length > 1 ? <Picker label="Image" value={String(pick)} options={labels.map((l, i) => ({ value: String(i), label: l }))} onChange={(v) => setPick(Number(v))} /> : null}
        {groups ? (
          <Toggle
            label="Show"
            value={show}
            options={[
              { value: "frames", label: `Each ${e.frame_dim ?? "frame"}` },
              { value: "groups", label: `Mean of each ${e.group_by ?? "group"}` },
            ]}
            onChange={setShow}
          />
        ) : null}
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <FieldViewer key={`${pick}-${show}`} field={shown} mode="data" mask={e.mask} maskLabel={e.mask_var} frameStats={show === "frames" ? e.frame_stats : null} />
      {stats ? <SciPlotView plot={stats} title={`${e.var} in each ${e.frame_dim ?? "frame"}`} caption={`The mean of all cells in each ${e.frame_dim ?? "frame"} (line) and the range from the minimum to the maximum (band).`} height={220} /> : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        {hist ? <SciPlotView plot={hist} title={`Values of all the cells of ${e.var}`} height={230} /> : null}
        {(e.components ?? []).map((c, i) =>
          c.histogram ? <SciPlotView key={`${str(c.label)}-${i}`} plot={countsHistogramPlot(c.histogram, { title: `${e.var}, component ${str(c.label)}` })} title={`Component ${str(c.label)} of ${e.var}`} height={200} /> : null,
        )}
        <StatTable
          label="Image statistics"
          rows={fields}
          rowKey={(f, i) => `${imageLabel(f)}-${i}`}
          columns={[
            { key: "var", header: "Image", cell: (f) => <span className={cn("font-mono text-xs", f === e && "font-semibold")}>{imageLabel(f)}</span> },
            { key: "size", header: "Cells", cell: (f) => `${f.source_height} x ${f.source_width}` },
            { key: "frames", header: "Frames", numeric: true, cell: (f) => count(f.frame_stats?.coords.length ?? f.frames.length + (f.omitted_frames ?? 0)) },
            { key: "min", header: "Min", numeric: true, cell: (f) => num(f.stats?.min) },
            { key: "mean", header: "Mean", numeric: true, cell: (f) => num(f.stats?.mean) },
            { key: "max", header: "Max", numeric: true, cell: (f) => num(f.stats?.max) },
            { key: "sd", header: "Std. dev.", numeric: true, cell: (f) => num(f.stats?.sd) },
            { key: "missing", header: "Missing", numeric: true, cell: (f) => pct(f.stats?.missing_share, 1) },
            { key: "sat", header: "At max", numeric: true, cell: (f) => pct(f.stats?.saturated_share, 1) },
          ]}
        />
        {view.omitted?.length ? <p className="text-xs text-muted-foreground">Not shown (too many images): {view.omitted.join(", ")}.</p> : null}
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ profiles

export function ProfilesSection({ view, expert }: P<ProfilesView>) {
  const items = useMemo(() => view.items ?? [], [view.items]);
  const [pick, setPick] = useState(0);
  const [frame, setFrame] = useState<number | null>(null);
  const e = items[pick] ?? items[0];
  const plot = useMemo(() => (e ? profilesPlot(e, frame !== null && frame < e.frames.length ? frame : null) : null), [e, frame]);
  if (!e) return <p className="text-muted-foreground">The data has no profiles.</p>;
  const reading = profileReading(e);
  const frameDim = e.frame_dim ?? "frame";
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        {items.length > 1 ? (
          <Picker
            label="Profile"
            value={String(pick)}
            options={items.map((it, i) => ({ value: String(i), label: seriesLabel(it.var, it.facet) }))}
            onChange={(v) => {
              setPick(Number(v));
              setFrame(null);
            }}
          />
        ) : null}
        {e.frames.length > 1 ? (
          <Picker label={`Curve (${frameDim})`} value={frame === null ? "all" : String(frame)} options={[{ value: "all", label: `All ${e.frames.length}` }, ...e.frames.map((f, i) => ({ value: String(i), label: str(f.label) }))]} onChange={(v) => setFrame(v === "all" ? null : Number(v))} />
        ) : null}
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <SciPlotView plot={plot} title={`${e.var} along ${e.along}`} height={300} caption={e.downsampled ? "The curves are thinned to fit the plot." : undefined} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Highest value of each curve"
          rows={e.peak ?? []}
          rowKey={(p, i) => `${str(p.frame)}-${i}`}
          empty="The view has no peaks."
          columns={[
            { key: "frame", header: frameDim, cell: (p) => str(p.frame) },
            { key: "at", header: `At ${e.along}`, cell: (p) => str(p.at) },
            { key: "value", header: "Value", numeric: true, cell: (p) => num(p.value) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ map

export function MapSection({ view, expert }: P<MapView>) {
  const layers = useMemo(() => view.layers ?? [], [view.layers]);
  // a ratio (target / exposure, E18 cases / expected) first: the raw counts mostly show the population
  const [layer, setLayer] = useState(() => Math.max(0, layers.findIndex((x) => (x.derived_from?.length ?? 0) > 0)));
  const [frame, setFrame] = useState(0);
  const [edges, setEdges] = useState(false);
  const l = layers[layer] ?? layers[0];
  const frames = l?.frames ?? null;
  const fi = frames?.length ? Math.min(frame, frames.length - 1) : null;
  const plot = useMemo(() => mapPlot(view, layers.indexOf(l!), fi, { edges }), [view, layers, l, fi, edges]);
  if (!l) return <p className="text-muted-foreground">The map has no values to show.</p>;
  const reading = mapReading(view, l.var);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        <Picker
          label="Colour the areas by"
          value={String(layers.indexOf(l))}
          options={layers.map((x, i) => ({ value: String(i), label: x.derived_from?.length ? `${x.var} (derived)` : x.var }))}
          onChange={(v) => {
            setLayer(Number(v));
            setFrame(0);
          }}
        />
        {view.edges?.length ? (
          <label className="flex items-center gap-2 pb-1 text-[13px]">
            <input type="checkbox" className="accent-primary" checked={edges} onChange={(e) => setEdges(e.target.checked)} />
            Show the neighbours
          </label>
        ) : null}
      </div>
      {frames && frames.length > 1 && fi !== null ? (
        <div className="grid gap-1">
          <label htmlFor="map-frame" className="flex justify-between text-xs font-medium text-muted-foreground">
            <span>Frame</span>
            <span className="tabular text-foreground">{str(frames[fi]?.label)}</span>
          </label>
          <input id="map-frame" type="range" min={0} max={frames.length - 1} value={fi} onChange={(e) => setFrame(Number(e.target.value))} className="w-full accent-primary" />
        </div>
      ) : null}
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <SciPlotView plot={plot} title={`Map of ${l.var}`} height={520} caption="The colour classes have about the same number of areas each. Point to an area to see its value." />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Spatial autocorrelation (Moran's I)"
          rows={view.moran ?? []}
          rowKey={(m) => m.var}
          empty="The view has no Moran's I."
          columns={[
            { key: "var", header: "Variable", cell: (m) => <span className="font-mono text-xs">{m.var}</span> },
            { key: "i", header: "Moran's I", numeric: true, cell: (m) => num(m.morans_i, 3) },
            { key: "e", header: "Expected", numeric: true, cell: (m) => num(m.expected, 3) },
            { key: "z", header: "z", numeric: true, cell: (m) => num(m.z, 3) },
            { key: "p", header: "p-value", numeric: true, cell: (m) => pValue(m.p_value) },
            { key: "w", header: "Neighbours", cell: (m) => (m.weights === "knn4" ? "4 nearest" : "adjacency") },
          ]}
        />
        <StatTable
          label="Areas"
          rows={view.areas.map((a, i) => ({ a, i }))}
          rowKey={(r) => String(r.i)}
          maxHeight="20rem"
          columns={[
            { key: "a", header: view.area_dim, cell: (r) => str(r.a) },
            ...layers.map((x, k) => ({ key: `l${k}`, header: x.var, numeric: true, cell: (r: { i: number }) => num(x.values[r.i]) })),
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ network

export function NetworkSection({ view, expert }: P<NetworkView>) {
  const items = useMemo(() => view.items ?? [], [view.items]);
  const [pick, setPick] = useState(0);
  const e = items[pick] ?? items[0];
  const attrs = Object.keys(e?.node_attrs ?? {});
  const [attr, setAttr] = useState<string>("");
  const [show, setShow] = useState<"links" | "matrix">("links");
  const useAttr = attr && attrs.includes(attr) ? attr : null;
  const links = useMemo(() => (e ? nodeLinkPlot(e, useAttr) : null), [e, useAttr]);
  const matrix = useMemo(() => (e ? networkMatrixPlot(e) : null), [e]);
  // a network without a matrix (too many nodes) always draws the nodes and ties
  const mode = matrix ? show : "links";
  const degree = useMemo(() => (e?.degree_hist ? countsHistogramPlot(e.degree_hist, { title: e.weighted ? "Strength" : "Degree (ties per node)" }) : null), [e]);
  if (!e) return <p className="text-muted-foreground">The data has no networks.</p>;
  const reading = networkReading(e);
  return (
    <>
      <div className="flex flex-wrap items-end gap-3">
        {items.length > 1 ? <Picker label="Network" value={String(pick)} options={items.map((it, i) => ({ value: String(i), label: seriesLabel(it.var, it.facet) }))} onChange={(v) => setPick(Number(v))} /> : null}
        <Toggle
          label="Draw as"
          value={mode}
          options={[{ value: "links", label: "Nodes and ties" }, ...(matrix ? [{ value: "matrix" as const, label: "Matrix" }] : [])]}
          onChange={setShow}
        />
        {attrs.length && mode === "links" ? <Picker label="Colour by" value={attr} options={[{ value: "", label: "Nothing" }, ...attrs.map((a) => ({ value: a, label: a }))]} onChange={setAttr} /> : null}
      </div>
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      {mode === "matrix" && matrix ? (
        <SciPlotView plot={matrix} title={`Ties of ${e.var} (matrix)`} height={460} caption="The rows and columns are in an order that puts connected nodes together (seriation)." />
      ) : (
        <SciPlotView plot={links} title={`Ties of ${e.var}`} height={460} caption="The size of a node gives its number of ties." />
      )}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        {degree ? <SciPlotView plot={degree} title={`Degree distribution of ${e.var}`} height={220} /> : null}
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" aria-label="Network statistics">
          <dt className="text-muted-foreground">Nodes</dt>
          <dd className="tabular">{count(e.n_nodes)}</dd>
          <dt className="text-muted-foreground">Ties</dt>
          <dd className="tabular">{count((e.edges ?? []).length + (e.omitted_edges ?? 0))}</dd>
          <dt className="text-muted-foreground">Kind</dt>
          <dd>{[e.binary ? "binary" : "weighted", e.signed ? "signed" : null, e.directed ? "directed" : "undirected"].filter(Boolean).join(", ")}</dd>
          <dt className="text-muted-foreground">Density</dt>
          <dd className="tabular">{num(e.density, 3)}</dd>
          <dt className="text-muted-foreground">Transitivity</dt>
          <dd className="tabular">{num(e.transitivity, 3)}</dd>
          {e.directed ? (
            <>
              <dt className="text-muted-foreground">Reciprocity</dt>
              <dd className="tabular">{num(e.reciprocity, 3)}</dd>
            </>
          ) : null}
          <dt className="text-muted-foreground">Separate parts</dt>
          <dd className="tabular">{e.components ? `${e.components.count} (largest: ${pct(e.components.largest_share)})` : "—"}</dd>
        </dl>
        <StatTable
          label="Homophily"
          rows={e.homophily ?? []}
          rowKey={(h) => h.attr}
          empty="The nodes have no attributes."
          columns={[
            { key: "a", header: "Attribute", cell: (h) => <span className="font-mono text-xs">{h.attr}</span> },
            { key: "w", header: "Ties in the same group", numeric: true, cell: (h) => pct(h.within_share) },
            { key: "e", header: "By chance", numeric: true, cell: (h) => pct(h.expected_share) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ tree

export function TreeSection({ view, expert }: P<TreeView>) {
  const traitVars = useMemo(() => (view.traits ?? []).map((t) => t.var), [view.traits]);
  const [shown, setShown] = useState<string[]>(() => traitVars.slice(0, 2));
  const plot = useMemo(() => dendrogramPlot(view, shown), [view, shown]);
  const reading = treeReading(view);
  return (
    <>
      {traitVars.length ? (
        <fieldset className="flex flex-wrap items-center gap-3 text-[13px]">
          <legend className="sr-only">Traits next to the tree</legend>
          <span className="text-xs font-medium text-muted-foreground">Traits next to the tree (a maximum of 4)</span>
          {traitVars.map((t) => (
            <label key={t} className="flex items-center gap-1.5">
              <input
                type="checkbox"
                className="accent-primary"
                checked={shown.includes(t)}
                disabled={!shown.includes(t) && shown.length >= 4}
                onChange={(e) => setShown((s) => (e.target.checked ? [...s, t] : s.filter((x) => x !== t)))}
              />
              <span className="font-mono text-xs">{t}</span>
            </label>
          ))}
        </fieldset>
      ) : null}
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <SciPlotView plot={plot} title={`Tree of ${view.node_dim}`} height={480} caption={view.matched_by && view.matched_by !== "label" ? `The trait values are matched to the tips by ${view.matched_by}, not by name.` : undefined} />
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" aria-label="Tree statistics">
          <dt className="text-muted-foreground">Nodes</dt>
          <dd className="tabular">{count(view.n_nodes)}</dd>
          <dt className="text-muted-foreground">Tips</dt>
          <dd className="tabular">{count(view.n_tips)}</dd>
          <dt className="text-muted-foreground">Roots</dt>
          <dd className="tabular">{num(view.roots?.length)}</dd>
          <dt className="text-muted-foreground">Depth</dt>
          <dd className="tabular">{num(view.depth)}</dd>
          <dt className="text-muted-foreground">Ultrametric</dt>
          <dd>{view.ultrametric == null ? "—" : view.ultrametric ? "yes" : "no"}</dd>
        </dl>
        <StatTable
          label="Phylogenetic signal (Moran's I)"
          rows={view.signal ?? []}
          rowKey={(s) => s.var}
          empty="The tree has no traits."
          columns={[
            { key: "v", header: "Trait", cell: (s) => <span className="font-mono text-xs">{s.var}</span> },
            { key: "i", header: "Moran's I", numeric: true, cell: (s) => num(s.morans_i, 3) },
            { key: "p", header: "p-value", numeric: true, cell: (s) => pValue(s.p_value) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ circular

export function CircularSection({ view, expert }: P<CircularView>) {
  const items = useMemo(() => view.items ?? [], [view.items]);
  const [pick, setPick] = useState(0);
  const e = items[pick] ?? items[0];
  const rose = useMemo(() => (e ? rosePlot(e) : null), [e]);
  const pairPlots = useMemo(
    () => (view.pairs ?? []).map((p) => ({ p, plot: circularPairPlot(p, items.find((i) => i.var === p.a), items.find((i) => i.var === p.b)) })),
    [view.pairs, items],
  );
  if (!e) return <p className="text-muted-foreground">The data has no directions or times of day.</p>;
  const reading = circularReading(e);
  return (
    <>
      {items.length > 1 ? (
        <div className="flex flex-wrap items-end gap-3">
          <Picker label="Variable" value={String(pick)} options={items.map((it, i) => ({ value: String(i), label: it.units ? `${it.var} (${it.units})` : it.var }))} onChange={(v) => setPick(Number(v))} />
        </div>
      ) : null}
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <SciPlotView plot={rose} title={`Rose of ${e.var}`} height={Math.max(340, Math.ceil(Math.min(e.groups.length, 12) / 3) * 300)} caption="Each bar counts the values in one sector of the circle. The red line points to the mean direction; its length is the concentration R." />
      {pairPlots.map(({ p, plot }) => (
        <SciPlotView key={`${p.a}-${p.b}`} plot={plot} title={`${p.a} against ${p.b}`} height={320} caption={p.rho != null ? `Circular correlation: ${num(p.rho, 2)}.` : undefined} />
      ))}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <StatTable
          label="Circular statistics"
          rows={e.groups}
          rowKey={(g, i) => `${str(g.label)}-${i}`}
          columns={[
            { key: "g", header: e.group_by ?? "Group", cell: (g) => str(g.label ?? "all") },
            { key: "n", header: "n", numeric: true, cell: (g) => count(g.n) },
            { key: "mean", header: "Mean direction", numeric: true, cell: (g) => circularMean(e, g) ?? "—" },
            { key: "r", header: "R", numeric: true, cell: (g) => num(g.resultant_length, 3) },
            { key: "sd", header: "Circular sd", numeric: true, cell: (g) => num(g.circular_sd, 3) },
            { key: "z", header: "Rayleigh z", numeric: true, cell: (g) => num(g.rayleigh_z, 3) },
            { key: "p", header: "p-value", numeric: true, cell: (g) => pValue(g.rayleigh_p) },
          ]}
        />
      </Statistics>
    </>
  );
}

// ------------------------------------------------------------------------------ points

function colourKey(c: PointsColour): string {
  return c.kind === "var" ? `var:${c.var}` : c.kind;
}

function colourLabel(c: PointsColour, view: PointsView): string {
  switch (c.kind) {
    case "srgb":
      return "Colour of each point";
    case "mode":
      return `Most frequent ${view.answers?.var ?? "answer"}`;
    case "agreement":
      return "Agreement of the answers";
    case "var":
      return c.var;
    default:
      return "No colour";
  }
}

/** For each term, the number of sampled points where it is the most frequent answer. */
export function modeCounts(view: PointsView): { t: string; n: number }[] {
  const a = view.answers;
  if (!a) return [];
  const n = new Map(a.terms.map((t) => [t, 0]));
  for (const m of a.mode) if (m !== null && n.has(m)) n.set(m, (n.get(m) ?? 0) + 1);
  return a.terms.map((t) => ({ t, n: n.get(t) ?? 0 })).sort((x, y) => y.n - x.n);
}

export function PointsSection({ view, expert }: P<PointsView>) {
  const colours = useMemo(() => pointsColours(view), [view]);
  const [key, setKey] = useState(colourKey(colours[0]!));
  const colour = colours.find((c) => colourKey(c) === key) ?? colours[0]!;
  const plot = useMemo(() => pointsPlot(view, colour), [view, colour]);
  const density = useMemo(() => pointsDensityPlot(view), [view]);
  const reading = pointsReading(view);
  return (
    <>
      {colours.length > 1 ? (
        <div className="flex flex-wrap items-end gap-3">
          <Picker label="Colour" value={key} options={colours.map((c) => ({ value: colourKey(c), label: colourLabel(c, view) }))} onChange={setKey} />
        </div>
      ) : null}
      <PlainSummary tone={reading.tone}>{reading.text}</PlainSummary>
      <SciPlotView plot={plot} title={`${view.point_dim}: ${colourLabel(colour, view)}`} height={view.lattice ? 300 : 400} />
      {density ? <SciPlotView plot={density} title={`Density of all ${count(view.n)} points`} height={340} /> : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
      <Statistics expert={expert}>
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]" aria-label="Point statistics">
          <dt className="text-muted-foreground">Points</dt>
          <dd className="tabular">{count(view.n)}</dd>
          <dt className="text-muted-foreground">Position</dt>
          <dd className="font-mono text-xs">
            {view.x}, {view.y}
            {view.lattice ? " (grid)" : ""}
          </dd>
          {view.clark_evans ? (
            <>
              <dt className="text-muted-foreground">Clark-Evans R</dt>
              <dd className="tabular">
                {num(view.clark_evans.r, 3)} (p {pValue(view.clark_evans.p_value)})
              </dd>
            </>
          ) : null}
        </dl>
        {view.answers?.terms.length ? (
          <StatTable
            label="Answers"
            rows={modeCounts(view)}
            rowKey={(r) => r.t}
            maxHeight="18rem"
            columns={[
              { key: "t", header: view.answers.var, cell: (r) => r.t },
              { key: "n", header: "Points where it is the most frequent answer", numeric: true, cell: (r) => count(r.n) },
            ]}
          />
        ) : null}
      </Statistics>
    </>
  );
}
