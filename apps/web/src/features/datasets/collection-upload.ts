/**
 * v0.15 image collections (`layout: "collection"`): the browser side of a folder upload.
 *
 * 1. {@link planCollection} hashes every image of the selected folder (SHA-256 with
 *    `crypto.subtle`, CRC-32 for the zip) and gives each a label: the first folder under the
 *    selected one, or the label column of a manifest file.
 * 2. `POST /v1/datasets/{id}/blobs/missing` tells which images the base version lacks
 *    ({@link findMissing}, chunks of `MAX_BLOB_QUERY`).
 * 3. {@link planParts} packs only those images into parts: deterministic STORE zips
 *    (`zip-store.ts`) of `<sha256>.<ext>` entries sorted by digest, at most
 *    `DEFAULT_PART_BYTES` and `MAX_PART_ENTRIES` each.
 * 4. {@link uploadCollection} uploads the manifest CSV ({@link manifestCsv}) and each part as an
 *    ordinary upload session, with 3 tries per part, a renewed URL after a 403 or an expired
 *    URL, and a resume state in `localStorage` (a part that `GET …/uploads/{id}` says arrived
 *    is not sent again after a reload).
 * 5. The caller creates the version with `collection = {manifest_upload_id, part_upload_ids,
 *    base_version, options}`.
 *
 * The constants mirror `mapi_spec.collection` (I-1); the DTOs are the generated OpenAPI types.
 */
import { api, ApiError, unwrap } from "@/api/client";
import type { BlobMissing, CollectionInfo, CollectionOptions, CollectionSource, DatasetVersion, PackInfo, UploadSession } from "@/api/types";
import { putToSignedUrl, UploadError, type UploadProgress } from "@/api/upload";
import { parseCsv } from "@/lib/csv";

import { blobBytes, buildStoreZip, crc32, storeZipSize, type ZipEntry } from "./zip-store";

export const MAX_COLLECTION_IMAGES = 1_000_000;
export const MAX_COLLECTION_PARTS = 64;
export const MAX_PART_BYTES = 2 * 1024 ** 3 - 64 * 1024 ** 2;
export const MAX_PART_ENTRIES = 60_000;
export const DEFAULT_PART_BYTES = 256 * 1024 ** 2;
export const MAX_BLOB_QUERY = 5000;
export const MAX_MANIFEST_BYTES = 256 * 1024 ** 2;
export const MAX_COLLECTION_CLASSES = 1000;
/** Manifest columns that the server keeps for itself (`MANIFEST_RESERVED`). */
export const MANIFEST_RESERVED = new Set(["image_id", "sha256", "pack", "offset", "stored", "method", "crc32", "nbytes", "format", "width", "height", "mode"]);
/** File extensions of the images a collection takes (`PART_ENTRY_RE`). */
export const IMAGE_EXTENSIONS = new Set(["png", "jpg", "jpeg", "tif", "tiff"]);
/** Tries of one part upload before the upload stops. */
export const PART_TRIES = 3;
export const RESUME_PREFIX = "mapi.collectionUpload.";

const SHA256_RE = /^[0-9a-f]{64}$/;

// ------------------------------------------------------------------------------ v0.15 DTOs

export type { BlobMissing, CollectionInfo, CollectionOptions, CollectionSource, PackInfo };
export type ImageMode = NonNullable<CollectionOptions["mode"]>;
export type ImageResize = NonNullable<CollectionOptions["resize"]>;

/** An upload session (with `received` on `GET`/`renew`). */
export type CollectionUploadSession = UploadSession;

/** A collection info with the defaults of its optional fields filled in. */
export type CollectionView = CollectionInfo &
  Required<Pick<CollectionInfo, "classes" | "other_classes" | "splits" | "formats" | "columns">> & { base_version: number | null };

/** The collection info of a version, or `null` (any other layout). */
export function collectionOf(version: DatasetVersion): CollectionView | null {
  const c = version.collection;
  if (!c || typeof c.n_images !== "number" || !Array.isArray(c.packs)) return null;
  return {
    ...c,
    classes: (c.classes ?? []).filter((x) => typeof x.label === "string" && typeof x.count === "number"),
    other_classes: c.other_classes ?? 0,
    splits: c.splits ?? {},
    formats: c.formats ?? {},
    columns: c.columns ?? [],
    base_version: c.base_version ?? null,
  };
}

