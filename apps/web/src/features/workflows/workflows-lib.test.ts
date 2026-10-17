import { describe, expect, it } from "vitest";

import generated from "@/mocks/workflows.gen.json";

import { cronProblem, describeCron, nextFires, parseCron } from "./cron";
import {
  addNode,
  asWorkflowDoc,
  connect,
  disconnect,
  expressionTexts,
  moveAlternative,
  nodeDependencies,
  nodeOfPointer,
  parseDocText,
  removeNode,
  renameNode,
  setNodeFields,
  toYaml,
  topologicalOrder,
} from "./doc";
import { layoutWorkflow } from "./layout";
import { checkConnection, compatibleSources, gateCandidates, subworkflowPortsOf, unboundRequired, type Catalog } from "./ports";
import type { NodeImplementation, WorkflowDoc } from "./types";

const catalog: Catalog = new Map((generated.nodes as unknown as NodeImplementation[]).map((n) => [n.id, n]));
const canonical = (generated.templates.find((t) => t.id === "ts_readiness_arimax")!.document as unknown) as WorkflowDoc;
const doc = () => structuredClone(canonical);

describe("workflow documents", () => {
  it("reads dependencies from bindings, gates and expressions", () => {
    const deps = nodeDependencies(doc());
    expect([...deps.get("wanders")!].sort()).toEqual(["adf", "kpss", "overdiff", "pp", "za"]);
    expect([...deps.get("diff")!].sort()).toEqual(["sdiff", "wanders"]);
    // `order` reads the branch through `{$expr: if(nodes.wanders.value, 1, 0)}`
    expect(deps.get("order")!.has("wanders")).toBe(true);
    expect(expressionTexts(doc().nodes.find((n) => n.id === "order")!).map((e) => e.path.join("/"))).toEqual(["d", "D"]);
    expect(topologicalOrder(doc())).toHaveLength(canonical.nodes.length);
  });

  it("adds, connects, renames and removes nodes without breaking references", () => {
    let d = doc();
    const added = addNode(d, "test.ljung_box");
    d = connect(added.doc, "diff.data", added.id, "data", "replace");
    d = connect(d, "inputs.data", added.id, "data", "append");
    expect(d.nodes.at(-1)).toEqual({ id: "ljung_box", use: "test.ljung_box", inputs: { data: ["diff.data", "inputs.data"] } });
    d = moveAlternative(d, "ljung_box", "data", 0, 1);
    expect(d.nodes.at(-1)!.inputs!.data).toEqual(["inputs.data", "diff.data"]);
    d = renameNode(d, "wanders", "needs_diff");
    expect(d.nodes.find((n) => n.id === "diff")!.when).toBe("needs_diff.then");
    expect(JSON.stringify(d.nodes.find((n) => n.id === "order")!.with)).toContain("nodes.needs_diff.value");
    d = removeNode(d, "diff");
    expect(d.nodes.find((n) => n.id === "ljung_box")!.inputs!.data).toBe("inputs.data");
    expect(d.nodes.find((n) => n.id === "acf")!.inputs!.data).toEqual(["sdiff.data", "inputs.data"]);
    d = disconnect(d, "ljung_box", "data");
    expect(d.nodes.find((n) => n.id === "ljung_box")!.inputs).toBeUndefined();
    d = setNodeFields(d, "ljung_box", { retries: 0, cache: false, label: "Residual check" });
    expect(d.nodes.find((n) => n.id === "ljung_box")).toEqual({ id: "ljung_box", use: "test.ljung_box", cache: false, label: "Residual check" });
  });

  it("round-trips through YAML and refuses shapes the canvas cannot draw", () => {
    const text = toYaml(doc());
    const parsed = parseDocText(text);
    expect(parsed.ok && parsed.doc).toEqual(canonical);
    expect(text).toContain("condition: (not nodes.adf.reject or nodes.kpss.reject)");
    expect(parseDocText("kind: Spec\n")).toEqual({ ok: false, error: "kind must be Workflow." });
    expect(parseDocText("a: [")).toMatchObject({ ok: false });
    expect(asWorkflowDoc({ kind: "Workflow", metadata: {}, nodes: [{ id: "a", use: "test.adf" }, { id: "a", use: "test.kpss" }] })).toEqual({ ok: false, error: "Two nodes are called a." });
  });

  it("pins issue pointers to nodes", () => {
    expect(nodeOfPointer(canonical, "/nodes/9/with/condition")).toEqual({ id: "wanders", rest: ["with", "condition"] });
    expect(nodeOfPointer(canonical, "/params/alpha")).toBeNull();
  });

  it("lays the graph out left to right by dependency depth", () => {
    const pos = layoutWorkflow(canonical);
    expect(pos.inputs!.x).toBe(0);
    expect(pos.adf!.x).toBeLessThan(pos.wanders!.x);
    expect(pos.wanders!.x).toBeLessThan(pos.diff!.x);
    expect(pos.select!.x).toBeLessThan(pos.governance!.x);
  });
});

