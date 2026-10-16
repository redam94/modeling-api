/**
 * A deterministic left-to-right layered layout of the workflow graph: layers by longest path
 * from the workflow inputs, order within a layer by the barycentre of each node's sources
 * (two sweeps). Positions are a view of the document, never stored in it; a user's own
 * arrangement is kept per browser (see `canvas-positions.ts`).
 */
import { nodeDependencies } from "./doc";
import type { WorkflowDoc } from "./types";
import { INPUTS_NODE } from "./types";

export interface Point {
  x: number;
  y: number;
}

export const LAYER_GAP = 270;
export const ROW_GAP = 118;

export function layoutWorkflow(doc: WorkflowDoc): Record<string, Point> {
  const deps = nodeDependencies(doc);
  const usesInputs = new Set(
    doc.nodes.filter((n) => Object.values(n.inputs ?? {}).flat().some((b) => typeof b === "string" && b.startsWith("inputs."))).map((n) => n.id),
  );
  const layer = new Map<string, number>();
  const visiting = new Set<string>();
  const depth = (id: string): number => {
    const known = layer.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return 1; // a cycle: the server reports it; draw it anyway
    visiting.add(id);
    let d = 1;
    for (const x of deps.get(id) ?? []) d = Math.max(d, depth(x) + 1);
    visiting.delete(id);
    layer.set(id, d);
    return d;
  };
  for (const n of doc.nodes) depth(n.id);

  const layers: string[][] = [[INPUTS_NODE]];
  for (const n of doc.nodes) {
    const l = layer.get(n.id) ?? 1;
    (layers[l] ??= []).push(n.id);
  }
  const sources = (id: string): string[] => [...(deps.get(id) ?? []), ...(usesInputs.has(id) ? [INPUTS_NODE] : [])];
  const rowOf = new Map<string, number>();
  const assign = () => layers.forEach((ids) => ids?.forEach((id, i) => rowOf.set(id, i)));
  assign();
  for (let sweep = 0; sweep < 2; sweep++) {
    for (let l = 1; l < layers.length; l++) {
      const ids = layers[l];
      if (!ids) continue;
      const bary = (id: string) => {
        const s = sources(id).map((x) => rowOf.get(x)).filter((r): r is number => r !== undefined);
        return s.length ? s.reduce((a, b) => a + b, 0) / s.length : (rowOf.get(id) ?? 0);
      };
      const order = new Map(ids.map((id, i) => [id, i]));
      ids.sort((a, b) => bary(a) - bary(b) || order.get(a)! - order.get(b)!);
      ids.forEach((id, i) => rowOf.set(id, i));
    }
  }
  const tallest = Math.max(1, ...layers.map((ids) => ids?.length ?? 0));
  const out: Record<string, Point> = {};
  layers.forEach((ids, l) => {
    if (!ids) return;
    const offset = ((tallest - ids.length) * ROW_GAP) / 2;
    ids.forEach((id, i) => {
      out[id] = { x: l * LAYER_GAP, y: offset + i * ROW_GAP };
    });
  });
  return out;
}
