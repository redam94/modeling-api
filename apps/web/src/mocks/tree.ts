/**
 * M4 mock API: a tree dataset (`lens_cubes` v1, computed by the real runner in
 * `scripts/gen_tree_fixtures.py`), upload inspection (`POST …/uploads/{id}/inspect`, then
 * `GET …/inspection`: `409 NOT_READY` on the first poll) and the uploads a multi-file
 * version reads. An upload named `*unsafe*` fails its inspect run like the HDF5 guard does.
 */
import { http, HttpResponse, type HttpHandler } from "msw";
import { parse } from "yaml";

import type { Dataset, DatasetVersion, Issue, Problem, Run, UploadInspection } from "@/api/types";
import { env } from "@/env";

import * as fx from "./fixtures";
import treeGen from "./tree.gen.json";

const API = env.apiBase;
const API_RE = API.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const enc = new TextEncoder();

export const TREE = treeGen as unknown as {
  dataset: Dataset;
  version: DatasetVersion;
  artifacts: Record<string, unknown>;
  inspection: UploadInspection;
  hdf5_mapping: Record<string, unknown>;
};
export const TREE_DS_ID = TREE.dataset.id;

export interface UploadRec {
  filename: string;
  /** Bytes received at the signed URL (`null`: not uploaded yet). */
  bytes: number | null;
  inspectRunId: string | null;
  polls: number;
}

export interface TreeMockState {
  uploads: Map<string, UploadRec>;
  seq: number;
}

export function createTreeState(): TreeMockState {
  return { uploads: new Map(), seq: 0 };
}

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

function formatOf(filename: string): UploadInspection["format"] {
  const n = filename.toLowerCase();
  if (n.endsWith(".nc") || n.endsWith(".nc4")) return "netcdf";
  if (n.endsWith(".zip")) return "zarr_zip";
  return "hdf5";
}

const UNSAFE: Problem = {
  type: "urn:mapi:error:INGEST_UNSAFE_FILE",
  title: "The upload is not safe to read",
  status: 422,
  code: "INGEST_UNSAFE_FILE",
  detail: "the HDF5 file links to another file (external link /data -> other.h5)",
  errors: [],
};