describe("typed ports", () => {
  it("refuses incompatible connections with a reason", () => {
    const d = doc();
    expect(checkConnection(d, catalog, { node: "adf", port: "result" }, { node: "acf", port: "data" })).toEqual({
      ok: false,
      reason: "acf.data takes dataset, but adf.result gives test result.",
    });
    expect(checkConnection(d, catalog, { node: "acf", port: "result" }, { node: "acf", port: "data" })).toEqual({ ok: false, reason: "A node cannot feed its own input." });
    const cycle = checkConnection(d, catalog, { node: "diff", port: "data" }, { node: "sdiff", port: "data" });
    expect(cycle.ok).toBe(false);
    expect(!cycle.ok && cycle.reason).toMatch(/^This would create a cycle: .*sdiff.*diff/);
    expect(checkConnection(d, catalog, { node: "inputs", port: "data" }, { node: "adf", port: "data" })).toEqual({ ok: false, reason: "inputs.data already feeds adf.data." });
    expect(checkConnection(d, catalog, { node: "diff", port: "data" }, { node: "adf", port: "nope" })).toEqual({ ok: false, reason: 'adf has no input "nope".' });
  });

  it("collects on multiple ports and replaces single ones", () => {
    const d = doc();
    expect(checkConnection(d, catalog, { node: "seas", port: "result" }, { node: "report", port: "tests" })).toMatchObject({ ok: false });
    const added = addNode(d, "test.ljung_box");
    expect(checkConnection(added.doc, catalog, { node: "ljung_box", port: "result" }, { node: "report", port: "tests" })).toMatchObject({ ok: true, mode: "append" });
    expect(checkConnection(d, catalog, { node: "diff", port: "data" }, { node: "order", port: "data" })).toMatchObject({ ok: true, mode: "replace" });
    const sources = compatibleSources(added.doc, catalog, { node: "ljung_box", port: "model" }).map((s) => s.binding);
    expect(sources).toEqual(["fit.model", "select.model"]);
    expect(unboundRequired(added.doc.nodes.at(-1)!, catalog)).toEqual([]);
    expect(unboundRequired({ id: "x", use: "model.backtest" }, catalog)).toEqual(["data", "split"]);
    expect(gateCandidates(d, "diff")).toEqual(["seasonal", "wanders"]);
  });

  it("derives a sub-workflow's ports from the child document", () => {
    const ports = subworkflowPortsOf(canonical, catalog);
    expect(ports.inputs.data).toMatchObject({ type: "dataset", required: true });
    expect(ports.outputs).toEqual({ model: "model_version", report: "report", evidence: "evidence" });
  });
});

describe("cron preview", () => {
  it.each(generated.cron_vectors)("$cron in $timezone matches mapi_spec.cron.next_fire", (v) => {
    const got = nextFires(v.cron, v.timezone, v.next.length, Date.parse(v.after)).map((t) => new Date(t).toISOString().replace(".000Z", "Z"));
    expect(got).toEqual(v.next);
  });

  it("refuses what the server refuses", () => {
    expect(cronProblem("*/5 * * * *")).toBe("schedules may fire at most every 15 minutes");
    expect(cronProblem("0 6 * *")).toBe("a cron expression has 5 fields: minute hour day month weekday");
    expect(cronProblem("0 25 * * *")).toBe("25 is outside 0-23");
    expect(cronProblem("0 0 30 2 *")).toBe("the schedule never fires");
    expect(cronProblem("*/15 * * * *")).toBeNull();
    expect(parseCron("0 6 * * mon-fri").weekdays).toEqual(new Set([1, 2, 3, 4, 5]));
    expect(describeCron("0 6 * * 1")).toBe("Every Monday at 06:00");
    expect(describeCron("@daily")).toBe("Every day at midnight");
  });
});
