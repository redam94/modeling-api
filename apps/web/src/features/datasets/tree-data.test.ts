import { describe, expect, it } from "vitest";

import type { DataSchema, InspectedArray } from "@/api/types";
import { enrichSpecSchema, inputColumns, specShape, type HintSources } from "@/features/editor/spec-hints";
import editorGen from "@/mocks/editor.gen.json";
import { TREE, treeAbout, treeSpecIssues } from "@/mocks/tree";

import { nodeIndexName } from "@/features/explore/api";

import { inspectBackoff, inspectionKey } from "./api";
import {
  combineFileText,
  combineSummaryText,
  coordDrafts,
  DEFAULT_PLACEMENT,
  formatFromFilename,
  initialHdf5Vars,
  needsAssembly,
  parseCoordValue,
  toAssembly,
  toHdf5Mapping,
  validateHdf5,
  validatePlacements,
  type Hdf5CoordDraft,
  type Hdf5VarDraft,
  type Placement,
} from "./mapping";
import { defaultNode, explorerNode, flatSchema, isNodePath, nodeKey, nodeParent, nodePathProblem, nodesWithVars, treeRows } from "./tree";
import { treeKeyTarget } from "./tree-view";

const place = (file: string, p: Partial<Placement> = {}): Placement => ({ file, ...DEFAULT_PLACEMENT, ...p });

