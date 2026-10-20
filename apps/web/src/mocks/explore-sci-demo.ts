/**
 * Mock API of two demo datasets for the Data Explorer's sections for scientific data, built
 * from the example views the spec freezes (`packages/spec/tests/data/explorer_science/*.json`,
 * valid against `mapi_spec.views`). They show every new section and the adaptive section
 * list from the contract alone, before (and next to) the runner-computed science fixtures of
 * `explore-sci.ts`:
 *
 * - `ds-sci-demo`: one plain dataset whose ingest index has every picture section (`order`
 *   by relevance), and the time sections as `not_applicable`;
 * - `ds-sci-demo-tree`: a tree dataset with two nodes, `/image` (an image) and `/links` (a
 *   network), each with its own index, shapes and hidden sections.
 *
 * The handlers come before the generic dataset routes and answer only for these datasets.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { DataSchema, Dataset, DatasetVersion } from "@/api/types";
import { env } from "@/env";

import circular from "../../../../packages/spec/tests/data/explorer_science/circular.json";
import images from "../../../../packages/spec/tests/data/explorer_science/images.json";
import map from "../../../../packages/spec/tests/data/explorer_science/map.json";
import network from "../../../../packages/spec/tests/data/explorer_science/network.json";
import points from "../../../../packages/spec/tests/data/explorer_science/points.json";
import profiles from "../../../../packages/spec/tests/data/explorer_science/profiles.json";
import structure from "../../../../packages/spec/tests/data/explorer_science/structure.json";
import tree from "../../../../packages/spec/tests/data/explorer_science/tree.json";
import * as fx from "./fixtures";
import gen from "./explorer.gen.json";

const API = env.apiBase;
const enc = new TextEncoder();

export const SCI_DEMO_ID = "ds-sci-demo";
export const SCI_DEMO_TREE_ID = "ds-sci-demo-tree";

type Json = Record<string, unknown>;

/** The time sections: they do not apply to the demo data (no time dim). */
export const SCI_DEMO_NOT_APPLICABLE = ["timeseries", "ccf", "acf_pacf", "stationarity", "stl", "groups", "vif"] as const;
const SCIENCE: Record<string, Json> = { structure, images, profiles, map, network, tree, circular, points };
const ALL = ["overview", "distributions", "timeseries", "missingness", "outliers", "correlation", "ccf", "acf_pacf", "stationarity", "stl", "vif", "groups", "quality", ...Object.keys(SCIENCE)];
const ingestViews = (gen as unknown as { dataset: { ingest: { views: Record<string, Json> } } }).dataset.ingest.views;

const reasonOf = (s: string) =>
  (SCI_DEMO_NOT_APPLICABLE as readonly string[]).includes(s) ? "Not available: the data has no time dimension." : `Not available: this node has no ${s === "images" ? "image" : s} data.`;

/** An ingest index and its views under `prefix` (`ui/explore/` or a node's folder). */
function part(prefix: string, ref: string, ok: Record<string, Json>, order: string[], shapes: string[]): Map<string, Json> {
  const out = new Map<string, Json>();
  const sections = ALL.map((s) =>
    ok[s] ? { section: s, view: `${prefix}${s}.json` } : { section: s, view: `${prefix}${s}.json`, status: "skipped", reason: "not_applicable", message_plain: reasonOf(s) },
  );
  out.set(`${prefix}index.json`, { format: "mapi.explorer-index/v1", target: "dataset", ref, shapes, order, sections });
  for (const [s, v] of Object.entries(ok)) out.set(`${prefix}${s}.json`, v);
  return out;
}

const overview = (shapes: unknown[], summary: string): Json => ({ ...ingestViews.overview, shapes, summary_plain: summary });
const shapesOf = (kinds: string[]) => (structure.shapes as { kind: string }[]).filter((s) => kinds.includes(s.kind));

function demoArtifacts(): Map<string, Json> {
  const ok: Record<string, Json> = {
    overview: overview(structure.shapes, "Demo data with an image, a map, a network, a tree, directions, points and profiles."),
    quality: ingestViews.quality!,
    distributions: ingestViews.distributions!,
    missingness: ingestViews.missingness!,
    outliers: ingestViews.outliers!,
    correlation: ingestViews.correlation!,
    ...SCIENCE,
  };
  const order = ["overview", "structure", "images", "map", "network", "tree", "circular", "points", "profiles", "quality", "distributions", "correlation", "missingness", "outliers"];
  return part("ui/explore/", "dataset://default/sci_demo@v1", ok, order, ["image", "areal_map", "network", "tree", "circular", "points", "profile"]);
}

const IMAGE_NODE = "/image";
const LINKS_NODE = "/links";