export function createTreeHandlers(
  state: TreeMockState,
  shared: { artifacts: Map<string, { bytes: Uint8Array; type: string }>; runs: Map<string, Run> },
): HttpHandler[] {
  const key = (name: string) => `tree:${name}`;
  for (const [name, value] of Object.entries(TREE.artifacts)) {
    if (!shared.artifacts.has(key(name))) shared.artifacts.set(key(name), { bytes: enc.encode(JSON.stringify(value)), type: "application/json" });
  }
  const treeLink = (name: string) => {
    const a = shared.artifacts.get(key(name));
    return a ? { ...fx.artifactLink(key(name), a.bytes, a.type), name } : null;
  };
  const inspectRun = (id: string, rec: UploadRec, failed: boolean): Run => ({
    ...fx.runDone,
    id,
    kind: "inspect",
    spec_id: null,
    spec_revision: null,
    model_version_id: null,
    state: failed ? "failed" : rec.polls > 0 ? "succeeded" : "running",
    problem: failed ? UNSAFE : null,
    progress: null,
  });

  return [
    // The tree dataset (before the generic dataset routes).
    http.get(`${API}/v1/datasets/${TREE_DS_ID}`, () => HttpResponse.json(TREE.dataset)),
    http.get(`${API}/v1/datasets/${TREE_DS_ID}/versions`, () => page([TREE.version])),
    http.get(`${API}/v1/datasets/${TREE_DS_ID}/versions/:v`, ({ params }) =>
      params.v === "1" ? HttpResponse.json(TREE.version) : problem(404, "NOT_FOUND", "Not found"),
    ),
    http.get(new RegExp(`^${API_RE}/v1/datasets/${TREE_DS_ID}/versions/1/artifacts/(.+)$`), ({ request }) => {
      const name = decodeURIComponent(new URL(request.url).pathname.split("/artifacts/")[1] ?? "");
      const l = treeLink(name);
      return l ? HttpResponse.json(l) : problem(404, "NOT_FOUND", "Not found", `no artifact ${name}`);
    }),

    // Inspection of an upload (any dataset).
    http.post(`${API}/v1/datasets/:id/uploads/:uid/inspect`, ({ params }) => {
      const rec = state.uploads.get(String(params.uid));
      if (!rec) return problem(404, "NOT_FOUND", "Not found", "no such upload");
      if (rec.bytes === null) return problem(409, "CONFLICT", "Conflict", "the upload has not arrived");
      const failed = rec.filename.includes("unsafe");
      if (!rec.inspectRunId || failed) {
        state.seq += 1;
        rec.inspectRunId = `run_01jaf3q2x8m9v7k5h4insp${String(state.seq).padStart(4, "0")}`;
        rec.polls = 0;
      }
      const run = inspectRun(rec.inspectRunId, rec, false);
      shared.runs.set(run.id, run);
      return HttpResponse.json(run, { status: 202 });
    }),
    http.get(`${API}/v1/datasets/:id/uploads/:uid/inspection`, ({ params }) => {
      const rec = state.uploads.get(String(params.uid));
      if (!rec?.inspectRunId) return problem(404, "NOT_FOUND", "Not found", "no inspection was requested");
      const failed = rec.filename.includes("unsafe");
      if (rec.polls === 0) {
        rec.polls += 1;
        return problem(409, "NOT_READY", "Not ready", `the inspection of upload ${String(params.uid)} is still running`);
      }
      const run = inspectRun(rec.inspectRunId, rec, failed);
      shared.runs.set(run.id, run);
      if (failed) return problem(404, "NOT_FOUND", "Not found", `upload inspection ${String(params.uid)} (inspect run ${run.id} ended failed)`);
      return HttpResponse.json({ ...TREE.inspection, format: formatOf(rec.filename), size_bytes: rec.bytes ?? 0 });
    }),
  ];
}

/**
 * The static checks of `data.nodes` (`mapi_spec.validate` with the version's tree schema), for
 * the mock's `POST /v1/specs:validate`: unknown nodes and variables, and nodes on a dataset
 * that is not a tree.
 */
export function treeSpecIssues(yaml: string): Issue[] {
  let doc: unknown;
  try {
    doc = parse(yaml);
  } catch {
    return [];
  }
  const data = (doc as { data?: { dataset?: unknown; nodes?: unknown } } | null)?.data;
  if (!data || typeof data.nodes !== "object" || data.nodes === null) return [];
  const isTreeRef = typeof data.dataset === "string" && data.dataset.startsWith(TREE.dataset.ref);
  if (!isTreeRef) {
    return [{ code: "SCHEMA_INVALID", severity: "error", pointer: "/data/nodes", message: "data.nodes is only used with a tree dataset; this dataset version is a plain dataset" }];
  }
  const nodes = TREE.version.tree_schema?.nodes ?? {};
  const issues: Issue[] = [];
  for (const [key, sel] of Object.entries(data.nodes as Record<string, { path?: unknown; vars?: unknown }>)) {
    const path = typeof sel?.path === "string" ? sel.path : "";
    const node = nodes[path];
    if (!node) {
      issues.push({ code: "SCHEMA_INVALID", severity: "error", pointer: `/data/nodes/${key}/path`, message: `the dataset has no node '${path}' (nodes: ${Object.keys(nodes).join(", ")})` });
      continue;
    }
    for (const v of Array.isArray(sel.vars) ? sel.vars : []) {
      if (typeof v === "string" && !(v in (node.vars ?? {}))) {
        issues.push({ code: "SCHEMA_INVALID", severity: "error", pointer: `/data/nodes/${key}/vars`, message: `node '${path}' has no variable '${v}'` });
      }
    }
  }
  return issues;
}