describe("node paths", () => {
  it("accepts the root and nested names like mapi_spec.tree.NodePath", () => {
    for (const p of ["/", "/a", "/instrument/psf", "/a.b-c/_d", `/${"x".repeat(64)}`, "/a/b/c/d/e/f/g/h"]) expect(isNodePath(p)).toBe(true);
  });

  it("refuses the rest, with a reason", () => {
    expect(nodePathProblem("a")).toMatch(/must start with \//);
    expect(nodePathProblem("/a/")).toMatch(/must not end with \//);
    expect(nodePathProblem("/a//b")).toMatch(/empty name/);
    expect(nodePathProblem("/1a")).toMatch(/"1a" is not a valid node name/);
    expect(nodePathProblem(`/${"x".repeat(65)}`)).toMatch(/not a valid node name/);
    expect(nodePathProblem("/a/b/c/d/e/f/g/h/i")).toMatch(/9 levels\. The maximum is 8/);
    expect(nodePathProblem("/a b")).not.toBeNull();
    expect(nodePathProblem("/ok")).toBeNull();
  });

  it("finds parents, keys and the default node", () => {
    expect(nodeParent("/")).toBeNull();
    expect(nodeParent("/a")).toBe("/");
    expect(nodeParent("/a/b")).toBe("/a");
    expect(nodeKey("/instrument/psf")).toBe("psf");
    expect(nodeKey("/")).toBe("root");
    expect(nodeKey("/a/psf", ["psf"])).toBe("psf_2");
    const nodes: Record<string, DataSchema> = { "/": {}, "/b": { vars: { x: { dims: [], dtype: "float64" } } }, "/a": {} };
    expect(defaultNode(nodes)).toBe("/b");
    expect(nodesWithVars(nodes)).toEqual(["/b"]);
  });

  it("lays the fixture's tree out depth first", () => {
    const nodes = TREE.version.tree_schema!.nodes;
    expect(treeRows(nodes).map((r) => [r.path, r.depth])).toEqual([
      ["/", 0],
      ["/instrument", 1],
      ["/instrument/psf", 2],
    ]);
    expect(defaultNode(nodes)).toBe("/");
  });

  it("merges a data.nodes selection like TreeSchema.flat (dims from ancestors too)", () => {
    const nodes = TREE.version.tree_schema!.nodes;
    const flat = flatSchema(nodes, { s: { path: "/", vars: ["sales"] }, k: { path: "/instrument/psf", prefix: "psf_" } });
    expect(Object.keys(flat.vars ?? {}).sort()).toEqual(["psf_kernel", "sales"]);
    expect(Object.keys(flat.dims ?? {}).sort()).toEqual(["geo", "pcol", "prow", "time", "year"]);
  });
});

describe("formats", () => {
  it("detects HDF5 and keeps netCDF", () => {
    expect(formatFromFilename("a.h5")).toBe("hdf5");
    expect(formatFromFilename("A.HDF5")).toBe("hdf5");
    expect(formatFromFilename("x.he5")).toBe("hdf5");
    expect(formatFromFilename("x.nc")).toBe("netcdf");
    expect(formatFromFilename("x.nc4")).toBe("netcdf");
    expect(formatFromFilename("x.csv")).toBe("csv");
    expect(formatFromFilename("x.bin")).toBeNull();
  });

  it("reads labels as numbers when they are numbers", () => {
    expect(parseCoordValue("2024")).toBe(2024);
    expect(parseCoordValue(" 0.5 ")).toBe(0.5);
    expect(parseCoordValue("-1e3")).toBe(-1000);
    expect(parseCoordValue("north")).toBe("north");
    expect(parseCoordValue("2024-01-01")).toBe("2024-01-01");
  });

  it("keeps a label with a leading zero as text", () => {
    expect(parseCoordValue("007")).toBe("007");
    expect(parseCoordValue("-01")).toBe("-01");
    expect(parseCoordValue("0")).toBe(0);
    expect(parseCoordValue("0.25")).toBe(0.25);
    const sources = toAssembly(
      ["007", "008"].map((label, i) => ({ uploadId: `upl_${i}`, mapping: { format: "netcdf" }, placement: place(`f${i}.nc`, { combine: "concat", concatDim: "run", concatLabel: label }) })),
    ).sources;
    expect(sources.map((x) => x.concat_label)).toEqual(["007", "008"]);
  });
});

describe("assembly placements (IngestAssembly rules)", () => {
  it("needs an assembly for several files or a non-default placement only", () => {
    expect(needsAssembly([place("a.nc")])).toBe(false);
    expect(needsAssembly([place("a.nc", { node: "/x" })])).toBe(true);
    expect(needsAssembly([place("a.nc", { combine: "merge" })])).toBe(true);
    expect(needsAssembly([place("a.nc"), place("b.nc", { node: "/b" })])).toBe(true);
  });

  it("accepts concat + a node file and builds the AssemblySources", () => {
    const ps = [
      place("a.nc", { combine: "concat", concatDim: "year", concatLabel: "2024" }),
      place("b.nc", { combine: "concat", concatDim: "year", concatLabel: "2025" }),
      place("i.nc", { node: "/instrument" }),
    ];
    expect(validatePlacements(ps)).toEqual([]);
    const a = toAssembly(ps.map((p, i) => ({ uploadId: `upl_${i}`, mapping: { format: "netcdf" }, placement: p })));
    expect(a.sources).toEqual([
      { upload_id: "upl_0", mapping: { format: "netcdf" }, node: "/", combine: "concat", concat_dim: "year", concat_label: 2024, join: "exact" },
      { upload_id: "upl_1", mapping: { format: "netcdf" }, node: "/", combine: "concat", concat_dim: "year", concat_label: 2025, join: "exact" },
      { upload_id: "upl_2", mapping: { format: "netcdf" }, node: "/instrument", combine: "node" },
    ]);
  });

  it("shows every problem at once", () => {
    const messages = validatePlacements([
      place("a.nc"),
      place("b.nc"),
      place("c.nc", { node: "bad" }),
      place("d.nc", { node: "/m", combine: "concat", concatDim: "", concatLabel: "" }),
      place("e.nc", { node: "/m", combine: "merge" }),
      place("f.nc", { node: "/k", combine: "concat", concatDim: "year", concatLabel: "1" }),
      place("g.nc", { node: "/k", combine: "concat", concatDim: "yr", concatLabel: "1.0", join: "outer" }),
      place("h.nc", { node: "/t", combine: "concat", concatDim: "y", concatLabel: "1" }),
      place("i.nc", { node: "/t", combine: "concat", concatDim: "y", concatLabel: "north" }),
    ]).map((p) => p.message);
    expect(messages).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^Node \/ has 2 files \(a\.nc, b\.nc\)/),
        expect.stringMatching(/^c\.nc: The node "bad" must start with \//),
        expect.stringMatching(/^d\.nc: type the dim to stack/),
        expect.stringMatching(/^d\.nc: type the label/),
        expect.stringMatching(/^Node \/m: the files d\.nc, e\.nc combine in different ways/),
        expect.stringMatching(/^Node \/k: the files f\.nc, g\.nc must stack along the same dim/),
        expect.stringMatching(/^Node \/k: two files have the label 1 \(1 and 1\.0 are the same number\)\./),
        expect.stringMatching(/^Node \/k: the files f\.nc, g\.nc must align their coordinates/),
        expect.stringMatching(/^Node \/t: the labels mix numbers and text/),
      ]),
    );
    expect(messages).toHaveLength(9);
  });

  it("says which labels are text, and a zero-padded label no longer collides with a number", () => {
    const mixed = validatePlacements([
      place("a.nc", { combine: "concat", concatDim: "run", concatLabel: "007" }),
      place("b.nc", { combine: "concat", concatDim: "run", concatLabel: "7" }),
    ]).map((p) => p.message);
    expect(mixed).toEqual([expect.stringMatching(/^Node \/: the labels mix numbers and text \(007 is text\)\. .* leading zero, such as 007, is text\./)]);
    const same = validatePlacements([
      place("a.nc", { combine: "concat", concatDim: "year", concatLabel: "2024" }),
      place("b.nc", { combine: "concat", concatDim: "year", concatLabel: "2024" }),
    ]).map((p) => p.message);
    expect(same).toEqual(["Node /: two files have the label 2024. Give each file a different label."]);
  });

  it("summarises each node's files under their own rule when the rules differ", () => {
    const stacked = [
      { ...DEFAULT_PLACEMENT, combine: "concat" as const, concatDim: "year", concatLabel: "2024" },
      { ...DEFAULT_PLACEMENT, combine: "concat" as const, concatDim: "year", concatLabel: "2025" },
    ];
    expect(combineSummaryText(stacked)).toBe("← stacked along year:");
    expect(combineFileText("a.nc", stacked[0]!, false)).toBe("a.nc = 2024");
    const mixed = [...stacked, { ...DEFAULT_PLACEMENT }];
    expect(combineSummaryText(mixed)).toBe("← the files combine in different ways:");
    expect(combineFileText("a.nc", stacked[0]!, true)).toBe("a.nc = 2024 (stacked along year)");
    expect(combineFileText("weird.bin", DEFAULT_PLACEMENT, true)).toBe("weird.bin (the only file of the node)");
    expect(combineSummaryText([{ ...DEFAULT_PLACEMENT }])).toBe("←");
    expect(combineSummaryText([{ ...DEFAULT_PLACEMENT }, { ...DEFAULT_PLACEMENT }])).toBe("← select how these files combine:");
    expect(combineSummaryText([{ ...DEFAULT_PLACEMENT, combine: "merge", join: "outer" }])).toBe("← merged (outer):");
  });

  it("refuses more than 64 files", () => {
    const many = Array.from({ length: 65 }, (_, i) => place(`f${i}.nc`, { node: `/n${i}` }));
    expect(validatePlacements(many).map((p) => p.message)).toEqual([expect.stringMatching(/maximum of 64 files/)]);
  });
});

