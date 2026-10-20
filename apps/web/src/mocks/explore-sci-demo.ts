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
 *   network), each with its own index, shapes and hidden sections;
 * - `ds-sci-demo-images` (v0.14): an image folder of 60 small colour pictures of three shapes
 *   (`pixels(image, y, x, channel)`, a `label` variable and a `split` coord), with the `images`
 *   (RGB) and `gallery` views built here (the PNG thumbnails too) until `explorer-sci.gen.json`
 *   has an ingested image folder.
 *
 * The handlers come before the generic dataset routes and answer only for these datasets.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { DataSchema, Dataset, DatasetVersion } from "@/api/types";
import { env } from "@/env";
import { encodeRaster, encodeRgb } from "@/features/explore/fields";

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
export const SCI_DEMO_IMAGES_ID = "ds-sci-demo-images";

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

// ------------------------------------------------------------------ v0.14: an image folder

const IMG = 16;
const N_IMAGES = 60;
const CLASSES = ["circle", "square", "cross"] as const;
const COLOURS: Record<(typeof CLASSES)[number], readonly [number, number, number]> = { circle: [220, 70, 60], square: [60, 170, 90], cross: [70, 100, 220] };

const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes: Uint8Array): number {
  let c = 0xffffffff;
  for (const b of bytes) c = CRC_TABLE[(c ^ b) & 255]! ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

/** A zlib stream of stored (uncompressed) deflate blocks: enough for the tiny mock thumbnails. */
function zlibStored(raw: Uint8Array): Uint8Array {
  const blocks = Math.max(1, Math.ceil(raw.length / 65535));
  const out = new Uint8Array(2 + raw.length + 5 * blocks + 4);
  out.set([0x78, 0x01]);
  let o = 2;
  for (let i = 0; i < blocks; i++) {
    const part = raw.subarray(i * 65535, (i + 1) * 65535);
    out[o++] = i === blocks - 1 ? 1 : 0;
    out[o++] = part.length & 255;
    out[o++] = part.length >> 8;
    out[o++] = ~part.length & 255;
    out[o++] = (~part.length >> 8) & 255;
    out.set(part, o);
    o += part.length;
  }
  let a = 1;
  let b = 0;
  for (const x of raw) {
    a = (a + x) % 65521;
    b = (b + a) % 65521;
  }
  new DataView(out.buffer).setUint32(o, ((b << 16) | a) >>> 0);
  return out;
}

function pngChunk(type: string, data: Uint8Array): Uint8Array {
  const out = new Uint8Array(12 + data.length);
  const view = new DataView(out.buffer);
  view.setUint32(0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
  out.set(data, 8);
  view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
  return out;
}

/** A base64 PNG of `height` x `width` 8-bit RGB pixels (row-major r, g, b). */
export function pngBase64(width: number, height: number, rgb: Uint8Array): string {
  const ihdr = new Uint8Array(13);
  const view = new DataView(ihdr.buffer);
  view.setUint32(0, width);
  view.setUint32(4, height);
  ihdr.set([8, 2, 0, 0, 0], 8);
  const raw = new Uint8Array(height * (1 + 3 * width));
  for (let y = 0; y < height; y++) raw.set(rgb.subarray(3 * width * y, 3 * width * (y + 1)), y * (1 + 3 * width) + 1);
  const parts = [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), pngChunk("IHDR", ihdr), pngChunk("IDAT", zlibStored(raw)), pngChunk("IEND", new Uint8Array(0))];
  const bytes = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) {
    bytes.set(p, o);
    o += p.length;
  }
  let bin = "";
  bytes.forEach((x) => (bin += String.fromCharCode(x)));
  return btoa(bin);
}

/** One 16 x 16 picture: a coloured shape (its class) on a dark, noisy background. */
function picture(i: number): Uint8Array {
  const cls = CLASSES[i % 3]!;
  const [r, g, b] = COLOURS[cls];
  let seed = 1013 * (i + 1);
  const noise = () => {
    seed = (seed * 1103515245 + 12345) & 0x7fffffff;
    return (seed % 31) - 15;
  };
  const cx = 7.5 + ((i % 5) - 2) * 0.5;
  const cy = 7.5 + ((i % 7) - 3) * 0.4;
  const out = new Uint8Array(IMG * IMG * 3);
  for (let y = 0; y < IMG; y++) {
    for (let x = 0; x < IMG; x++) {
      const dx = x - cx;
      const dy = y - cy;
      const inside = cls === "circle" ? dx * dx + dy * dy < 25 : cls === "square" ? Math.abs(dx) < 4.5 && Math.abs(dy) < 4.5 : (Math.abs(dx) < 1.6 && Math.abs(dy) < 6) || (Math.abs(dy) < 1.6 && Math.abs(dx) < 6);
      const o = 3 * (y * IMG + x);
      const base = inside ? [r, g, b] : [24, 24, 32];
      for (let c = 0; c < 3; c++) out[o + c] = Math.max(0, Math.min(255, base[c]! + noise()));
    }
  }
  return out;
}

