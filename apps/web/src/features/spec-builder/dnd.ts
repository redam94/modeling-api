/**
 * Drag and drop between the palette and the spec blocks (native HTML5 DnD, as the workflow
 * canvas uses). The item kind is in the MIME type, because a drop target can only read the
 * types, not the data, while a drag is over it.
 */
import type { DragEvent } from "react";

export type PaletteKind = "dataset" | "step" | "model" | "plot";

export interface PaletteItem {
  kind: PaletteKind;
  /** A `use` (`core.resample@^1`), a dataset ref or a plot id. */
  id: string;
}

const PREFIX = "application/x-mapi-spec-";
/** A pipeline step being moved (its index). */
export const MOVE_MIME = `${PREFIX}move`;

export function dragPayload(item: PaletteItem): [string, string] {
  return [`${PREFIX}${item.kind}`, item.id];
}

/** Does the drag carry one of `kinds` (or a step move, with `"move"`)? */
export function carries(e: DragEvent, kinds: (PaletteKind | "move")[]): boolean {
  return kinds.some((k) => e.dataTransfer.types.includes(`${PREFIX}${k}`));
}

export function readDrop(e: DragEvent): PaletteItem | { kind: "move"; from: number } | null {
  const move = e.dataTransfer.getData(MOVE_MIME);
  if (move !== "") return { kind: "move", from: Number(move) };
  for (const kind of ["dataset", "step", "model", "plot"] as const) {
    const id = e.dataTransfer.getData(`${PREFIX}${kind}`);
    if (id) return { kind, id };
  }
  return null;
}