describe("raw HDF5 mapping (Hdf5Mapping rules)", () => {
  const arrays = TREE.inspection.arrays as InspectedArray[];
  const drafts = () => initialHdf5Vars(arrays);
  const pick = (vars: Hdf5VarDraft[], path: string, patch: Partial<Hdf5VarDraft>) => vars.map((v) => (v.path === path ? { ...v, include: true, ...patch } : v));

  it("prefills names, dims from the file's dim names and units", () => {
    const vars = drafts();
    const flux = vars.find((v) => v.path === "/cube/flux")!;
    expect(flux).toMatchObject({ name: "flux", dims: "band, pixel", include: false, node: "/" });
    const image = vars.find((v) => v.path === "/observations/image")!;
    expect(image).toMatchObject({ name: "image", dims: "image_0, image_1", units: "counts" });
  });

  it("builds the fixture's mapping (which the generator ingested with the real runner)", () => {
    let vars = pick(drafts(), "/observations/image", { dims: "row, col", role: "target" });
    vars = pick(vars, "/psf/kernel", { node: "/psf", dims: "prow, pcol" });
    let coords = coordDrafts(vars, []);
    expect(coords.map((c) => `${c.node}:${c.dim}`)).toEqual(["/:row", "/:col", "/psf:prow", "/psf:pcol"]);
    coords = coords.map((c): Hdf5CoordDraft => (c.dim === "row" ? { ...c, source: "path", path: "/axes/rows" } : c.dim === "col" ? { ...c, source: "range", start: "0", step: "0.5" } : c));
    expect(validateHdf5(vars, coords, arrays)).toEqual([]);
    const mapping = toHdf5Mapping(vars, coords);
    const expected = TREE.hdf5_mapping as { hdf5: { vars: object[]; coords: object[] } };
    expect(mapping.hdf5!.coords).toEqual(expected.hdf5.coords.map((c) => ({ node: "/", ...c })));
    expect(mapping.hdf5!.vars).toEqual(expected.hdf5.vars.map((v) => ({ node: "/", role: "covariate", ...v })));
  });

  it("keeps one coord per dim at the highest node that uses it", () => {
    let vars = pick(drafts(), "/axes/rows", { name: "r", node: "/a/b", dims: "x" });
    vars = pick(vars, "/spare/unused", { name: "u", node: "/a", dims: "x" });
    expect(coordDrafts(vars, []).map((c) => `${c.node}:${c.dim}`)).toEqual(["/a:x"]);
  });

  it("shows every problem before submit", () => {
    let vars = pick(drafts(), "/observations/image", { name: "row", dims: "row" });
    vars = pick(vars, "/psf/kernel", { name: "1bad", node: "psf", dims: "k, k" });
    vars = pick(vars, "/axes/rows", { name: "rows", dims: "row" });
    vars = pick(vars, "/spare/unused", { name: "rows", dims: "row" });
    const coords: Hdf5CoordDraft[] = [
      { dim: "row", node: "/", source: "path", path: "/observations/image", start: "", step: "" },
      { dim: "k", node: "psf", source: "range", path: "", start: "2024-01-01", step: "2" },
      { dim: "z", node: "/", source: "range", path: "", start: "", step: "" },
      { dim: "q", node: "/", source: "range", path: "", start: "1", step: "0" },
    ];
    const messages = validateHdf5(vars, coords, arrays).map((p) => p.message);
    expect(messages).toEqual(
      expect.arrayContaining([
        expect.stringMatching(/^\/observations\/image has 2 axes \(shape 4 × 5\)\. Type 2 dim names, not 1\./),
        expect.stringMatching(/the variable "row" has the name of one of its dims/),
        expect.stringMatching(/^\/psf\/kernel: The variable name "1bad" is not a valid name/),
        expect.stringMatching(/^\/psf\/kernel: The node "psf" must start with \//),
        expect.stringMatching(/^\/psf\/kernel: each axis needs a different dim name/),
        expect.stringMatching(/^Two arrays make the variable "rows" in node \//),
        expect.stringMatching(/^The dim "row" has 7 values in \/spare\/unused but 4 in/),
        expect.stringMatching(/^Node \/: row is used as a dim and as a variable/),
        expect.stringMatching(/^Coordinate row: \/observations\/image must be a 1-D array/),
        expect.stringMatching(/a range is two numbers .* not a mix/),
        expect.stringMatching(/^Coordinate z: type the first value/),
        expect.stringMatching(/^Coordinate q: the step must not be 0/),
      ]),
    );
  });

  it("points each problem at the input it is about", () => {
    let vars = pick(drafts(), "/cube/flux", { dims: "x" });
    vars = pick(vars, "/psf/kernel", { node: "psf", dims: "a, b" });
    vars = pick(vars, "/observations/image", { name: "1x", dims: "r, c", units: "u".repeat(80) });
    const coords: Hdf5CoordDraft[] = [
      { dim: "a", node: "psf", source: "path", path: "", start: "", step: "" },
      { dim: "r", node: "/", source: "range", path: "", start: "", step: "" },
      { dim: "c", node: "/", source: "range", path: "", start: "1", step: "0" },
    ];
    const at = (key: string) =>
      validateHdf5(vars, coords, arrays)
        .filter((p) => p.key === key)
        .map((p) => p.field)
        .sort();
    expect(at("/cube/flux")).toEqual(["dims"]);
    expect(at("/psf/kernel")).toEqual(["node"]);
    expect(at("/observations/image")).toEqual(["name", "units"]);
    expect(at("coord:psf:a")).toEqual(["path"]);
    expect(at("coord:/:r")).toEqual(["start"]);
    expect(at("coord:/:c")).toEqual(["step"]);
  });

  it("refuses units longer than Hdf5Var.units allows and sends missing values when not forbid", () => {
    let vars = pick(drafts(), "/observations/image", { dims: "r, c", units: "u".repeat(65) });
    expect(validateHdf5(vars, [], arrays).map((p) => p.message)).toEqual(["/observations/image: the units have 65 characters. The maximum is 64."]);
    vars = pick(vars, "/observations/image", { units: "u".repeat(64), missing: "allow" });
    vars = pick(vars, "/psf/kernel", { dims: "p, q" });
    expect(validateHdf5(vars, [], arrays)).toEqual([]);
    const out = toHdf5Mapping(vars, []).hdf5!.vars;
    expect(out.find((v) => v.path === "/observations/image")).toMatchObject({ units: "u".repeat(64), missing: "allow" });
    expect(out.find((v) => v.path === "/psf/kernel")).not.toHaveProperty("missing");
  });

  it("asks for an array when nothing is mapped, and checks label lengths", () => {
    expect(validateHdf5(drafts(), [], arrays).map((p) => p.message)).toEqual(["Select one or more arrays to make variables."]);
    const vars = pick(drafts(), "/psf/kernel", { dims: "a, b" });
    const coords: Hdf5CoordDraft[] = [{ dim: "a", node: "/", source: "path", path: "/axes/rows", start: "", step: "" }];
    expect(validateHdf5(vars, coords, arrays).map((p) => p.message)).toEqual(["Coordinate a: /axes/rows has 4 values but the dim has 3."]);
  });
});

describe("inspection polling and explorer node indexes", () => {
  it("backs off from 0.5 s to at most 5 s", () => {
    expect([1, 2, 3, 4, 10, 50].map(inspectBackoff)).toEqual([500, 750, 1125, 1688, 5000, 5000]);
  });

  it("keys the inspection query by its run, so a retry after a failed run fetches again", () => {
    expect(inspectionKey("p1", "ds", "upl", "run_a")).not.toEqual(inspectionKey("p1", "ds", "upl", "run_b"));
    expect(inspectionKey("p1", "ds", "upl", "run_a").at(-1)).toBe("run_a");
  });

  it("explores only a node with variables, and says why another node is not shown", () => {
    const nodes: Record<string, DataSchema> = { "/": {}, "/a": { vars: { x: { dims: [], dtype: "float64" } } }, "/a/g": {} };
    expect(explorerNode(nodes, "/a")).toEqual({ node: "/a", note: null });
    expect(explorerNode(nodes, undefined)).toEqual({ node: "/a", note: null });
    expect(explorerNode(nodes, "/")).toEqual({ node: "/a", note: "The node / has no variables to explore. The page shows /a." });
    expect(explorerNode(nodes, "/a/g").node).toBe("/a");
    expect(explorerNode(nodes, "/nope").note).toBe("This dataset has no node /nope. The page shows /a.");
  });

  it("moves through the nodes with the tree keys", () => {
    const rows = treeRows(TREE.version.tree_schema!.nodes);
    expect(rows.map((r) => r.path)).toEqual(["/", "/instrument", "/instrument/psf"]);
    expect(treeKeyTarget(rows, 0, "ArrowDown")).toBe(1);
    expect(treeKeyTarget(rows, 2, "ArrowDown")).toBe(2);
    expect(treeKeyTarget(rows, 1, "ArrowUp")).toBe(0);
    expect(treeKeyTarget(rows, 2, "Home")).toBe(0);
    expect(treeKeyTarget(rows, 0, "End")).toBe(2);
    expect(treeKeyTarget(rows, 1, "ArrowRight")).toBe(2);
    expect(treeKeyTarget(rows, 2, "ArrowRight")).toBe(-1);
    expect(treeKeyTarget(rows, 2, "ArrowLeft")).toBe(1);
    expect(treeKeyTarget(rows, 0, "ArrowLeft")).toBe(-1);
    expect(treeKeyTarget(rows, 0, "a")).toBeNull();
  });

  it("finds a node's explorer index only under ui/explore/nodes/", () => {
    const index = TREE.artifacts["ui/explore/nodes/index.json"];
    expect(nodeIndexName(index, "/instrument/psf")).toBe("ui/explore/nodes/instrument~psf/index.json");
    expect(nodeIndexName(index, "/nope")).toBeNull();
    expect(nodeIndexName({ nodes: { "/": { index: "../../secret.json" } } }, "/")).toBeNull();
    expect(nodeIndexName({ nodes: { "/": { index: "ui/explore/nodes/../x.json" } } }, "/")).toBeNull();
  });
});

describe("spec editor data.nodes hints", () => {
  const tree = TREE.version.tree_schema!.nodes;
  const sources: HintSources = { catalog: null, datasets: [TREE.dataset], columns: TREE.version.data_schema ?? null, stages: [], tree };

  it("suggests node keys with their paths, node paths and node variables", () => {
    const shape = specShape({ data: { dataset: "dataset://default/lens_cubes@latest", nodes: { psf: { path: "/instrument/psf" } } } });
    const schema = enrichSpecSchema(editorGen.modelspec_schema, shape, sources) as { $defs: Record<string, { properties: Record<string, unknown> }> };
    const nodes = schema.$defs.DataBlock!.properties.nodes as { anyOf: { properties?: Record<string, { defaultSnippets: { body: unknown }[] }> }[]; defaultSnippets: { label: string }[] };
    const block = nodes.anyOf.find((s) => s.properties)!;
    expect(Object.keys(block.properties!)).toEqual(["root", "instrument", "psf"]);
    expect(block.properties!.psf!.defaultSnippets[0]!.body).toEqual({ path: "/instrument/psf" });
    expect(nodes.defaultSnippets.map((s) => s.label)).toEqual(["root: /", "instrument: /instrument", "psf: /instrument/psf"]);
    const path = schema.$defs.NodeSelect!.properties.path as { defaultSnippets: { label: string }[] };
    expect(path.defaultSnippets.map((s) => s.label)).toEqual(["/", "/instrument", "/instrument/psf"]);
    // The binding sees the merged names of the selected nodes.
    expect(Object.keys(inputColumns(shape, sources)?.vars ?? {})).toEqual(["kernel"]);
  });

  it("reads the root node without data.nodes, and a plain dataset as before", () => {
    const shape = specShape({ data: { dataset: "dataset://default/lens_cubes@latest" } });
    expect(Object.keys(inputColumns(shape, sources)?.vars ?? {}).sort()).toEqual(["sales", "tv"]);
    const plain = { ...sources, tree: null, columns: { vars: { y: { dims: [], dtype: "float64" } } } as DataSchema };
    expect(Object.keys(inputColumns(shape, plain)?.vars ?? {})).toEqual(["y"]);
    const schema = enrichSpecSchema(editorGen.modelspec_schema, shape, plain) as { $defs: Record<string, { properties: Record<string, unknown> }> };
    expect((schema.$defs.NodeSelect!.properties.path as { defaultSnippets?: unknown }).defaultSnippets).toBeUndefined();
  });
});

describe("the tree dataset mock", () => {
  const spec = (data: string) => `apiVersion: mapi/v1\nkind: ModelSpec\nmetadata: {name: x, project: default}\ndata:\n${data}\nmodel: {use: mmm.bayesian@^1}\n`;

  it("describes the tree dataset in its own About card", () => {
    const about = treeAbout("mine");
    expect(about.description_user).toBe("mine");
    expect(about.highlights).toEqual(["3 nodes", "6 weeks", "2 geos", "2 years"]);
    expect(about.summary_generated).toContain("/instrument/psf (kernel over prow, pcol)");
    expect(about.summary_generated).not.toMatch(/channel|156 weeks/);
  });

  it("checks data.bind against the selected nodes", () => {
    const stale = spec("  dataset: dataset://default/lens_cubes@latest\n  nodes: {root: {path: /}}\n  bind: {dims: {geo: market, time: date}, vars: {y: revenue_local}}");
    expect(treeSpecIssues(stale).map((i) => [i.code, i.pointer, i.message])).toEqual([
      ["BINDING_INVALID", "/data/bind/dims/geo", "the dataset has no dim with the name 'market'"],
      ["BINDING_INVALID", "/data/bind/dims/time", "the dataset has no dim with the name 'date'"],
      ["BINDING_INVALID", "/data/bind/vars/y", "the dataset has no variable with the name 'revenue_local'"],
    ]);
    const ok = spec("  dataset: dataset://default/lens_cubes@latest\n  nodes: {root: {path: /}, psf: {path: /instrument/psf, prefix: p_}}\n  bind: {dims: {geo: geo, time: time}, vars: {y: sales, k: p_kernel}}");
    expect(treeSpecIssues(ok)).toEqual([]);
    // without data.nodes the root node is read
    expect(treeSpecIssues(spec("  dataset: dataset://default/lens_cubes@latest\n  bind: {vars: {y: kernel}}")).map((i) => i.pointer)).toEqual(["/data/bind/vars/y"]);
  });
});
