/**
 * v0.15 image collections in the mock API: the dataset `shapes_photos` with one ready
 * collection version (60 images of 3 classes), its own upload sessions (so `GET …/uploads/{id}`
 * can say whether the bytes arrived and `POST …/renew` can sign a fresh URL),
 * `POST …/blobs/missing` against the latest version's images, and version create with
 * `collection` (the mock reads the manifest CSV it received to count classes and splits).
 * Semantics follow I-4 closely enough for the upload wizard and the version page; it is not a
 * server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import type { CollectionInfo, CollectionSource, Dataset, DatasetVersion, Problem, UploadSession } from "@/api/types";
import { env } from "@/env";
import { parseCsv } from "@/lib/csv";

import * as fx from "./fixtures";

const API = env.apiBase;
const STORE = "http://127.0.0.1:4443/mock-collection-upload";

export const COLLECTION_DS_ID = "ds_01jaf3a1b2c3d4e5f6g7h8jc01";
const NAME = "shapes_photos";
/** The images of version 1 (fake digests: an upload of new files never matches them). */
export const V1_MEMBERS = Array.from({ length: 60 }, (_, i) => (i + 1).toString(16).padStart(64, "a"));

const base = { tenant_id: "default", project_id: "default" } as const;

export const collectionDataset: Dataset = {
  ...base,
  id: COLLECTION_DS_ID,
  name: NAME,
  ref: `dataset://default/${NAME}`,
  description: "Photos of three shapes for the image classifiers",
  latest_version: 1,
  created_at: fx.T0,
  updated_at: fx.T0,
};

function info(patch: Partial<CollectionInfo>): CollectionInfo {
  return {
    format: "mapi.collection/v1",
    n_images: 60,
    dim: "image",
    var: "pixels",
    label: true,
    split: true,
    classes: [
      { label: "circle", count: 20 },
      { label: "cross", count: 20 },
      { label: "square", count: 20 },
    ],
    other_classes: 0,
    splits: { test: 15, train: 45 },
    mode: "rgb",
    size: [16, 16],
    min_size: [16, 16],
    max_size: [16, 16],
    formats: { png: 60 },
    total_bytes: 60 * 812,
    packs: [{ digest: "c".repeat(64), size: 60 * 900, crc32c: "AAAAAA==", md5: "1B2M2Y8AsgTpgAmY7PhCfg==", entries: 60, images: 60, source: "upload", input: "part.0000" }],
    new_images: 60,
    reused_images: 0,
    base_version: null,
    columns: [],
    manifest_sha256: "d".repeat(64),
    ...patch,
  };
}

function collectionVersion(version: number, c: CollectionInfo, message: string | null): DatasetVersion {
  return {
    ...base,
    dataset_id: COLLECTION_DS_ID,
    version,
    ref: `dataset://default/${NAME}@v${version}`,
    status: "ready",
    sha256: `sha256:${c.manifest_sha256}`,
    size_bytes: c.total_bytes,
    layout: "collection",
    collection: c,
    data_schema: {
      layout: "dense",
      dims: {
        image: { kind: "categorical", policy: "new_levels" },
        y: { kind: "integer", policy: "fixed" },
        x: { kind: "integer", policy: "fixed" },
        channel: { kind: "categorical", policy: "fixed" },
      },
      vars: {
        pixels: { dims: ["image", "y", "x", "channel"], dtype: "uint8", role: "covariate" },
        ...(c.label ? { label: { dims: ["image"], dtype: "str", role: "target", missing: "allow" } } : {}),
      },
    },
    summary: {
      vars: ["pixels", ...(c.label ? ["label"] : [])],
      coords: {
        image: { kind: "categorical", size: c.n_images },
        ...(c.split ? { split: { kind: "categorical", size: Object.keys(c.splits ?? {}).length, values: Object.keys(c.splits ?? {}) } } : {}),
      },
    },
    message,
    ingest_run_id: fx.RUN_DONE,
    issues: [],
    created_at: fx.T0,
    ready_at: fx.T0,
  };
}

interface UploadRec {
  session: UploadSession;
  bytes: Uint8Array | null;
  consumed: boolean;
}

export interface CollectionMockState {
  versions: DatasetVersion[];
  /** Image digests of each ready version. */
  members: Map<number, Set<string>>;
  uploads: Map<string, UploadRec>;
  seq: number;
}

export function createCollectionState(): CollectionMockState {
  return {
    versions: [collectionVersion(1, info({}), "First photos")],
    members: new Map([[1, new Set(V1_MEMBERS)]]),
    uploads: new Map(),
    seq: 0,
  };
}

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

