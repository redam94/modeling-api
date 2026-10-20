/**
 * The runs table's state in the URL (I-12) and its mapping to the `GET /v1/runs` filters and to
 * a saved view's query (`RunViewQuery`). The router validates the search with
 * {@link validateRunsSearch}; applying a saved view writes {@link searchFromViewQuery}.
 *
 * Search keys: `state`, `kind`, `experiment`, `name`, `f` (filters), `c` (columns), `o` (order),
 * `view` (the applied saved view) and, for views saved with them (the CLI can), `t` (tags),
 * `spec` and `parent`.
 */
import type { RunFilters } from "@/api/queries";
import type { RunKind, RunState, RunViewQuery } from "@/api/types";
import { RUN_KINDS, RUN_STATES } from "@/lib/run-state";

import { MAX_RUN_COLUMNS, MAX_RUN_FILTERS } from "./run-query";

export interface RunsSearch {
  state?: RunState | undefined;
  kind?: RunKind | undefined;
  experiment?: string | undefined;
  name?: string | undefined;
  f?: string[] | undefined;
  c?: string[] | undefined;
  o?: string | undefined;
  view?: string | undefined;
  t?: string[] | undefined;
  spec?: string | undefined;
  parent?: string | undefined;
}

/** Characters of one filter, column or order text in the URL. */
const MAX_REF_CHARS = 500;
const MAX_NAME_CHARS = 200;
const MAX_TAGS = 16;

function oneOf<T extends string>(v: unknown, allowed: readonly T[]): T | undefined {
  return typeof v === "string" && (allowed as readonly string[]).includes(v) ? (v as T) : undefined;
}

function idParam(v: unknown): string | undefined {
  return typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(v) ? v : undefined;
}

function text(v: unknown, max: number): string | undefined {
  return typeof v === "string" && v.trim() && v.length <= max ? v : undefined;
}

function texts(v: unknown, maxItems: number): string[] | undefined {
  const list = (Array.isArray(v) ? v : typeof v === "string" ? [v] : []).filter((x): x is string => typeof x === "string" && x.trim() !== "" && x.length <= MAX_REF_CHARS);
  return list.length ? [...new Set(list)].slice(0, maxItems) : undefined;
}

export function validateRunsSearch(s: Record<string, unknown>): RunsSearch {
  return {
    state: oneOf(s.state, RUN_STATES),
    kind: oneOf(s.kind, RUN_KINDS),
    experiment: idParam(s.experiment),
    name: text(s.name, MAX_NAME_CHARS),
    f: texts(s.f, MAX_RUN_FILTERS),
    c: texts(s.c, MAX_RUN_COLUMNS),
    o: text(s.o, MAX_REF_CHARS),
    view: idParam(s.view),
    t: texts(s.t, MAX_TAGS),
    spec: idParam(s.spec),
    parent: idParam(s.parent),
  };
}

/** The search without the empty keys (so `/runs` stays `/runs`). */
export function compactSearch(s: RunsSearch): RunsSearch {
  return Object.fromEntries(Object.entries(s).filter(([, v]) => v !== undefined && v !== "" && !(Array.isArray(v) && v.length === 0)));
}

/** The `GET /v1/runs` filters of a search. `experimentId` fixes the experiment (the experiment page). */
export function filtersFromSearch(s: RunsSearch, experimentId?: string): Omit<RunFilters, "cursor"> {
  const experiment = experimentId ?? s.experiment;
  return {
    ...(s.state ? { state: s.state } : {}),
    ...(s.kind ? { kind: s.kind } : {}),
    ...(experiment ? { experiment_id: experiment } : {}),
    ...(s.name?.trim() ? { name: s.name.trim() } : {}),
    ...(s.t?.length ? { tag: s.t } : {}),
    ...(s.f?.length ? { filter: s.f } : {}),
    ...(s.o ? { order_by: s.o } : {}),
    ...(s.c?.length ? { column: s.c } : {}),
    ...(s.spec ? { spec_id: s.spec } : {}),
    ...(s.parent ? { parent_run_id: s.parent } : {}),
  };
}

/** The saved-view query of a search (the experiment page saves its experiment in the view). */
export function viewQueryFromSearch(s: RunsSearch, experimentId?: string): RunViewQuery {
  const experiment = experimentId ?? s.experiment;
  return {
    ...(s.kind ? { kind: s.kind } : {}),
    ...(s.state ? { state: s.state } : {}),
    ...(s.name?.trim() ? { name: s.name.trim() } : {}),
    ...(s.spec ? { spec_id: s.spec } : {}),
    ...(experiment ? { experiment_id: experiment } : {}),
    ...(s.parent ? { parent_run_id: s.parent } : {}),
    tags: s.t ?? [],
    filters: s.f ?? [],
    ...(s.o ? { order_by: s.o } : {}),
    columns: s.c ?? [],
  };
}

/**
 * The search that applies a saved view. On the experiment page (`experimentId`) the view's
 * experiment is not written to the URL: the page fixes it.
 */
export function searchFromViewQuery(q: RunViewQuery, viewId: string | undefined, experimentId?: string): RunsSearch {
  return compactSearch({
    state: q.state ?? undefined,
    kind: q.kind ?? undefined,
    experiment: experimentId ? undefined : (q.experiment_id ?? undefined),
    name: q.name ?? undefined,
    f: q.filters?.length ? [...q.filters] : undefined,
    c: q.columns?.length ? [...q.columns] : undefined,
    o: q.order_by ?? undefined,
    view: viewId,
    t: q.tags?.length ? [...q.tags] : undefined,
    spec: q.spec_id ?? undefined,
    parent: q.parent_run_id ?? undefined,
  });
}

function normal(q: RunViewQuery) {
  return JSON.stringify([
    q.kind ?? null,
    q.state ?? null,
    q.name?.trim() || null,
    q.spec_id ?? null,
    q.experiment_id ?? null,
    q.parent_run_id ?? null,
    q.tags ?? [],
    q.filters ?? [],
    q.order_by ?? null,
    q.columns ?? [],
  ]);
}

/** Do two view queries select and show the same runs and columns? */
export function sameViewQuery(a: RunViewQuery, b: RunViewQuery): boolean {
  return normal(a) === normal(b);
}

/** Does the search narrow the runs (any filter beyond order and columns)? */
export function hasRunFilters(s: RunsSearch, experimentId?: string): boolean {
  return Boolean(s.state || s.kind || (!experimentId && s.experiment) || s.name?.trim() || s.f?.length || s.t?.length || s.spec || s.parent);
}