const luma = (rgb: Uint8Array) => Array.from({ length: rgb.length / 3 }, (_, k) => 0.299 * rgb[3 * k]! + 0.587 * rgb[3 * k + 1]! + 0.114 * rgb[3 * k + 2]!);
const triplets = (rgb: Uint8Array | Float64Array) => Array.from({ length: rgb.length / 3 }, (_, k) => [Math.round(rgb[3 * k]!), Math.round(rgb[3 * k + 1]!), Math.round(rgb[3 * k + 2]!)] as const);
const imageId = (i: number) => String(i).padStart(2, "0");
const splitOf = (i: number) => (i % 4 === 0 ? "test" : "train");

const IMAGE_SCHEMA: DataSchema = {
  layout: "dense",
  dims: { image: { kind: "categorical", policy: "new_levels", new_level_strategy: "pooled" }, y: { kind: "integer", policy: "fixed" }, x: { kind: "integer", policy: "fixed" }, channel: { kind: "categorical", policy: "fixed" } },
  vars: {
    pixels: { dims: ["image", "y", "x", "channel"], dtype: "uint8", role: "covariate" },
    label: { dims: ["image"], dtype: "str", role: "target", missing: "allow" },
  },
};

export const SCI_DEMO_IMAGES_DATASET: Dataset = {
  ...fx.dataset,
  id: SCI_DEMO_IMAGES_ID,
  name: "sci_demo_images",
  ref: "dataset://default/sci_demo_images",
  description: "Demo of an image folder: 60 small colour pictures of circles, squares and crosses.",
};

/** The `images` (RGB) and `gallery` views of the image folder, built like the runner builds them. */
export function imageFolderViews(): { images: Json; gallery: Json } {
  const pics = Array.from({ length: N_IMAGES }, (_, i) => picture(i));
  const lumas = pics.map(luma);
  const shown = Array.from({ length: 12 }, (_, k) => Math.round((k * (N_IMAGES - 1)) / 11));
  const all = lumas.flat();
  const mean = all.reduce((a, v) => a + v, 0) / all.length;
  const sd = Math.sqrt(all.reduce((a, v) => a + (v - mean) ** 2, 0) / (all.length - 1));
  const edges = Array.from({ length: 11 }, (_, k) => k * 25.5);
  const counts = edges.slice(0, -1).map((lo, k) => all.filter((v) => v >= lo && (k === 9 ? v <= 255 : v < lo + 25.5)).length);
  const groups = CLASSES.map((cls, c) => {
    const members = pics.filter((_, i) => i % 3 === c);
    const sum = new Float64Array(IMG * IMG * 3);
    for (const p of members) p.forEach((v, k) => (sum[k] = sum[k]! + v / members.length));
    return { label: cls, n: members.length, mean: encodeRaster(luma(Uint8Array.from(sum, Math.round))), rgb: encodeRgb(triplets(sum)) };
  });
  const stat = (f: (v: number[]) => number) => lumas.map(f);
  const entry = {
    output: "pixels",
    var: "pixels",
    role: "covariate",
    units: null,
    facet: {},
    kind: "scalar",
    y_dim: "y",
    x_dim: "x",
    frame_dim: "image",
    height: IMG,
    width: IMG,
    source_height: IMG,
    source_width: IMG,
    downsampled: false,
    y_coords: Array.from({ length: IMG }, (_, k) => k),
    x_coords: Array.from({ length: IMG }, (_, k) => k),
    colormap: "gray",
    log: false,
    origin: "upper",
    encoding: "linear",
    n_draws: 0,
    frames: shown.map((i) => ({ label: imageId(i), mean: encodeRaster(lumas[i]!), rgb: encodeRgb(triplets(pics[i]!)) })),
    omitted_frames: N_IMAGES - shown.length,
    mask_var: null,
    mask: null,
    frame_stats: {
      coords: pics.map((_, i) => imageId(i)),
      mean: stat((v) => v.reduce((a, x) => a + x, 0) / v.length),
      min: stat((v) => Math.min(...v)),
      max: stat((v) => Math.max(...v)),
      missing_share: lumas.map(() => 0),
      downsampled: false,
    },
    histogram: { edges, counts },
    log_counts: false,
    stats: { min: Math.min(...all), max: Math.max(...all), mean, sd, missing_share: 0, saturated_share: 0 },
    group_by: "label",
    groups,
    components: [],
    color: "rgb",
    channel_dim: "channel",
    rgb_scale: "uint8",
  };
  const images = {
    format: "mapi.data-explorer.images/v1",
    caption_plain: "Shows each variable on its grid. Use the slider to go through the frames. The line under the image shows the mean of each frame.",
    warnings_plain: [
      "Some images have more than 12 frames. The viewer shows an even sample. The frame statistics use all the frames.",
      "The colour images show red, green and blue. The statistics, the histogram and the frame line use the brightness (0.299 red + 0.587 green + 0.114 blue).",
    ],
    fields: [entry],
    omitted: [],
  };
  const labelCounts = Object.fromEntries(CLASSES.map((c) => [c, N_IMAGES / 3]));
  const gallery = {
    format: "mapi.data-explorer.gallery/v1",
    caption_plain: "Shows small copies of the images. Use the filters to show one class or split. Click an image to see it larger.",
    warnings_plain: [],
    var: "pixels",
    frame_dim: "image",
    y_dim: "y",
    x_dim: "x",
    channel_dim: "channel",
    color: "rgb",
    source_height: IMG,
    source_width: IMG,
    thumb_height: IMG,
    thumb_width: IMG,
    n_images: N_IMAGES,
    items: pics.map((p, i) => ({ index: i, label: imageId(i), meta: { label: CLASSES[i % 3]!, split: splitOf(i) }, png: pngBase64(IMG, IMG, p) })),
    facets: [
      { name: "label", source: "var", counts: labelCounts },
      { name: "split", source: "coord", counts: { test: N_IMAGES / 4, train: (3 * N_IMAGES) / 4 } },
    ],
    selection: "all",
    other_vars: [],
  };
  return { images, gallery };
}