const page = <T,>(items: T[]) => HttpResponse.json({ items, next_cursor: null });

/** The upload session with `received`, and a URL that is valid for one hour from now. */
function sessionOf(rec: UploadRec): UploadSession & { received: boolean } {
  return { ...rec.session, received: rec.bytes !== null, status: rec.bytes !== null ? "uploaded" : rec.session.status };
}

function manifestCounts(bytes: Uint8Array | null): { rows: number; classes: Map<string, number>; splits: Map<string, number> } {
  const csv = parseCsv(new TextDecoder().decode(bytes ?? new Uint8Array()), 1_000_000);
  const labelAt = csv.header.indexOf("label");
  const splitAt = csv.header.indexOf("split");
  const classes = new Map<string, number>();
  const splits = new Map<string, number>();
  const rows = csv.rows.filter((r) => r.some((v) => v !== ""));
  for (const r of rows) {
    const label = labelAt >= 0 ? (r[labelAt] ?? "") : "";
    if (label) classes.set(label, (classes.get(label) ?? 0) + 1);
    const split = splitAt >= 0 ? (r[splitAt] ?? "") : "";
    if (split) splits.set(split, (splits.get(split) ?? 0) + 1);
  }
  return { rows: rows.length, classes, splits };
}

export function createCollectionHandlers(state: CollectionMockState): HttpHandler[] {
  const DS = `${API}/v1/datasets/${COLLECTION_DS_ID}`;
  const latest = () => [...state.versions].filter((v) => v.status === "ready").sort((a, b) => b.version - a.version)[0] ?? null;
  return [
    http.get(DS, () => HttpResponse.json({ ...collectionDataset, latest_version: state.versions.length })),
    http.get(`${DS}/versions`, () => page([...state.versions].reverse())),
    http.get(`${DS}/versions/:v`, ({ params }) => {
      const v = state.versions.find((x) => x.version === Number(params.v));
      return v ? HttpResponse.json(v) : problem(404, "NOT_FOUND", "Not found");
    }),
    http.get(`${DS}/about`, () =>
      HttpResponse.json({ description_user: null, highlights: [], summary_generated: "An image collection of photos of three shapes.", warnings_plain: [], generated_at: fx.T0 }),
    ),
    http.post(`${DS}/uploads`, async ({ request }) => {
      const body = (await request.json()) as { filename: string; content_type: string; size_bytes: number };
      const id = `upl_01jaf3d1e2f3g4h5j6k7m8nc${String(++state.seq).padStart(2, "0")}`;
      const expires = new Date(Date.now() + 3600_000).toISOString();
      const session: UploadSession = {
        ...base,
        id,
        dataset_id: COLLECTION_DS_ID,
        filename: body.filename,
        content_type: body.content_type,
        size_bytes: body.size_bytes,
        status: "pending",
        upload: { method: "PUT", url: `${STORE}/${id}?X-Goog-Signature=mock`, name: `t/default/uploads/${id}`, expires_at: expires, headers: { "content-type": body.content_type, "x-goog-if-generation-match": "0" }, max_bytes: body.size_bytes },
        expires_at: expires,
        created_at: new Date().toISOString(),
      };
      state.uploads.set(id, { session, bytes: null, consumed: false });
      return HttpResponse.json(session, { status: 201 });
    }),
    http.put(`${STORE}/:id`, async ({ request, params }) => {
      const rec = state.uploads.get(String(params.id));
      if (!rec) return new HttpResponse(null, { status: 404 });
      if (Date.parse(rec.session.upload.expires_at) <= Date.now()) return new HttpResponse(null, { status: 403 });
      if (rec.bytes !== null || request.headers.get("x-goog-if-generation-match") !== "0") return new HttpResponse(null, { status: 412 });
      rec.bytes = new Uint8Array(await request.arrayBuffer());
      return HttpResponse.json({ generation: "1" });
    }),
    http.get(`${DS}/uploads/:uid`, ({ params }) => {
      const rec = state.uploads.get(String(params.uid));
      return rec ? HttpResponse.json(sessionOf(rec)) : problem(404, "NOT_FOUND", "Not found", "no such upload");
    }),
    http.post(`${DS}/uploads/:uid/renew`, ({ params }) => {
      const rec = state.uploads.get(String(params.uid));
      if (!rec) return problem(404, "NOT_FOUND", "Not found", "no such upload");
      if (rec.consumed || rec.bytes !== null) return problem(409, "CONFLICT", "Conflict", "the file of this upload already arrived");
      const expires = new Date(Date.now() + 3600_000).toISOString();
      rec.session = { ...rec.session, status: "pending", expires_at: expires, upload: { ...rec.session.upload, expires_at: expires } };
      return HttpResponse.json({ ...sessionOf(rec), received: false });
    }),
    http.post(`${DS}/blobs/missing`, async ({ request }) => {
      const body = (await request.json()) as { sha256?: string[]; base_version?: number | null };
      const shas = body.sha256 ?? [];
      if (!shas.length || shas.length > 5000 || new Set(shas).size !== shas.length || shas.some((s) => !/^[0-9a-f]{64}$/.test(s))) {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "sha256 must list 1 to 5000 unique digests");
      }
      if (body.base_version === 0) return HttpResponse.json({ base_version: null, missing: shas, present: 0 });
      const baseVersion = body.base_version ?? latest()?.version ?? null;
      if (baseVersion !== null && !state.members.has(baseVersion)) return problem(404, "NOT_FOUND", "Not found", "no such version");
      const members = baseVersion !== null ? state.members.get(baseVersion)! : new Set<string>();
      const missing = shas.filter((s) => !members.has(s));
      return HttpResponse.json({ base_version: baseVersion, missing, present: shas.length - missing.length });
    }),
    http.post(`${DS}/versions`, async ({ request }) => {
      const body = (await request.json()) as { collection?: CollectionSource; message?: string };
      const c = body.collection;
      if (!c) return problem(422, "SCHEMA_INVALID", "Invalid request", "this dataset takes image collections only (give collection)");
      const partIds = c.part_upload_ids ?? [];
      const options = c.options ?? {};
      const ids = [c.manifest_upload_id, ...partIds];
      if (new Set(ids).size !== ids.length) return problem(422, "SCHEMA_INVALID", "Invalid request", "an upload may be used once");
      const recs = ids.map((id) => state.uploads.get(id));
      if (recs.some((r) => !r || r.bytes === null || r.consumed)) return problem(409, "CONFLICT", "Conflict", "an upload is not finished");
      const baseVersion = c.base_version === 0 ? null : (c.base_version ?? latest()?.version ?? null);
      const counts = manifestCounts(recs[0]!.bytes);
      const partBytes = recs.slice(1).reduce((n, r) => n + (r!.bytes?.length ?? 0), 0);
      for (const r of recs) r!.consumed = true;
      const version = state.versions.length + 1;
      const shas = new Set<string>();
      const csv = parseCsv(new TextDecoder().decode(recs[0]!.bytes ?? new Uint8Array()), 1_000_000);
      const shaAt = csv.header.indexOf("sha256");
      for (const r of csv.rows) if (shaAt >= 0 && r[shaAt]) shas.add(r[shaAt]);
      const reused = baseVersion !== null ? [...shas].filter((s) => state.members.get(baseVersion)?.has(s)).length : 0;
      const classes = [...counts.classes.entries()].sort(([a, x], [b, y]) => y - x || a.localeCompare(b)).map(([label, count]) => ({ label, count }));
      const size = options.size ?? null;
      const ci = info({
        n_images: counts.rows,
        label: classes.length > 0,
        split: counts.splits.size > 0,
        classes,
        splits: Object.fromEntries(counts.splits),
        mode: options.mode === "gray" ? "gray" : "rgb",
        size: size ?? [16, 16],
        formats: { png: counts.rows },
        total_bytes: partBytes,
        packs: partIds.map((_, i) => ({ digest: (version * 100 + i).toString(16).padStart(64, "e"), size: recs[i + 1]!.bytes!.length, crc32c: "AAAAAA==", md5: "1B2M2Y8AsgTpgAmY7PhCfg==", entries: 1, images: 1, source: "upload" as const, input: `part.${String(i).padStart(4, "0")}` })),
        new_images: shas.size - reused,
        reused_images: reused,
        base_version: baseVersion,
        manifest_sha256: version.toString(16).padStart(64, "d"),
      });
      if (!ci.packs.length) ci.packs = [{ ...info({}).packs[0]!, source: "base", input: null }];
      const v = collectionVersion(version, ci, body.message ?? null);
      state.versions.push(v);
      state.members.set(version, new Set([...shas, ...(baseVersion !== null ? (state.members.get(baseVersion) ?? []) : [])]));
      return HttpResponse.json(v, { status: 202 });
    }),
  ];
}
