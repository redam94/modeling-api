/**
 * MSW handlers for v0.14 run media (`GET/POST /v1/runs/{id}/media`, `media-keys`, one item)
 * over images built when the state is created (tiny RGB PNGs, a different pattern per step)
 * and one table. Bytes are served by the signed-URL route of the GCS shim mock
 * (`/mock-artifacts/...`, see `handlers.ts`). Semantics follow I-3 closely enough for the
 * Media tab and the compare page; it is not a server.
 */
import { http, HttpResponse, type HttpHandler } from "msw";

import { MAX_API_MEDIA_BYTES, MEDIA_EXT, MEDIA_KINDS } from "@/api/media";
import type { MediaContentType, MediaKeySummary, MediaKind, Problem, Run, RunMedia } from "@/api/types";
import { env } from "@/env";
import { labelsKey, METRIC_KEY_PATTERN } from "@/features/runs/run-query";
import { crc32cBase64 } from "@/lib/crc32c";

import { RUN_FIT_A, RUN_FIT_B } from "./fixtures";
import { encodeRgbPng, sha256Hex } from "./png";

const API = env.apiBase;
const enc = new TextEncoder();
const METRIC_KEY = new RegExp(METRIC_KEY_PATTERN);

export interface MockMediaItem {
  item: Omit<RunMedia, "url" | "expires_at">;
  /** The artifact name of its bytes in the mock store. */
  name: string;
}

export type MediaState = Map<string, MockMediaItem[]>;

type Artifacts = Map<string, { bytes: Uint8Array; type: string }>;

function problem(status: number, code: Problem["code"], title: string, detail?: string) {
  return HttpResponse.json(
    { type: `urn:mapi:error:${code}`, title, status, code, detail: detail ?? null, errors: [] } satisfies Problem,
    { status, headers: { "content-type": "application/problem+json" } },
  );
}

/** A 16 x 16 image of a run at a step: a disc that grows with the step, in the run's colour. */
export function samplePng(step: number, hue: [number, number, number], size = 16): Uint8Array {
  const rgb = new Uint8Array(size * size * 3);
  const r = 2 + step * 1.4;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const inside = (x - size / 2 + 0.5) ** 2 + (y - size / 2 + 0.5) ** 2 <= r * r;
      const o = (y * size + x) * 3;
      rgb[o] = inside ? hue[0] : 240 - 4 * y;
      rgb[o + 1] = inside ? hue[1] : 240 - 4 * x;
      rgb[o + 2] = inside ? hue[2] : 236;
    }
  }
  return encodeRgbPng(size, size, rgb);
}

let mediaSeq = 0;

export function addItem(
  state: MediaState,
  artifacts: Artifacts,
  runId: string,
  m: { key: string; kind: MediaKind; contentType: MediaContentType; bytes: Uint8Array; step: number | null; labels?: Record<string, string>; caption?: string | null; width?: number; height?: number; source?: "runner" | "api"; at: string },
): RunMedia {
  const sha = sha256Hex(m.bytes);
  const name = `run-media/${runId}/${sha}${MEDIA_EXT[m.contentType]}`;
  artifacts.set(name, { bytes: m.bytes, type: m.contentType });
  const item: MockMediaItem["item"] = {
    id: `rm_${runId.slice(-6)}${String(++mediaSeq).padStart(20, "0")}`,
    run_id: runId,
    attempt: 1,
    key: m.key,
    labels: m.labels ?? {},
    step: m.step,
    kind: m.kind,
    content_type: m.contentType,
    size: m.bytes.length,
    sha256: `sha256:${sha}`,
    crc32c: crc32cBase64(m.bytes),
    width: m.width ?? null,
    height: m.height ?? null,
    caption: m.caption ?? null,
    source: m.source ?? "runner",
    created_at: m.at,
  };
  const list = state.get(runId) ?? [];
  // same (key, labels, step): the newest item wins, like the server's upsert
  const i = list.findIndex((x) => x.item.key === m.key && labelsKey(x.item.labels ?? {}) === labelsKey(m.labels ?? {}) && x.item.step === m.step);
  if (i >= 0) list.splice(i, 1);
  list.push({ item, name });
  state.set(runId, list);
  return item;
}

/** Media of the two study fits: `samples` images at steps 0..4 and a `coefficients` table. */
export function createMediaState(artifacts: Artifacts): MediaState {
  const state: MediaState = new Map();
  const runs: [string, [number, number, number], string][] = [
    [RUN_FIT_A, [91, 75, 219], "2026-10-14T14:01:00Z"],
    [RUN_FIT_B, [31, 138, 112], "2026-10-14T15:01:00Z"],
  ];
  runs.forEach(([runId, hue, t0], r) => {
    for (let step = 0; step <= 4; step++) {
      // the second run logs no image at step 1 (the grid says so)
      if (r === 1 && step === 1) continue;
      addItem(state, artifacts, runId, {
        key: "samples",
        kind: "image",
        contentType: "image/png",
        bytes: samplePng(step, hue),
        step,
        caption: step === 4 ? "Posterior sample <b>not bold</b>" : `Posterior sample at step ${step}`,
        width: 16,
        height: 16,
        at: new Date(Date.parse(t0) + step * 60_000).toISOString(),
      });
    }
    const table = {
      columns: ["channel", "coefficient", "note"],
      rows: [
        ["tv", r === 0 ? 0.42 : 0.39, "<script>alert(1)</script>"],
        ["search", r === 0 ? 0.31 : 0.35, null],
        ["social", 0.12, "small"],
      ],
    };
    addItem(state, artifacts, runId, { key: "coefficients", kind: "table", contentType: "application/json", bytes: enc.encode(JSON.stringify(table)), step: null, caption: "Coefficients of the channels", at: t0 });
  });
  return state;
}

