/**
 * Query keys of the library (`/v1/library`, v0.18).
 *
 * The library routes are project-scoped (they list the project's items and the tenant's),
 * so every key starts with `["p", "<tenant>/<project>", "library"]` like `qk`'s project
 * keys: a project switch never mixes caches. This module imports nothing at runtime.
 */
import type { LibraryKind, LibraryListFilters } from "./types";

export const lk = {
  /** Every library query of project `p`. */
  all: (p: string) => ["p", p, "library"] as const,
  /** Prefix of every list of `p`. */
  lists: (p: string) => ["p", p, "library", "list"] as const,
  list: (p: string, f: LibraryListFilters = {}) => ["p", p, "library", "list", f] as const,
  /** One item and everything under it. */
  item: (p: string, id: string) => ["p", p, "library", "item", id] as const,
  revisions: (p: string, id: string) => ["p", p, "library", "item", id, "revisions"] as const,
  revision: (p: string, id: string, rev: number) => ["p", p, "library", "item", id, "revisions", rev] as const,
  /** A `:validate` answer for one document (keyed by its canonical JSON + dataset). */
  validation: (p: string, docKey: string, dataset: string | null) => ["p", p, "library", "validate", docKey, dataset] as const,
  /** A chart template preview on its source recipe's example (keyed by the document). */
  chartPreview: (p: string, docKey: string) => ["p", p, "library", "chart-preview", docKey] as const,
  /** Items of one kind for a picker (non-archived). */
  picker: (p: string, kind: LibraryKind) => ["p", p, "library", "list", { kind }] as const,
};