/** A version of the collection layout. */
export function isCollectionVersion(version: DatasetVersion): boolean {
  return version.layout === "collection" || collectionOf(version) !== null;
}

// ------------------------------------------------------------------------------- planning

export interface PlannedImage {
  /** The path under the selected folder (`circle/a.png`), the manifest's path column. */
  path: string;
  sha256: string;
  /** The lowercase file extension (`png`, `jpg`, `jpeg`, `tif`, `tiff`). */
  ext: string;
  size: number;
  crc32: number;
  label: string | null;
  split: string | null;
  /** Other manifest columns of the image (only with a manifest). */
  extra: Record<string, string>;
  file: File;
}

export interface CollectionPlan {
  images: PlannedImage[];
  /** Extra manifest columns, in manifest order. */
  columns: string[];
  labelSource: "folder" | "manifest" | "none";
  hasSplit: boolean;
  /** Distinct images (by SHA-256). */
  distinct: number;
  totalBytes: number;
  /** Files that are not images (a manifest, `.DS_Store`, …): not uploaded. */
  ignored: string[];
  /** Images in the folder that the manifest does not list: not uploaded. */
  unlisted: string[];
  /** Problems that block the upload. */
  problems: string[];
}

export interface PlanOptions {
  /** The label of an image is the first folder under the selected folder. */
  labelFromFolder: boolean;
  /** An optional manifest table (CSV or TSV) with one row for each image. */
  manifest?: { name: string; text: string } | null;
  pathColumn?: string;
  labelColumn?: string;
  splitColumn?: string;
  onProgress?: (done: number, total: number) => void;
  /** Hash this many files at the same time (default 4). */
  concurrency?: number;
  signal?: AbortSignal;
}

/** The path of a file under the selected folder (the top folder name removed). */
export function relativePath(file: File): string {
  const raw = (file.webkitRelativePath || file.name).replace(/\\/g, "/");
  const parts = raw.split("/").filter(Boolean);
  return parts.length > 1 ? parts.slice(1).join("/") : (parts[0] ?? file.name);
}

/** The lowercase extension of an image file name, or `null` when it is not an image. */
export function imageExtension(name: string): string | null {
  const dot = name.lastIndexOf(".");
  if (dot < 0) return null;
  const ext = name.slice(dot + 1).toLowerCase();
  return IMAGE_EXTENSIONS.has(ext) ? ext : null;
}

function hex(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, "0")).join("");
}

/** SHA-256 hex digest of bytes (`crypto.subtle`). */
export async function sha256Hex(bytes: Uint8Array | ArrayBuffer): Promise<string> {
  const data = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  return hex(await crypto.subtle.digest("SHA-256", data as Uint8Array<ArrayBuffer>));
}

/** Run `fn` over `items` with at most `limit` calls at the same time, results in input order. */
async function pool<T, R>(items: readonly T[], limit: number, fn: (item: T, i: number) => Promise<R>, signal?: AbortSignal): Promise<R[]> {
  const out = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      if (signal?.aborted) throw new DOMException("The upload was cancelled.", "AbortError");
      const i = next++;
      out[i] = await fn(items[i]!, i);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, worker));
  return out;
}

interface ManifestRows {
  header: string[];
  byPath: Map<string, Record<string, string>>;
}