/** A signed link of the stored copy (the mock store's URL). */
function withLink(m: MockMediaItem): RunMedia {
  return { ...m.item, url: `http://127.0.0.1:4443/mock-artifacts/${encodeURIComponent(m.name)}?X-Goog-Signature=mock`, expires_at: "2099-01-01T00:00:00Z" };
}

function order(a: MockMediaItem, b: MockMediaItem): number {
  return a.item.key.localeCompare(b.item.key) || labelsKey(a.item.labels ?? {}).localeCompare(labelsKey(b.item.labels ?? {})) || (a.item.step ?? -1) - (b.item.step ?? -1);
}

/** Up to `max` items in step order, evenly thinned, the last kept (`MAX_COMPARE_MEDIA_ITEMS`). */
export function thinMedia<T>(items: readonly T[], max: number): T[] {
  if (items.length <= max) return [...items];
  return Array.from({ length: max }, (_, i) => items[Math.round((i * (items.length - 1)) / (max - 1))]!);
}

/** The media series of a run for `compare`: per (key, labels) of `keys`, items with links. */
export function compareMedia(state: MediaState, runId: string, keys: readonly string[], maxItems: number) {
  const list = [...(state.get(runId) ?? [])].sort(order).filter((m) => keys.includes(m.item.key));
  const groups = new Map<string, MockMediaItem[]>();
  for (const m of list) {
    const id = `${m.item.key}|${labelsKey(m.item.labels ?? {})}`;
    groups.set(id, [...(groups.get(id) ?? []), m]);
  }
  return [...groups.values()].map((g) => ({
    run_id: runId,
    key: g[0]!.item.key,
    labels: g[0]!.item.labels ?? {},
    kind: g[0]!.item.kind,
    items: thinMedia(g, maxItems).map(withLink),
    truncated: g.length > maxItems,
  }));
}

export function createMediaHandlers(state: MediaState, ctx: { runs: Map<string, Run>; artifacts: Artifacts }): HttpHandler[] {
  return [
    http.get(`${API}/v1/runs/:id/media-keys`, ({ params }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const groups = new Map<string, MockMediaItem[]>();
      for (const m of [...(state.get(id) ?? [])].sort(order)) {
        const k = `${m.item.key}|${labelsKey(m.item.labels ?? {})}`;
        groups.set(k, [...(groups.get(k) ?? []), m]);
      }
      const items: MediaKeySummary[] = [...groups.values()].map((g) => {
        const steps = g.map((m) => m.item.step).filter((s): s is number => s !== null && s !== undefined);
        return {
          key: g[0]!.item.key,
          labels: g[0]!.item.labels ?? {},
          kind: g[0]!.item.kind,
          count: g.length,
          first_step: steps.length ? Math.min(...steps) : null,
          last_step: steps.length ? Math.max(...steps) : null,
          last_created_at: g.map((m) => m.item.created_at).sort().at(-1)!,
        };
      });
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.get(`${API}/v1/runs/:id/media/:mediaId`, ({ params }) => {
      const m = (state.get(String(params.id)) ?? []).find((x) => x.item.id === params.mediaId);
      return m ? HttpResponse.json(withLink(m)) : problem(404, "NOT_FOUND", "Not found", "no such media item");
    }),
    http.get(`${API}/v1/runs/:id/media`, ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const url = new URL(request.url);
      const key = url.searchParams.get("key");
      const kind = url.searchParams.get("kind");
      const links = url.searchParams.get("links") !== "false";
      const items = [...(state.get(id) ?? [])]
        .filter((m) => (!key || m.item.key === key) && (!kind || m.item.kind === kind))
        .sort(order)
        .map((m) => (links ? withLink(m) : { ...m.item }));
      return HttpResponse.json({ items, next_cursor: null });
    }),
    http.post(`${API}/v1/runs/:id/media`, async ({ params, request }) => {
      const id = String(params.id);
      if (!ctx.runs.has(id)) return problem(404, "NOT_FOUND", "Not found", "no such run");
      const body = (await request.json()) as { key?: string; kind?: MediaKind; content_type?: MediaContentType; data?: string; step?: number | null; labels?: Record<string, string>; caption?: string | null };
      if (!body.key || !METRIC_KEY.test(body.key) || !body.content_type || MEDIA_KINDS[body.content_type] !== body.kind || typeof body.data !== "string") {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "the media key, kind or content type is not valid");
      }
      let bytes: Uint8Array;
      try {
        bytes = Uint8Array.from(atob(body.data), (c) => c.charCodeAt(0));
      } catch {
        return problem(422, "SCHEMA_INVALID", "Invalid request", "data is not base64");
      }
      if (bytes.length > MAX_API_MEDIA_BYTES) return problem(413, "PAYLOAD_TOO_LARGE", "Payload too large", "the media item is too large");
      const item = addItem(state, ctx.artifacts, id, {
        key: body.key,
        kind: body.kind,
        contentType: body.content_type,
        bytes,
        step: body.step ?? null,
        labels: body.labels ?? {},
        caption: body.caption ?? null,
        source: "api",
        at: new Date().toISOString(),
      });
      return HttpResponse.json(item, { status: 201 });
    }),
  ];
}
