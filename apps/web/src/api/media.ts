/**
 * v0.14 run media: images (PNG, JPEG) and tables a run logged (`ctx.log_image`, `log_figure`,
 * `log_table`, or `POST /v1/runs/{id}/media`). The constants mirror `mapi_spec.media` (I-1).
 *
 * Bytes come only through the signed GET link of the stored copy, checked for size, CRC32C
 * and SHA-256 like other artifacts. An image becomes a `Blob` of its own image type only when
 * its first bytes are a PNG or JPEG signature (the page shows it in an `<img>` through an
 * object URL, which the CSP allows as `blob:`). A table is JSON text: every cell is untrusted
 * text and the page renders it as text only.
 */
import { ArtifactError, fetchArtifactBytes } from "@/lib/artifacts";

import { api, unwrap } from "./client";
import type { MediaContentType, MediaKind, Page, RunMedia } from "./types";

export const MEDIA_EXT: Readonly<Record<MediaContentType, string>> = { "image/png": ".png", "image/jpeg": ".jpg", "application/json": ".json" };
export const MEDIA_KINDS: Readonly<Record<MediaContentType, MediaKind>> = { "image/png": "image", "image/jpeg": "image", "application/json": "table" };
export const MEDIA_NAME_RE = /^[0-9a-f]{64}\.(png|jpg|json)$/;
export const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
export const MAX_TABLE_BYTES = 1024 * 1024;
export const MAX_API_MEDIA_BYTES = 4 * 1024 * 1024;
export const MAX_IMAGE_SIDE = 4096;
export const MAX_MEDIA_CAPTION_CHARS = 500;
export const MAX_TABLE_ROWS = 10_000;
export const MAX_TABLE_COLUMNS = 64;
export const MAX_TABLE_CELLS = 200_000;
export const MAX_TABLE_CELL_CHARS = 1000;
/** Pages of `GET /v1/runs/{id}/media` read per key (200 items each). */
export const MAX_MEDIA_PAGES = 5;

export type MediaCell = string | number | boolean | null;

/** The `application/json` body of a table: column names and row-major cells. */
export interface MediaTable {
  columns: string[];
  rows: MediaCell[][];
}

export class MediaError extends Error {
  override name = "MediaError";
}

interface Ctx {
  headers: Record<string, string>;
  signal?: AbortSignal | undefined;
}

// ------------------------------------------------------------------------- fetchers

export interface MediaListQuery {
  key?: string | undefined;
  kind?: MediaKind | undefined;
  attempt?: number | undefined;
}

/** Media items of a run in key, labels, step order (with signed links), up to 1000. */
export async function fetchRunMedia(runId: string, q: MediaListQuery, ctx: Ctx): Promise<{ items: RunMedia[]; more: boolean }> {
  const items: RunMedia[] = [];
  let cursor: string | null = null;
  for (let i = 0; i < MAX_MEDIA_PAGES; i++) {
    const page: Page<RunMedia> = await unwrap(
      api.GET("/v1/runs/{run_id}/media", {
        params: {
          path: { run_id: runId },
          query: { limit: 200, ...(q.key ? { key: q.key } : {}), ...(q.kind ? { kind: q.kind } : {}), ...(q.attempt ? { attempt: q.attempt } : {}), ...(cursor ? { cursor } : {}) },
        },
        headers: ctx.headers,
        signal: ctx.signal,
      }),
    );
    items.push(...page.items);
    cursor = page.next_cursor ?? null;
    if (!cursor) return { items, more: false };
  }
  return { items, more: true };
}

/** One media series per (key, labels) of a run. */
export function fetchRunMediaKeys(runId: string, ctx: Ctx) {
  return unwrap(api.GET("/v1/runs/{run_id}/media-keys", { params: { path: { run_id: runId }, query: { limit: 200 } }, headers: ctx.headers, signal: ctx.signal }));
}

/** One item, always with a fresh signed link. */
export function fetchRunMediaItem(runId: string, mediaId: string, ctx: Ctx) {
  return unwrap(api.GET("/v1/runs/{run_id}/media/{media_id}", { params: { path: { run_id: runId, media_id: mediaId } }, headers: ctx.headers, signal: ctx.signal }));
}

// ------------------------------------------------------------------------- bytes

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