function readManifest(manifest: NonNullable<PlanOptions["manifest"]>, pathColumn: string, problems: string[]): ManifestRows | null {
  if (manifest.text.length > MAX_MANIFEST_BYTES) {
    problems.push(`The manifest ${manifest.name} is too large. The maximum is ${MAX_MANIFEST_BYTES / 1024 ** 2} MB.`);
    return null;
  }
  const delimiter = /\.tsv$/i.test(manifest.name) ? "\t" : undefined;
  const csv = parseCsv(manifest.text, MAX_COLLECTION_IMAGES + 1, delimiter);
  if (!csv.header.length) {
    problems.push(`The manifest ${manifest.name} has no header row.`);
    return null;
  }
  const pathAt = csv.header.indexOf(pathColumn);
  if (pathAt < 0) {
    problems.push(`The manifest ${manifest.name} has no column "${pathColumn}". Type the name of the column with the path of each image.`);
    return null;
  }
  const byPath = new Map<string, Record<string, string>>();
  for (const row of csv.rows) {
    if (row.length === 1 && row[0] === "") continue;
    const path = (row[pathAt] ?? "").trim().replace(/\\/g, "/").replace(/^\.\//, "");
    if (!path) continue;
    if (byPath.has(path)) {
      problems.push(`The manifest has the path ${path} two times. Each image must have one row.`);
      continue;
    }
    const rec: Record<string, string> = {};
    csv.header.forEach((h, i) => (rec[h] = row[i] ?? ""));
    byPath.set(path, rec);
  }
  return { header: csv.header, byPath };
}

/**
 * Hash the images of a folder selection and give each its label and split. With a manifest,
 * only the images it lists are in the plan; its label and split columns give the values and
 * its other columns stay in the manifest that the server gets.
 */
export async function planCollection(files: readonly File[], opts: PlanOptions): Promise<CollectionPlan> {
  const problems: string[] = [];
  const ignored: string[] = [];
  const candidates: { file: File; path: string; ext: string }[] = [];
  for (const file of files) {
    const path = relativePath(file);
    const ext = imageExtension(path);
    if (ext && file.size > 0) candidates.push({ file, path, ext });
    else ignored.push(path);
  }
  candidates.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));

  const pathColumn = opts.pathColumn?.trim() || "path";
  const labelColumn = opts.labelColumn?.trim() || "";
  const splitColumn = opts.splitColumn?.trim() || "";
  const manifest = opts.manifest ? readManifest(opts.manifest, pathColumn, problems) : null;
  if (manifest) {
    for (const [field, name] of [["label", labelColumn], ["split", splitColumn]] as const) {
      if (name && !manifest.header.includes(name)) problems.push(`The manifest has no ${field} column "${name}".`);
    }
  }
  const unlisted: string[] = [];
  let selected = candidates;
  if (manifest) {
    selected = candidates.filter((c) => manifest.byPath.has(c.path));
    for (const c of candidates) if (!manifest.byPath.has(c.path)) unlisted.push(c.path);
    const found = new Set(selected.map((c) => c.path));
    const absent = [...manifest.byPath.keys()].filter((p) => !found.has(p));
    if (absent.length) {
      problems.push(
        `The folder does not have ${absent.length === 1 ? "the image" : `${absent.length} images`} of the manifest: ${absent.slice(0, 5).join(", ")}${absent.length > 5 ? ", …" : ""}. Select the folder that has all the images.`,
      );
    }
  }
  if (selected.length > MAX_COLLECTION_IMAGES) problems.push(`The folder has ${selected.length.toLocaleString("en-US")} images. The maximum is ${MAX_COLLECTION_IMAGES.toLocaleString("en-US")}.`);
  if (!selected.length && !problems.length) problems.push("The folder has no images. Select a folder with PNG, JPEG or TIFF files.");

  const extraColumns = manifest
    ? manifest.header.filter((h) => h !== pathColumn && h !== labelColumn && h !== splitColumn && !MANIFEST_RESERVED.has(h) && h !== "label" && h !== "split" && h.trim() !== "")
    : [];
  const dropped = manifest ? manifest.header.filter((h) => MANIFEST_RESERVED.has(h) && h !== pathColumn) : [];
  if (dropped.length) problems.push(`The manifest has columns that the server uses for itself: ${dropped.join(", ")}. Rename or remove them.`);

  let done = 0;
  const images = problems.length
    ? []
    : await pool(
        selected,
        opts.concurrency ?? 4,
        async ({ file, path, ext }): Promise<PlannedImage> => {
          const bytes = await blobBytes(file);
          const sha256 = await sha256Hex(bytes);
          const row = manifest?.byPath.get(path);
          let label: string | null = null;
          if (row && labelColumn) label = row[labelColumn]?.trim() || null;
          else if (!manifest && opts.labelFromFolder) label = path.includes("/") ? path.split("/")[0]! : null;
          const split = row && splitColumn ? row[splitColumn]?.trim() || null : null;
          const extra: Record<string, string> = {};
          for (const c of extraColumns) extra[c] = row?.[c] ?? "";
          done += 1;
          opts.onProgress?.(done, selected.length);
          return { path, sha256, ext, size: bytes.length, crc32: crc32(bytes), label, split, extra, file };
        },
        opts.signal,
      );
  const labelSource: CollectionPlan["labelSource"] = manifest && labelColumn ? "manifest" : !manifest && opts.labelFromFolder ? "folder" : "none";
  const classes = new Set(images.map((i) => i.label).filter((l): l is string => l !== null));
  if (classes.size > MAX_COLLECTION_CLASSES) problems.push(`The images have ${classes.size} classes. The maximum is ${MAX_COLLECTION_CLASSES}.`);
  if (labelSource === "folder" && images.length && !classes.size) {
    problems.push("No image is in a folder, so no image gets a class. Put the images of each class in a folder with the name of the class, or clear the option.");
  }
  const distinct = new Map(images.map((i) => [i.sha256, i.size]));
  return {
    images,
    columns: extraColumns,
    labelSource,
    hasSplit: images.some((i) => i.split !== null),
    distinct: distinct.size,
    totalBytes: [...distinct.values()].reduce((a, b) => a + b, 0),
    ignored,
    unlisted,
    problems,
  };
}

