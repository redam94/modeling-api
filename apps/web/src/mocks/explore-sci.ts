/**
 * Mock API of the Data Explorer on scientific data, serving `explorer-sci.gen.json`: the
 * synthetic exports of eleven pymc-challenges cases (`mapi_plugin_science.export`) and the
 * E80 lens as a tree dataset, each ingested by the real runner (`run_ingest`) and explored by
 * an explore run of every section (`scripts/gen_explorer_fixtures.py --science`).
 *
 * - `ds-sci-<case>` (`ds-sci-e07` … `ds-sci-e81b`, `ds-sci-e80tree`): the dataset, its one
 *   version, its About card and every artifact the ingest wrote (`ui/explore/…`);
 * - `POST …/explore` on these datasets starts an explore run that finishes
 *   {@link SCI_RUN_MS} later with the views of the computed run (and the ingest's views for the
 *   sections the run computed identically); on the tree, it serves the ingest views of the node.
 *
 * The handlers come before the generic dataset and explorer routes and answer only for these
 * datasets.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { Dataset, DatasetVersion, Problem, Run } from "@/api/types";
import { env } from "@/env";

import gen from "./explorer-sci.gen.json";
import * as fx from "./fixtures";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

/** How long a mock explore run of a science dataset takes. */
export const SCI_RUN_MS = 1200;

type Json = Record<string, unknown>;
interface SectionRef {
  section: string;
  view: string;
  status?: "ok" | "skipped" | "error";
  plots?: string;
  [key: string]: unknown;
}
interface Index {
  format: string;
  target: "dataset";
  ref: string;
  sections: SectionRef[];
  [key: string]: unknown;
}

export interface SciDataset {
  /** The export case (`e07`, …, `e80tree`). */
  case: string;
  dataset: Dataset;
  version: DatasetVersion;
  /** The artifacts the ingest wrote, by name (`summary.json`, `ui/explore/index.json`, …). */
  artifacts: Record<string, unknown>;
  /** The default explore run of every section (plain datasets only). */
  explore: { request?: Json; artifacts?: Record<string, unknown> };
  about: Json | null;
  warnings: string[];
}

export const SCI = (gen as unknown as { datasets: SciDataset[] }).datasets;
export const SCI_DATASETS: Dataset[] = SCI.map((d) => d.dataset);
/** The mock dataset id of an export case (`e07` -> `ds-sci-e07`). */
export const sciDatasetId = (c: string) => `ds-sci-${c}`;

const byId = new Map(SCI.map((d) => [d.dataset.id, d]));

export interface SciMockState {
  runs: Map<string, { id: string; started: number; ds: SciDataset; index: Index; views: Map<string, string>; node: string | null }>;
  cache: Map<string, string>;
  descriptions: Map<string, string | null>;
  seq: number;
}

export function createSciState(): SciMockState {
  return { runs: new Map(), cache: new Map(), descriptions: new Map(), seq: 0 };
}

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

/** The About card of a science dataset (the tree's is made from its schema). */
function aboutOf(d: SciDataset, description: string | null | undefined) {
  const user = description === undefined ? (d.dataset.description ?? null) : description;
  if (d.about) return { ...d.about, description_user: user, generated_at: fx.T0 };
  const nodes = Object.entries(d.version.tree_schema?.nodes ?? {}).filter(([, s]) => Object.keys(s.vars ?? {}).length);
  const parts = nodes.map(([path, s]) => `${path} (${Object.keys(s.vars ?? {}).join(", ")})`);
  return {
    description_user: user,
    highlights: [`${nodes.length} nodes with variables`],
    summary_generated: `A tree dataset with images in ${parts.join(" and ")}.`,
    warnings_plain: [],
    generated_at: fx.T0,
  };
}

/** The node index of a tree dataset's node (`ui/explore/nodes/<slug>/index.json`). */
function nodeIndexName(d: SciDataset, node: string | null): string | null {
  const nodes = d.artifacts["ui/explore/nodes/index.json"] as { default?: string; nodes?: Record<string, { index: string }> } | undefined;
  if (!nodes?.nodes) return null;
  const path = node ?? nodes.default ?? Object.keys(nodes.nodes)[0] ?? "/";
  return nodes.nodes[path]?.index ?? null;
}