function imageArtifacts(): Map<string, Json> {
  const { images: imagesView, gallery } = imageFolderViews();
  const shape = { kind: "image", vars: ["pixels"], dims: { y: "y", x: "x", frame: "image", channel: "channel" }, score: 0.9, label_plain: `image: ${IMG} x ${IMG} cells, ${N_IMAGES} image`, sections: ["images", "gallery"] };
  const ok: Record<string, Json> = {
    overview: overview([shape], `${N_IMAGES} colour images of ${IMG} x ${IMG} pixels with a class label and a train/test split.`),
    images: imagesView,
    gallery,
    quality: ingestViews.quality!,
  };
  const out = new Map<string, Json>();
  const sections = [...ALL, "gallery"].map((s) =>
    ok[s] ? { section: s, view: `ui/explore/${s}.json` } : { section: s, view: `ui/explore/${s}.json`, status: "skipped", reason: "not_applicable", message_plain: (SCI_DEMO_NOT_APPLICABLE as readonly string[]).includes(s) ? "Not available: the data has no time dimension." : "Not available: this demo has only the image views." },
  );
  out.set("ui/explore/index.json", { format: "mapi.explorer-index/v1", target: "dataset", ref: "dataset://default/sci_demo_images@v1", shapes: ["image"], order: ["overview", "images", "gallery", "quality"], sections });
  for (const [s, v] of Object.entries(ok)) out.set(`ui/explore/${s}.json`, v);
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

function version(id: string, name: string, treeLayout: boolean, schema: DataSchema = grid): DatasetVersion {
  const v: DatasetVersion = { ...fx.datasetVersion, dataset_id: id, ref: `dataset://default/${name}@v1`, data_schema: schema };
  return treeLayout ? ({ ...v, layout: "tree", tree_schema: { nodes: { "/": { layout: "dense", dims: {}, vars: {} }, [IMAGE_NODE]: grid, [LINKS_NODE]: links } } }) : v;
}

export function createSciDemoHandlers(shared: { artifacts: Map<string, { bytes: Uint8Array; type: string }> }): HttpHandler[] {
  const demos = [
    { id: SCI_DEMO_ID, name: "sci_demo", artifacts: demoArtifacts(), tree: false },
    { id: SCI_DEMO_TREE_ID, name: "sci_demo_tree", artifacts: treeArtifacts(), tree: true },
    { id: SCI_DEMO_IMAGES_ID, name: "sci_demo_images", artifacts: imageArtifacts(), tree: false, schema: IMAGE_SCHEMA },
  ];
  const key = (id: string, name: string) => `${id}:${name}`;
  for (const d of demos) for (const [name, value] of d.artifacts) shared.artifacts.set(key(d.id, name), { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
  return demos.flatMap((d) => [
    http.get(`${API}/v1/datasets/${d.id}`, () => HttpResponse.json(d.id === SCI_DEMO_IMAGES_ID ? SCI_DEMO_IMAGES_DATASET : dataset(d.id, d.name))),
    http.get(`${API}/v1/datasets/${d.id}/versions`, () => HttpResponse.json({ items: [version(d.id, d.name, d.tree, d.schema)], next_cursor: null })),
    http.get(`${API}/v1/datasets/${d.id}/versions/:v`, ({ params }) =>
      params.v === "1" ? HttpResponse.json(version(d.id, d.name, d.tree, d.schema)) : HttpResponse.json({ type: "urn:mapi:error:NOT_FOUND", title: "Not found", status: 404, code: "NOT_FOUND", errors: [] }, { status: 404 }),
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
