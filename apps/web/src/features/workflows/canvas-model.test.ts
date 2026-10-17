import { describe, expect, it } from "vitest";

import generated from "@/mocks/workflows.gen.json";

import { buildCanvasEdges, buildCanvasNodes, checkCanvasConnection, GATE_HANDLE } from "./canvas-model";
import type { Catalog } from "./ports";
import type { NodeImplementation, WorkflowDoc } from "./types";

const catalog: Catalog = new Map((generated.nodes as unknown as NodeImplementation[]).map((n) => [n.id, n]));
const doc = generated.templates.find((t) => t.id === "ts_readiness_arimax")!.document as unknown as WorkflowDoc;

describe("the canvas graph", () => {
  it("draws data edges per binding, numbering alternatives", () => {
    const edges = buildCanvasEdges(doc, catalog);
    const diff = edges.filter((e) => e.target === "diff" && e.kind === "data");
    expect(diff.map((e) => [e.source, e.sourceHandle, e.targetHandle, e.label])).toEqual([
      ["sdiff", "out:data", "in:data", "alt 1"],
      ["inputs", "out:data", "in:data", "alt 2"],
    ]);
    // A multiple port collects sources: no alternative numbers.
    expect(edges.filter((e) => e.target === "report" && e.port === "tests").every((e) => e.label === null)).toBe(true);
    expect(edges.find((e) => e.target === "adf")?.portType).toBe("dataset");
  });

  it("draws gates from branch arms and the results expressions read", () => {
    const edges = buildCanvasEdges(doc, catalog);
    expect(edges.find((e) => e.id === "w:diff")).toMatchObject({ kind: "control", source: "wanders", sourceHandle: "arm:then", targetHandle: GATE_HANDLE });
    // `order` reads the branch through `{$expr: if(nodes.wanders.value, 1, 0)}` without a binding.
    expect(edges.find((e) => e.id === "r:wanders:order")).toMatchObject({ kind: "reads" });
    // A condition reads its tests: they are drawn as reads edges into the branch.
    expect(edges.filter((e) => e.target === "wanders").map((e) => e.source).sort()).toEqual(["adf", "kpss", "overdiff", "pp", "za"]);
  });

  it("describes nodes with their ports, gate and issues", () => {
    const nodes = buildCanvasNodes(doc, catalog, { issues: new Map([["adf", [{ code: "WORKFLOW_INVALID", message: "x", pointer: "/nodes/0" }]]]) });
    expect(nodes[0]).toMatchObject({ nodeId: "inputs", role: "inputs", outputs: [["data", "dataset"]] });
    const sdiff = nodes.find((n) => n.nodeId === "sdiff")!;
    expect(sdiff.gatedBy).toEqual({ branch: "seasonal", arm: "then" });
    expect(sdiff.title).toBe(catalog.get("transform.seasonal_difference")!.about.title);
    expect(nodes.find((n) => n.nodeId === "adf")!.issues).toHaveLength(1);
    expect(nodes.find((n) => n.nodeId === "wanders")!.isBranch).toBe(true);
  });

  it("checks what a drag would do before it does it", () => {
    const check = (source: string, sourceHandle: string, target: string, targetHandle: string) => checkCanvasConnection(doc, catalog, { source, sourceHandle, target, targetHandle });
    expect(check("wanders", "arm:then", "acf", GATE_HANDLE)).toMatchObject({ kind: "control", ok: true, arm: "then" });
    const armOnData = check("wanders", "arm:else", "acf", "in:data");
    expect(!armOnData.ok && armOnData.reason).toMatch(/does not carry data/);
    const dataOnGate = check("adf", "out:result", "order", GATE_HANDLE);
    expect(!dataOnGate.ok && dataOnGate.reason).toMatch(/takes a branch arm/);
    expect(check("diff", "out:data", "wanders", GATE_HANDLE)).toMatchObject({ ok: false });
    expect(check("diff", "out:data", "order", "in:data")).toMatchObject({ kind: "data", ok: true, mode: "replace", port: "data", sourcePort: "data" });
  });
});