export function createSciHandlers(
  state: SciMockState,
  shared: { artifacts: Map<string, { bytes: Uint8Array; type: string }>; runs: Map<string, Run> },
): HttpHandler[] {
  const key = (d: SciDataset, name: string, part: "ingest" | "run" = "ingest") => `sci:${d.case}:${part}:${name}`;
  for (const d of SCI) {
    for (const [name, value] of Object.entries(d.artifacts)) shared.artifacts.set(key(d, name), { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
    for (const [name, value] of Object.entries(d.explore.artifacts ?? {})) shared.artifacts.set(key(d, name, "run"), { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
  }
  const linkOf = (k: string, name: string) => {
    const a = shared.artifacts.get(k);
    return a ? { ...fx.artifactLink(k, a.bytes, a.type), name } : null;
  };
  const isDone = (started: number) => Date.now() - started >= SCI_RUN_MS;
  const runOf = (id: string, started: number): Run => {
    const done = isDone(started);
    const run: Run = {
      ...fx.runDone,
      id,
      kind: "explore",
      state: done ? "succeeded" : "running",
      spec_id: null,
      spec_revision: null,
      output_model_version_id: null,
      problem: null,
      progress: done ? null : { seq: 3, phase: "fit", fraction: Math.min(0.9, (Date.now() - started) / SCI_RUN_MS), message: "computing explorer sections", updated_at: new Date().toISOString() },
      created_at: new Date(started).toISOString(),
      started_at: new Date(started).toISOString(),
      finished_at: done ? new Date(started + SCI_RUN_MS).toISOString() : null,
      updated_at: new Date().toISOString(),
    };
    shared.runs.set(id, run);
    return run;
  };
  const exploration = (id: string) => {
    const rec = state.runs.get(id)!;
    const done = isDone(rec.started);
    return {
      run: runOf(id, rec.started),
      done,
      target: "dataset",
      ref: rec.index.ref,
      index: done ? rec.index : null,
      views: done ? [...rec.views].map(([name, k]) => linkOf(k, name)).filter((l) => l !== null) : [],
    };
  };
  /** The run's index (the asked sections) and the artifact key of each of its views and plots. */
  const plan = (d: SciDataset, sections: string[] | undefined, node: string | null): { index: Index; views: Map<string, string> } => {
    const views = new Map<string, string>();
    const runIndex = d.explore.artifacts?.["ui/explore/index.json"] as Index | undefined;
    const nodeName = nodeIndexName(d, node);
    const base = (runIndex ?? (nodeName ? d.artifacts[nodeName] : d.artifacts["ui/explore/index.json"])) as Index;
    const wanted = sections?.length ? new Set(sections) : null;
    const refs = base.sections.filter((s) => !wanted || wanted.has(s.section));
    for (const ref of refs) {
      if ((ref.status ?? "ok") !== "ok") continue;
      for (const name of [ref.view, ref.plots].filter((n): n is string => !!n)) {
        // a view the run computed as the ingest did is served from the ingest copy (same name)
        if (runIndex && shared.artifacts.has(key(d, name, "run"))) views.set(name, key(d, name, "run"));
        else if (shared.artifacts.has(key(d, name))) views.set(name, key(d, name));
      }
    }
    return { index: { ...base, sections: refs }, views };
  };

  return [
    http.get(`${API}/v1/datasets/:id`, ({ params }) => {
      const d = byId.get(String(params.id));
      return d ? HttpResponse.json(d.dataset) : undefined;
    }),
    http.get(`${API}/v1/datasets/:id/versions`, ({ params }) => {
      const d = byId.get(String(params.id));
      return d ? page([d.version]) : undefined;
    }),
    http.get(`${API}/v1/datasets/:id/versions/:v`, ({ params }) => {
      const d = byId.get(String(params.id));
      if (!d) return undefined;
      return params.v === "1" ? HttpResponse.json(d.version) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${API}/v1/datasets/:id/about`, ({ params }) => {
      const d = byId.get(String(params.id));
      return d ? HttpResponse.json(aboutOf(d, state.descriptions.get(d.dataset.id))) : undefined;
    }),
    http.patch(`${API}/v1/datasets/:id/about`, async ({ params, request }) => {
      const d = byId.get(String(params.id));
      if (!d) return undefined;
      const body = (await request.json()) as { description_user?: string | null };
      state.descriptions.set(d.dataset.id, body.description_user ?? null);
      return HttpResponse.json(aboutOf(d, body.description_user ?? null));
    }),
    http.get(new RegExp(`^${API_RE}/v1/datasets/ds-sci-[^/]+/versions/\\d+/artifacts/(.+)$`), ({ request }) => {
      const path = new URL(request.url).pathname;
      const d = byId.get(path.split("/")[3] ?? "");
      if (!d) return undefined;
      const name = decodeURIComponent(path.split("/artifacts/")[1] ?? "");
      const l = linkOf(key(d, name), name);
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found", `no artifact ${name}`);
    }),
    http.post(`${API}/v1/datasets/:id/versions/:v/explore`, async ({ params, request }) => {
      const d = byId.get(String(params.id));
      if (!d) return undefined;
      const body = (await request.json()) as { sections?: string[]; node?: string | null };
      const cacheKey = `${d.dataset.id}:${JSON.stringify(body)}`;
      const prior = state.cache.get(cacheKey);
      if (prior && state.runs.has(prior)) return HttpResponse.json(exploration(prior), { status: 200 });
      const id = `run_01jaf3sci${d.case.padEnd(7, "0")}${String(++state.seq).padStart(8, "0")}`;
      const { index, views } = plan(d, body.sections, body.node ?? null);
      state.runs.set(id, { id, started: Date.now(), ds: d, index, views, node: body.node ?? null });
      state.cache.set(cacheKey, id);
      return HttpResponse.json(exploration(id), { status: 202 });
    }),
    http.get(`${API}/v1/explorations/:id`, ({ params }) => (state.runs.has(String(params.id)) ? HttpResponse.json(exploration(String(params.id))) : undefined)),
  ];
}