function treeArtifacts(): Map<string, Json> {
  const out = new Map<string, Json>();
  const nodes = { default: IMAGE_NODE, nodes: { [IMAGE_NODE]: { index: "ui/explore/nodes/image/index.json", slug: "image" }, [LINKS_NODE]: { index: "ui/explore/nodes/links/index.json", slug: "links" } } };
  out.set("ui/explore/nodes/index.json", nodes);
  const img = part(
    "ui/explore/nodes/image/",
    "dataset://default/sci_demo_tree@v1",
    { overview: overview(shapesOf(["image"]), "The image node: one image movie."), structure: { ...structure, shapes: shapesOf(["image"]) }, images, quality: ingestViews.quality! },
    ["overview", "structure", "images", "quality"],
    ["image"],
  );
  const links = part(
    "ui/explore/nodes/links/",
    "dataset://default/sci_demo_tree@v1",
    { overview: overview([{ kind: "network", vars: ["cowork"], dims: { node: "lawyer" }, score: 0.9, label_plain: "network: 4 lawyer", sections: ["network"] }], "The links node: one network."), network, quality: ingestViews.quality! },
    ["overview", "network", "quality"],
    ["network"],
  );
  for (const [k, v] of [...img, ...links]) out.set(k, v);
  // the default node's views are also at ui/explore/ (as a tree ingest writes them)
  for (const [k, v] of img) out.set(k.replace("ui/explore/nodes/image/", "ui/explore/"), v);
  return out;
}

const grid: DataSchema = {
  layout: "dense",
  dims: { quarter: { kind: "time", policy: "extend", freq: "QS" }, row: { kind: "integer", policy: "fixed" }, col: { kind: "integer", policy: "fixed" } },
  vars: { quakes: { dims: ["quarter", "row", "col"], dtype: "int64", role: "target", units: "events" } },
};
const links: DataSchema = {
  layout: "dense",
  dims: { lawyer: { kind: "integer", policy: "fixed" }, lawyer_: { kind: "integer", policy: "fixed" } },
  vars: { cowork: { dims: ["lawyer", "lawyer_"], dtype: "int64", role: "covariate" } },
};

function dataset(id: string, name: string): Dataset {
  return { ...fx.dataset, id, name, ref: `dataset://default/${name}`, description: "Demo of the Data Explorer for scientific data (the spec's example views)." };
}

function version(id: string, name: string, treeLayout: boolean): DatasetVersion {
  const v: DatasetVersion = { ...fx.datasetVersion, dataset_id: id, ref: `dataset://default/${name}@v1`, data_schema: grid };
  return treeLayout ? ({ ...v, layout: "tree", tree_schema: { nodes: { "/": { layout: "dense", dims: {}, vars: {} }, [IMAGE_NODE]: grid, [LINKS_NODE]: links } } }) : v;
}

export function createSciDemoHandlers(shared: { artifacts: Map<string, { bytes: Uint8Array; type: string }> }): HttpHandler[] {
  const demos = [
    { id: SCI_DEMO_ID, name: "sci_demo", artifacts: demoArtifacts(), tree: false },
    { id: SCI_DEMO_TREE_ID, name: "sci_demo_tree", artifacts: treeArtifacts(), tree: true },
  ];
  const key = (id: string, name: string) => `${id}:${name}`;
  for (const d of demos) for (const [name, value] of d.artifacts) shared.artifacts.set(key(d.id, name), { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
  return demos.flatMap((d) => [
    http.get(`${API}/v1/datasets/${d.id}`, () => HttpResponse.json(dataset(d.id, d.name))),
    http.get(`${API}/v1/datasets/${d.id}/versions`, () => HttpResponse.json({ items: [version(d.id, d.name, d.tree)], next_cursor: null })),
    http.get(`${API}/v1/datasets/${d.id}/versions/:v`, ({ params }) =>
      params.v === "1" ? HttpResponse.json(version(d.id, d.name, d.tree)) : HttpResponse.json({ type: "urn:mapi:error:NOT_FOUND", title: "Not found", status: 404, code: "NOT_FOUND", errors: [] }, { status: 404 }),
    ),
    http.get(`${API}/v1/datasets/${d.id}/about`, () => HttpResponse.json({ description_user: null, highlights: [], summary_generated: "Demo data for the Data Explorer on scientific data.", warnings_plain: [], generated_at: fx.T0 })),
    http.get(new RegExp(`/v1/datasets/${d.id}/versions/1/artifacts/(.+)$`), ({ request }) => {
      const name = decodeURIComponent(new URL(request.url).pathname.split("/artifacts/")[1] ?? "");
      const a = shared.artifacts.get(key(d.id, name));
      return a
        ? HttpResponse.json({ ...fx.artifactLink(key(d.id, name), a.bytes, a.type), name })
        : HttpResponse.json({ type: "urn:mapi:error:NOT_FOUND", title: "Not found", status: 404, code: "NOT_FOUND", detail: `no artifact ${name}`, errors: [] }, { status: 404 });
    }),
  ]);
}