function csvCell(value: string): string {
  return /[",\r\n]|^\s|\s$/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/** The columns of the manifest CSV that the server gets: `path,sha256,label[,split]` + manifest columns. */
export function manifestColumns(plan: CollectionPlan): string[] {
  return ["path", "sha256", "label", ...(plan.hasSplit ? ["split"] : []), ...plan.columns];
}

/** The manifest CSV of a plan (one row per image, in path order; `\n` line ends). */
export function manifestCsv(plan: CollectionPlan): string {
  const header = manifestColumns(plan);
  const lines = [header.map(csvCell).join(",")];
  for (const img of plan.images) {
    const row = [img.path, img.sha256, img.label ?? "", ...(plan.hasSplit ? [img.split ?? ""] : []), ...plan.columns.map((c) => img.extra[c] ?? "")];
    lines.push(row.map(csvCell).join(","));
  }
  return lines.join("\n") + "\n";
}

/** The `CollectionOptions` that go with {@link manifestCsv}. */
export function collectionOptions(plan: CollectionPlan, opts: { idColumn?: string; mode?: ImageMode; size?: [number, number] | null; resize?: ImageResize }): CollectionOptions {
  const out: CollectionOptions = {
    sha256_column: "sha256",
    path_column: "path",
    label_column: plan.images.some((i) => i.label !== null) ? "label" : null,
    split_column: plan.hasSplit ? "split" : null,
    mode: opts.mode ?? "auto",
    max_images: Math.max(1, plan.images.length),
  };
  const id = opts.idColumn?.trim();
  if (id) out.id_column = id;
  if (opts.size) {
    out.size = opts.size;
    out.resize = opts.resize ?? "stretch";
  }
  return out;
}

export interface PartEntry {
  /** `<sha256>.<ext>` */
  name: string;
  sha256: string;
  file: File;
  crc32: number;
  size: number;
}

export interface PartPlan {
  index: number;
  entries: PartEntry[];
  /** The size of the zip. */
  bytes: number;
  /** SHA-256 of the sorted entry names: the identity of the part in the resume state. */
  key: string;
}

/**
 * The parts of the images whose SHA-256 is in `missing` (each image once), sorted by digest:
 * a new part starts when the zip would pass `partBytes` or `MAX_PART_ENTRIES` entries. An
 * image larger than `partBytes` gets a part of its own. `key` is filled by {@link partKeys}.
 */
export function planParts(plan: Pick<CollectionPlan, "images">, missing: ReadonlySet<string>, partBytes: number = DEFAULT_PART_BYTES): PartPlan[] {
  const byDigest = new Map<string, PartEntry>();
  for (const img of plan.images) {
    if (!missing.has(img.sha256) || byDigest.has(img.sha256)) continue;
    byDigest.set(img.sha256, { name: `${img.sha256}.${img.ext}`, sha256: img.sha256, file: img.file, crc32: img.crc32, size: img.size });
  }
  const entries = [...byDigest.values()].sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const cap = Math.min(Math.max(1, partBytes), MAX_PART_BYTES);
  const parts: PartPlan[] = [];
  const empty = storeZipSize([]);
  let current: PartEntry[] = [];
  let bytes = empty;
  for (const e of entries) {
    const add = storeZipSize([{ nameLength: e.name.length, size: e.size }]) - empty;
    if (current.length && (current.length >= MAX_PART_ENTRIES || bytes + add > cap)) {
      parts.push({ index: parts.length, entries: current, bytes, key: "" });
      current = [];
      bytes = empty;
    }
    current.push(e);
    bytes += add;
  }
  if (current.length) parts.push({ index: parts.length, entries: current, bytes, key: "" });
  return parts;
}

/** Fill in the `key` of each part (SHA-256 of its sorted entry names). */
export async function partKeys(parts: PartPlan[]): Promise<PartPlan[]> {
  const enc = new TextEncoder();
  return Promise.all(parts.map(async (p) => ({ ...p, key: await sha256Hex(enc.encode(p.entries.map((e) => e.name).join("\n"))) })));
}

/** The zip of a part (deterministic: the same entries give the same bytes). */
export function partZip(part: PartPlan): Blob {
  return buildStoreZip(part.entries.map((e): ZipEntry => ({ name: e.name, blob: e.file, crc32: e.crc32 })));
}

// ------------------------------------------------------------------------ the options form

/** The options form of the wizard step "Folder of images" (strings as typed). */
export interface CollectionDraft {
  /** Where the class of an image comes from. */
  labelFrom: "folder" | "column" | "none";
  pathColumn: string;
  labelColumn: string;
  splitColumn: string;
  idColumn: string;
  height: string;
  width: string;
  resize: ImageResize;
  mode: ImageMode;
  /** Compare with the latest ready collection version (upload only new images), or upload all. */
  base: "latest" | "none";
}

export const DEFAULT_COLLECTION_DRAFT: CollectionDraft = {
  labelFrom: "folder",
  pathColumn: "path",
  labelColumn: "label",
  splitColumn: "",
  idColumn: "",
  height: "",
  width: "",
  resize: "stretch",
  mode: "auto",
  base: "latest",
};

export const MAX_COLLECTION_SIDE = 4096;
const COLUMN_RE = /^[^\u0000-\u001f]{1,128}$/;

export interface DraftProblem {
  field: keyof CollectionDraft | "manifest";
  message: string;
}

/** The problems of the options form (each blocks the upload). */
export function validateCollectionDraft(d: CollectionDraft, hasManifest: boolean): DraftProblem[] {
  const out: DraftProblem[] = [];
  if (d.labelFrom === "column" && !hasManifest) out.push({ field: "manifest", message: "Select a manifest file to read the classes from a column, or take the classes from the folders." });
  if (d.labelFrom === "column" && !d.labelColumn.trim()) out.push({ field: "labelColumn", message: "Type the name of the class column of the manifest." });
  if (hasManifest && !d.pathColumn.trim()) out.push({ field: "pathColumn", message: "Type the name of the path column of the manifest." });
  for (const [field, label] of [["splitColumn", "split column"], ["idColumn", "id column"]] as const) {
    if (d[field].trim() && !hasManifest) out.push({ field, message: `A ${label} needs a manifest file. Select a manifest, or clear the ${label}.` });
  }
  for (const field of ["pathColumn", "labelColumn", "splitColumn", "idColumn"] as const) {
    const v = d[field].trim();
    if (v && !COLUMN_RE.test(v)) out.push({ field, message: `The column name "${v}" is not correct.` });
  }
  const h = d.height.trim();
  const w = d.width.trim();
  if (Boolean(h) !== Boolean(w)) out.push({ field: h ? "width" : "height", message: "Type the height and the width, or clear both." });
  for (const [field, v] of [["height", h], ["width", w]] as const) {
    if (v && !(/^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= MAX_COLLECTION_SIDE)) out.push({ field, message: `The ${field} must be a whole number from 1 to ${MAX_COLLECTION_SIDE}.` });
  }
  return out;
}

/** The `[height, width]` of the form, or `null` (keep the sizes). */
export function draftSize(d: CollectionDraft): [number, number] | null {
  const h = Number(d.height.trim());
  const w = Number(d.width.trim());
  return d.height.trim() && d.width.trim() && h >= 1 && w >= 1 ? [h, w] : null;
}

// --------------------------------------------------------------------------- resume state

export interface ResumeState {
  manifestUploadId: string | null;
  /** Part index → upload id. */
  parts: Record<string, string>;
  /** Part index → part key (a part whose key changed is uploaded again). */
  keys?: Record<string, string>;
}

export function resumeKey(datasetId: string, manifestSha256: string): string {
  return `${RESUME_PREFIX}${datasetId}.${manifestSha256}`;
}

/** The saved state of an upload of this manifest, or an empty state (storage can be blocked). */
export function loadResume(datasetId: string, manifestSha256: string): ResumeState {
  try {
    const raw = localStorage.getItem(resumeKey(datasetId, manifestSha256));
    if (!raw) return { manifestUploadId: null, parts: {} };
    const v = JSON.parse(raw) as Partial<ResumeState>;
    const ids = (o: unknown): Record<string, string> =>
      typeof o === "object" && o !== null ? Object.fromEntries(Object.entries(o).filter((e): e is [string, string] => typeof e[1] === "string")) : {};
    return { manifestUploadId: typeof v.manifestUploadId === "string" ? v.manifestUploadId : null, parts: ids(v.parts), keys: ids(v.keys) };
  } catch {
    return { manifestUploadId: null, parts: {} };
  }
}

export function saveResume(datasetId: string, manifestSha256: string, state: ResumeState): void {
  try {
    localStorage.setItem(resumeKey(datasetId, manifestSha256), JSON.stringify(state));
  } catch {
    // storage is full or blocked: the upload works, it only cannot resume after a reload
  }
}

export function clearResume(datasetId: string, manifestSha256: string): void {
  try {
    localStorage.removeItem(resumeKey(datasetId, manifestSha256));
  } catch {
    // nothing to do
  }
}

// ------------------------------------------------------------------------------ the upload

/** The HTTP calls of a collection upload (the real one is {@link httpCollectionApi}; tests give fakes). */
export interface CollectionApi {
  createUpload(body: { filename: string; content_type: string; size_bytes: number }): Promise<CollectionUploadSession>;
  getUpload(uploadId: string): Promise<CollectionUploadSession>;
  renewUpload(uploadId: string): Promise<CollectionUploadSession>;
  findMissing(sha256: string[], baseVersion: number | null): Promise<BlobMissing>;
  put(session: CollectionUploadSession, blob: Blob, onProgress: (p: UploadProgress) => void, signal?: AbortSignal): Promise<void>;
}

/** What the upload does now (the progress of the wizard). */
export type UploadPhase =
  | { phase: "checking"; done: number; total: number }
  | { phase: "manifest" }
  | { phase: "part"; index: number; count: number; loaded: number; total: number; attempt: number }
  | { phase: "skipped"; index: number; count: number };

export interface CollectionUploadResult {
  source: CollectionSource;
  manifestSha256: string;
  parts: number;
  skippedParts: number;
  newImages: number;
  presentImages: number;
  baseVersion: number | null;
}

/**
 * The images that the base version lacks. `baseVersion` 0 asks nothing (all are new); `null`
 * compares with the latest ready collection version of the dataset.
 */
export async function findMissing(client: Pick<CollectionApi, "findMissing">, shas: readonly string[], baseVersion: number | null, onProgress?: (done: number, total: number) => void): Promise<{ missing: Set<string>; base: number | null; present: number }> {
  const unique = [...new Set(shas)].sort();
  if (baseVersion === 0) return { missing: new Set(unique), base: null, present: 0 };
  const missing = new Set<string>();
  let base: number | null = null;
  let present = 0;
  // the first answer resolves "latest"; the other chunks ask that version (or no base), so a
  // version that becomes ready meanwhile cannot answer part of the images
  let ask = baseVersion;
  for (let i = 0; i < unique.length; i += MAX_BLOB_QUERY) {
    const chunk = unique.slice(i, i + MAX_BLOB_QUERY);
    const res = await client.findMissing(chunk, ask);
    for (const s of res.missing) if (SHA256_RE.test(s)) missing.add(s);
    base = res.base_version;
    ask = base ?? 0;
    present += res.present;
    onProgress?.(Math.min(unique.length, i + chunk.length), unique.length);
  }
  return { missing, base, present };
}

function expired(session: CollectionUploadSession, now = Date.now()): boolean {
  const t = Date.parse(session.upload.expires_at);
  return Number.isFinite(t) && t <= now + 30_000;
}

function needsRenew(err: unknown): boolean {
  return err instanceof UploadError && (err.status === 403 || err.status === 400 || /expired/i.test(err.message));
}

/**
 * Put `blob` into an upload session (an earlier one when the resume state has it): skip it
 * when the server says it arrived, renew an expired URL, try at most {@link PART_TRIES} times.
 * Returns the upload id and whether the bytes were already there.
 */
async function putOnce(
  client: CollectionApi,
  blob: Blob,
  meta: { filename: string; contentType: string },
  previous: string | null,
  onProgress: (p: UploadProgress, attempt: number) => void,
  signal?: AbortSignal,
): Promise<{ id: string; skipped: boolean }> {
  let session: CollectionUploadSession | null = null;
  if (previous) {
    try {
      const s = await client.getUpload(previous);
      if (s.received) return { id: s.id, skipped: true };
      if (s.size_bytes === blob.size) session = s;
    } catch (err) {
      if (!(err instanceof ApiError && (err.status === 404 || err.status === 409 || err.status === 410))) throw err;
    }
  }
  session ??= await client.createUpload({ filename: meta.filename, content_type: meta.contentType, size_bytes: blob.size });
  let lastError: unknown = null;
  for (let attempt = 1; attempt <= PART_TRIES; attempt++) {
    if (signal?.aborted) throw new UploadError("upload cancelled", null);
    try {
      if (expired(session)) session = await client.renewUpload(session.id);
      await client.put(session, blob, (p) => onProgress(p, attempt), signal);
      return { id: session.id, skipped: false };
    } catch (err) {
      lastError = err;
      if (signal?.aborted) throw err;
      // the bytes can have arrived although the answer did not (a retried PUT gets HTTP 412)
      const check = await client.getUpload(session.id).catch(() => null);
      if (check?.received) return { id: session.id, skipped: false };
      if (needsRenew(err)) {
        try {
          session = await client.renewUpload(session.id);
        } catch (renewErr) {
          lastError = renewErr;
        }
      }
    }
  }
  throw lastError instanceof Error ? lastError : new UploadError("the upload failed", null);
}

export interface UploadCollectionOptions {
  datasetId: string;
  plan: CollectionPlan;
  options: CollectionOptions;
  /** `null`: the latest ready collection version; 0: no base (upload every image). */
  baseVersion: number | null;
  partBytes?: number;
  onPhase?: (p: UploadPhase) => void;
  signal?: AbortSignal;
}

/**
 * Upload a planned collection: ask which images are new, put the manifest and every part
 * (resuming from the saved state), and return the `CollectionSource` of the version.
 */
export async function uploadCollection(client: CollectionApi, o: UploadCollectionOptions): Promise<CollectionUploadResult> {
  const { plan } = o;
  o.onPhase?.({ phase: "checking", done: 0, total: plan.distinct });
  const { missing, base, present } = await findMissing(
    client,
    plan.images.map((i) => i.sha256),
    o.baseVersion,
    (done, total) => o.onPhase?.({ phase: "checking", done, total }),
  );
  const parts = await partKeys(planParts(plan, missing, o.partBytes ?? DEFAULT_PART_BYTES));
  if (parts.length > MAX_COLLECTION_PARTS) {
    throw new UploadError(`The new images need ${parts.length} parts. The maximum is ${MAX_COLLECTION_PARTS}. Upload fewer images, or upload them in two versions.`, null);
  }
  const manifestText = manifestCsv(plan);
  const manifestBytes = new TextEncoder().encode(manifestText);
  const manifestSha256 = await sha256Hex(manifestBytes);
  const state = loadResume(o.datasetId, manifestSha256);
  const keys = state.keys ?? {};

  o.onPhase?.({ phase: "manifest" });
  const manifest = await putOnce(client, new Blob([manifestBytes], { type: "text/csv" }), { filename: "manifest.csv", contentType: "text/csv" }, state.manifestUploadId, () => undefined, o.signal);
  state.manifestUploadId = manifest.id;
  saveResume(o.datasetId, manifestSha256, state);

  const ids: string[] = [];
  let skipped = 0;
  for (const part of parts) {
    const i = String(part.index);
    const previous = keys[i] === part.key ? (state.parts[i] ?? null) : null;
    const blob = partZip(part);
    const res = await putOnce(
      client,
      blob,
      { filename: `part-${String(part.index).padStart(4, "0")}.zip`, contentType: "application/zip" },
      previous,
      (p, attempt) => o.onPhase?.({ phase: "part", index: part.index, count: parts.length, loaded: p.loaded, total: p.total, attempt }),
      o.signal,
    );
    if (res.skipped) {
      skipped += 1;
      o.onPhase?.({ phase: "skipped", index: part.index, count: parts.length });
    }
    ids.push(res.id);
    state.parts[i] = res.id;
    keys[i] = part.key;
    state.keys = keys;
    saveResume(o.datasetId, manifestSha256, state);
  }
  return {
    // the version the parts were compared with (0: none), not "latest" again at create time
    source: { manifest_upload_id: manifest.id, part_upload_ids: ids, base_version: base ?? 0, options: o.options },
    manifestSha256,
    parts: parts.length,
    skippedParts: skipped,
    newImages: missing.size,
    presentImages: present,
    baseVersion: base,
  };
}

// ------------------------------------------------------------------------- HTTP (v0.15 routes)

export function httpCollectionApi(datasetId: string, headers: Record<string, string> = {}): CollectionApi {
  const path = { dataset_id: datasetId };
  return {
    createUpload: (body) => unwrap(api.POST("/v1/datasets/{dataset_id}/uploads", { params: { path }, headers, body })),
    getUpload: (uploadId) => unwrap(api.GET("/v1/datasets/{dataset_id}/uploads/{upload_id}", { params: { path: { ...path, upload_id: uploadId } }, headers })),
    renewUpload: (uploadId) => unwrap(api.POST("/v1/datasets/{dataset_id}/uploads/{upload_id}/renew", { params: { path: { ...path, upload_id: uploadId } }, headers })),
    findMissing: (sha256, baseVersion) =>
      unwrap(api.POST("/v1/datasets/{dataset_id}/blobs/missing", { params: { path }, headers, body: { sha256, ...(baseVersion !== null ? { base_version: baseVersion } : {}) } })),
    put: (session, blob, onProgress, signal) => putToSignedUrl(session, blob, onProgress, signal),
  };
}

/**
 * A stable Idempotency-Key for the version create of these uploads (as the Python client's):
 * a retry after a lost answer, or a resumed upload of the same uploads, gets the version that
 * the first call made instead of a 409 for uploads that are already used.
 */
export async function collectionKey(datasetId: string, source: Pick<CollectionSource, "manifest_upload_id" | "part_upload_ids">): Promise<string> {
  const name = [datasetId, source.manifest_upload_id, ...(source.part_upload_ids ?? [])].join("\n");
  return `collection-${(await sha256Hex(new TextEncoder().encode(name))).slice(0, 48)}`;
}

/** `POST /v1/datasets/{id}/versions` with a collection source. */
export function createCollectionVersion(datasetId: string, body: { collection: CollectionSource; message?: string }, key: string, headers: Record<string, string> = {}): Promise<DatasetVersion> {
  return unwrap(
    api.POST("/v1/datasets/{dataset_id}/versions", {
      params: { path: { dataset_id: datasetId }, header: { "Idempotency-Key": key } },
      headers,
      body,
    }),
  );
}