/** The image type the first bytes show (`null` for anything that is not PNG or JPEG). */
export function sniffImageType(bytes: Uint8Array): "image/png" | "image/jpeg" | null {
  if (bytes.length >= 8 && PNG_SIGNATURE.every((b, i) => bytes[i] === b)) return "image/png";
  if (bytes.length >= 3 && bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  return null;
}

async function sha256Hex(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as Uint8Array<ArrayBuffer>);
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** A name for messages: `samples step 3`. */
export function mediaLabel(m: Pick<RunMedia, "key" | "step">): string {
  return m.step === null || m.step === undefined ? m.key : `${m.key} step ${m.step}`;
}

/** Is the signed link missing or about to expire (within `marginMs`)? */
export function linkExpired(m: Pick<RunMedia, "url" | "expires_at">, now = Date.now(), marginMs = 30_000): boolean {
  if (!m.url) return true;
  if (!m.expires_at) return false;
  const t = Date.parse(m.expires_at);
  return Number.isFinite(t) && t - marginMs <= now;
}

/**
 * Download one item through its signed link and check it (size, CRC32C, SHA-256). An image is
 * returned as a `Blob` of its image type only if its bytes are that image type; a table as an
 * `application/json` Blob.
 */
export async function fetchMediaBlob(media: RunMedia, signal?: AbortSignal, fetchImpl?: typeof fetch): Promise<Blob> {
  if (!media.url) throw new MediaError(`${mediaLabel(media)} has no download link`);
  const max = media.kind === "table" ? MAX_TABLE_BYTES : MAX_IMAGE_BYTES;
  let bytes: Uint8Array;
  try {
    bytes = await fetchArtifactBytes(
      { url: media.url, size: media.size, crc32c: media.crc32c, name: mediaLabel(media) },
      { maxBytes: max, ...(signal ? { signal } : {}), ...(fetchImpl ? { fetchImpl } : {}) },
    );
  } catch (err) {
    throw err instanceof ArtifactError ? new MediaError(err.message) : err;
  }
  const want = media.sha256.replace(/^sha256:/, "");
  if ((await sha256Hex(bytes)) !== want) throw new MediaError(`${mediaLabel(media)}: the SHA-256 does not agree with the server`);
  if (media.kind === "table") return new Blob([bytes as Uint8Array<ArrayBuffer>], { type: "application/json" });
  const type = sniffImageType(bytes);
  if (!type || type !== media.content_type) throw new MediaError(`${mediaLabel(media)} is not a ${media.content_type === "image/png" ? "PNG" : "JPEG"} image`);
  return new Blob([bytes as Uint8Array<ArrayBuffer>], { type });
}

const isCell = (v: unknown): v is MediaCell =>
  v === null || typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v)) || (typeof v === "string" && v.length <= MAX_TABLE_CELL_CHARS);

/** Parse and check the bytes of a table (`MediaTable` of I-1). Cells stay untrusted text. */
export function parseMediaTable(bytes: Uint8Array): MediaTable {
  if (bytes.length > MAX_TABLE_BYTES) throw new MediaError("The table is too large to show.");
  let data: unknown;
  try {
    data = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
  } catch {
    throw new MediaError("The table is not UTF-8 JSON.");
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) throw new MediaError("The table is not a JSON object.");
  const { columns, rows = [] } = data as { columns?: unknown; rows?: unknown };
  if (!Array.isArray(columns) || columns.length < 1 || columns.length > MAX_TABLE_COLUMNS) {
    throw new MediaError(`The table must have 1 to ${MAX_TABLE_COLUMNS} columns.`);
  }
  if (!columns.every((c): c is string => typeof c === "string" && c.length >= 1 && c.length <= 128)) {
    throw new MediaError("Each column name must be text of 1 to 128 characters.");
  }
  if (!Array.isArray(rows) || rows.length > MAX_TABLE_ROWS) throw new MediaError(`The table must have ${MAX_TABLE_ROWS.toLocaleString("en")} rows or fewer.`);
  if (rows.length * columns.length > MAX_TABLE_CELLS) throw new MediaError(`The table must have ${MAX_TABLE_CELLS.toLocaleString("en")} cells or fewer.`);
  rows.forEach((row: unknown, i) => {
    if (!Array.isArray(row) || row.length !== columns.length) throw new MediaError(`Row ${i + 1} must have ${columns.length} cells.`);
    if (!row.every(isCell)) throw new MediaError(`Row ${i + 1} has a cell that is not text, a number, true, false or empty.`);
  });
  return { columns, rows: rows as MediaCell[][] };
}

/** The text of a table cell ("" for an empty cell). */
export function cellText(v: MediaCell): string {
  if (v === null) return "";
  return typeof v === "string" ? v : String(v);
}
