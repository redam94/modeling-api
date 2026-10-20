/**
 * The browser side of a collection upload (v0.15): planning a folder (hashes, labels from
 * folders or a manifest), the manifest CSV, part planning caps, the resume state and the upload
 * loop against a fake API (skips parts that arrived, renews expired URLs, tries 3 times).
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { UploadError } from "@/api/upload";

import {
  collectionKey,
  collectionOf,
  collectionOptions,
  DEFAULT_COLLECTION_DRAFT,
  DEFAULT_PART_BYTES,
  draftSize,
  findMissing,
  isCollectionVersion,
  loadResume,
  MAX_BLOB_QUERY,
  MAX_PART_ENTRIES,
  manifestCsv,
  partZip,
  planCollection,
  planParts,
  relativePath,
  resumeKey,
  saveResume,
  sha256Hex,
  uploadCollection,
  validateCollectionDraft,
  type BlobMissing,
  type CollectionApi,
  type CollectionPlan,
  type CollectionUploadSession,
  type PlannedImage,
} from "./collection-upload";
import { blobBytes, crc32 } from "./zip-store";

const enc = new TextEncoder();
const SHA_ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

/** A file as a folder input gives it (`webkitRelativePath` under the selected folder). */
function picked(path: string, content: string | Uint8Array<ArrayBuffer>): File {
  const name = path.split("/").at(-1)!;
  const file = new File([typeof content === "string" ? enc.encode(content) : content], name);
  Object.defineProperty(file, "webkitRelativePath", { value: path });
  return file;
}

const folder = () => [
  picked("shapes/circle/a.png", "abc"),
  picked("shapes/circle/b.PNG", "circle-b"),
  picked("shapes/square/c.jpg", "square-c"),
  picked("shapes/square/dup.png", "abc"), // the same bytes as circle/a.png
  picked("shapes/notes.txt", "not an image"),
  picked("shapes/.DS_Store", "x"),
  picked("shapes/loose.tif", "no folder"),
];

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("planCollection", () => {
  it("hashes the images, takes labels from the first folder and ignores other files", async () => {
    const progress: number[] = [];
    const plan = await planCollection(folder(), { labelFromFolder: true, onProgress: (d) => progress.push(d) });
    expect(plan.problems).toEqual([]);
    expect(plan.images.map((i) => i.path)).toEqual(["circle/a.png", "circle/b.PNG", "loose.tif", "square/c.jpg", "square/dup.png"]);
    expect(plan.images.map((i) => i.label)).toEqual(["circle", "circle", null, "square", "square"]);
    expect(plan.images.map((i) => i.ext)).toEqual(["png", "png", "tif", "jpg", "png"]);
    expect(plan.images[0]!.sha256).toBe(SHA_ABC);
    expect(plan.images[0]!.crc32).toBe(crc32(enc.encode("abc")));
    expect(plan.ignored.sort()).toEqual([".DS_Store", "notes.txt"]);
    expect(plan.distinct).toBe(4);
    expect(plan.labelSource).toBe("folder");
    expect(progress).toHaveLength(5);
    expect(relativePath(picked("top/a/b/c.png", "x"))).toBe("a/b/c.png");
  });

  it("reads labels, splits and other columns from a manifest", async () => {
    const manifest = { name: "manifest.csv", text: 'file,class,fold,site,width\ncircle/a.png,round,train,"Paris, FR",1\nsquare/c.jpg,box,test,Oslo,2\n' };
    const plan = await planCollection(folder(), { labelFromFolder: false, manifest, pathColumn: "file", labelColumn: "class", splitColumn: "fold" });
    // `width` is a reserved column of the server
    expect(plan.problems).toEqual([expect.stringContaining("width")]);
    const ok = await planCollection(folder(), {
      labelFromFolder: false,
      manifest: { name: "manifest.csv", text: 'file,class,fold,site\ncircle/a.png,round,train,"Paris, FR"\nsquare/c.jpg,box,test,Oslo\n' },
      pathColumn: "file",
      labelColumn: "class",
      splitColumn: "fold",
    });
    expect(ok.problems).toEqual([]);
    expect(ok.images.map((i) => [i.path, i.label, i.split, i.extra.site])).toEqual([
      ["circle/a.png", "round", "train", "Paris, FR"],
      ["square/c.jpg", "box", "test", "Oslo"],
    ]);
    expect(ok.unlisted).toEqual(["circle/b.PNG", "loose.tif", "square/dup.png"]);
    expect(ok.columns).toEqual(["site"]);
    expect(manifestCsv(ok)).toBe(`path,sha256,label,split,site\ncircle/a.png,${SHA_ABC},round,train,"Paris, FR"\nsquare/c.jpg,${ok.images[1]!.sha256},box,test,Oslo\n`);
    expect(collectionOptions(ok, { mode: "gray", size: [32, 24], resize: "crop" })).toEqual({
      sha256_column: "sha256",
      path_column: "path",
      label_column: "label",
      split_column: "split",
      mode: "gray",
      max_images: 2,
      size: [32, 24],
      resize: "crop",
    });
  });

  it("names the images of the manifest that the folder does not have", async () => {
    const manifest = { name: "m.csv", text: "path,label\ncircle/a.png,c\nmissing/x.png,m\n" };
    const plan = await planCollection(folder(), { labelFromFolder: false, manifest, labelColumn: "label" });
    expect(plan.problems).toEqual([expect.stringContaining("missing/x.png")]);
    expect(plan.images).toEqual([]);
    const noPath = await planCollection(folder(), { labelFromFolder: false, manifest: { name: "m.csv", text: "file\nx.png\n" } });
    expect(noPath.problems[0]).toMatch(/no column "path"/);
  });

  it("refuses a folder without images, and folder labels when no image is in a folder", async () => {
    expect((await planCollection([picked("top/readme.md", "x")], { labelFromFolder: true })).problems[0]).toMatch(/has no images/);
    expect((await planCollection([picked("top/a.png", "x")], { labelFromFolder: true })).problems[0]).toMatch(/No image is in a folder/);
    const unlabelled = await planCollection([picked("top/a.png", "x")], { labelFromFolder: false });
    expect(unlabelled.problems).toEqual([]);
    expect(collectionOptions(unlabelled, {}).label_column).toBeNull();
    expect(manifestCsv(unlabelled).split("\n")[0]).toBe("path,sha256,label");
  });
});

function fakeImages(n: number, size: number): PlannedImage[] {
  const file = new File(["x"], "x.png");
  return Array.from({ length: n }, (_, i) => ({
    path: `img/${i}.png`,
    sha256: i.toString(16).padStart(64, "0"),
    ext: "png",
    size,
    crc32: 0,
    label: null,
    split: null,
    extra: {},
    file,
  }));
}

describe("planParts", () => {
  it("packs only missing images, once each, sorted by digest, under the byte cap", () => {
    const images = fakeImages(10, 1000);
    const dup = { ...images[3]!, path: "img/dup.png" };
    const missing = new Set(images.slice(2).map((i) => i.sha256));
    const parts = planParts({ images: [...images].reverse().concat(dup) }, missing, 3700);
    expect(parts.map((p) => p.entries.length)).toEqual([3, 3, 2]);
    expect(parts.flatMap((p) => p.entries.map((e) => e.sha256))).toEqual(images.slice(2).map((i) => i.sha256));
    for (const p of parts) expect(p.bytes).toBeLessThanOrEqual(3700);
    expect(parts.map((p) => p.index)).toEqual([0, 1, 2]);
    expect(parts[0]!.entries[0]!.name).toBe(`${images[2]!.sha256}.png`);
  });

  it("starts a new part at MAX_PART_ENTRIES entries and gives a large image its own part", () => {
    const many = fakeImages(MAX_PART_ENTRIES + 5, 1);
    const parts = planParts({ images: many }, new Set(many.map((i) => i.sha256)), DEFAULT_PART_BYTES);
    expect(parts.map((p) => p.entries.length)).toEqual([MAX_PART_ENTRIES, 5]);
    const big = fakeImages(3, 5000);
    expect(planParts({ images: big }, new Set(big.map((i) => i.sha256)), 2000).map((p) => p.entries.length)).toEqual([1, 1, 1]);
  });

  it("builds the same zip bytes again", async () => {
    const plan = await planCollection(folder(), { labelFromFolder: true });
    const all = new Set(plan.images.map((i) => i.sha256));
    const [a] = planParts(plan, all);
    const [b] = planParts(plan, all);
    const one = await blobBytes(partZip(a!));
    expect(one).toEqual(await blobBytes(partZip(b!)));
    expect(one.length).toBe(a!.bytes);
  });
});

describe("resume state", () => {
  it("round trips in localStorage under the dataset and manifest key", () => {
    expect(loadResume("ds_1", "f".repeat(64))).toEqual({ manifestUploadId: null, parts: {} });
    saveResume("ds_1", "f".repeat(64), { manifestUploadId: "upl_m", parts: { "0": "upl_0" }, keys: { "0": "k" } });
    expect(localStorage.getItem(resumeKey("ds_1", "f".repeat(64)))).toContain("upl_0");
    expect(resumeKey("ds_1", "ab")).toBe("mapi.collectionUpload.ds_1.ab");
    expect(loadResume("ds_1", "f".repeat(64))).toEqual({ manifestUploadId: "upl_m", parts: { "0": "upl_0" }, keys: { "0": "k" } });
  });

  it("works without storage", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(() => saveResume("ds", "x", { manifestUploadId: "u", parts: {} })).not.toThrow();
    expect(loadResume("ds", "x")).toEqual({ manifestUploadId: null, parts: {} });
  });
});

/** An API of fake upload sessions: `arrived` ids got their bytes. */
function fakeApi(opts: { missing?: (shas: string[]) => string[]; failPuts?: number; status?: number | null } = {}) {
  let seq = 0;
  const sessions = new Map<string, CollectionUploadSession & { arrived: boolean }>();
  const calls = { creates: 0, puts: 0, renews: 0, queries: [] as string[][], bases: [] as (number | null)[], gets: 0 };
  let failures = opts.failPuts ?? 0;
  const session = (id: string, size: number): CollectionUploadSession & { arrived: boolean } => ({
    id,
    tenant_id: "default",
    project_id: "default",
    dataset_id: "ds",
    filename: "f",
    content_type: "application/zip",
    size_bytes: size,
    status: "pending",
    upload: { method: "PUT", url: `https://store/${id}`, name: id, expires_at: "2099-01-01T00:00:00Z", headers: {}, max_bytes: size },
    expires_at: "2099-01-01T00:00:00Z",
    created_at: "2026-10-20T00:00:00Z",
    arrived: false,
  });
  const api: CollectionApi = {
    createUpload: async (body) => {
      calls.creates += 1;
      const s = session(`upl_${++seq}`, body.size_bytes);
      sessions.set(s.id, s);
      return s;
    },
    getUpload: async (id) => {
      calls.gets += 1;
      const s = sessions.get(id);
      if (!s) throw new (await import("@/api/client")).ApiError(404, null);
      return { ...s, received: s.arrived };
    },
    renewUpload: async (id) => {
      calls.renews += 1;
      return { ...sessions.get(id)!, received: false };
    },
    findMissing: async (shas, base): Promise<BlobMissing> => {
      calls.queries.push(shas);
      calls.bases.push(base);
      const missing = opts.missing ? opts.missing(shas) : shas;
      return { base_version: base ?? 3, missing, present: shas.length - missing.length };
    },
    put: async (s, blob, onProgress) => {
      calls.puts += 1;
      if (failures > 0) {
        failures -= 1;
        throw new UploadError("upload failed with HTTP 403", opts.status === undefined ? 403 : opts.status);
      }
      onProgress({ loaded: blob.size, total: blob.size });
      sessions.get(s.id)!.arrived = true;
    },
  };
  return { api, calls, sessions };
}

async function smallPlan(): Promise<CollectionPlan> {
  return planCollection(folder(), { labelFromFolder: true });
}

describe("uploadCollection", () => {
  it("uploads the manifest and the parts of the new images, then resumes without sending them again", async () => {
    const plan = await smallPlan();
    const present = plan.images[0]!.sha256;
    const { api, calls } = fakeApi({ missing: (shas) => shas.filter((s) => s !== present) });
    const phases: string[] = [];
    const options = collectionOptions(plan, {});
    const res = await uploadCollection(api, { datasetId: "ds", plan, options, baseVersion: null, partBytes: 40, onPhase: (p) => phases.push(p.phase) });
    expect(res.newImages).toBe(3);
    expect(res.presentImages).toBe(1);
    expect(res.baseVersion).toBe(3);
    expect(res.parts).toBe(3);
    // the version the server compared with, not "latest" again at create time
    expect(res.source).toEqual({ manifest_upload_id: "upl_1", part_upload_ids: ["upl_2", "upl_3", "upl_4"], base_version: 3, options });
    expect(calls.puts).toBe(4);
    expect(phases).toContain("checking");
    expect(phases).toContain("part");
    expect(res.manifestSha256).toBe(await sha256Hex(enc.encode(manifestCsv(plan))));

    // a reload: the same folder gives the same manifest; nothing is sent again
    const again = await uploadCollection(api, { datasetId: "ds", plan, options, baseVersion: null, partBytes: 40 });
    expect(again.source.part_upload_ids).toEqual(res.source.part_upload_ids);
    expect(again.skippedParts).toBe(3);
    expect(calls.puts).toBe(4);
    expect(calls.creates).toBe(4);
  });

  it("renews the URL after a 403 and tries a part at most 3 times", async () => {
    const plan = await smallPlan();
    const one = fakeApi({ failPuts: 1 });
    await uploadCollection(one.api, { datasetId: "ds-a", plan, options: {}, baseVersion: 0 });
    expect(one.calls.renews).toBe(1);
    expect(one.calls.queries).toEqual([]); // base 0: every image is new, nothing to ask
    const always = fakeApi({ failPuts: 99, status: 500 });
    await expect(uploadCollection(always.api, { datasetId: "ds-b", plan, options: {}, baseVersion: 0 })).rejects.toThrow(/HTTP 403/);
    expect(always.calls.puts).toBe(3);
    expect(always.calls.renews).toBe(0);
  });

  it("asks for missing images in chunks of MAX_BLOB_QUERY", async () => {
    const shas = fakeImages(MAX_BLOB_QUERY + 2, 1).map((i) => i.sha256);
    const { api, calls } = fakeApi({ missing: (s) => s.slice(0, 1) });
    const res = await findMissing(api, [...shas, shas[0]!], null);
    expect(calls.queries.map((q) => q.length)).toEqual([MAX_BLOB_QUERY, 2]);
    expect(res.missing.size).toBe(2);
    expect(res.present).toBe(MAX_BLOB_QUERY);
    // the first answer resolves "latest" (v3); the next chunk asks v3, not "latest" again
    expect(calls.bases).toEqual([null, 3]);
    expect(res.base).toBe(3);
  });

  it("pins no base when the dataset has no collection version yet", async () => {
    const shas = fakeImages(MAX_BLOB_QUERY + 1, 1).map((i) => i.sha256);
    const { api, calls } = fakeApi();
    api.findMissing = async (chunk, base) => {
      calls.bases.push(base);
      return { base_version: null, missing: chunk, present: 0 };
    };
    const res = await findMissing(api, shas, null);
    expect(calls.bases).toEqual([null, 0]);
    expect(res.base).toBeNull();
    const plan = await smallPlan();
    const up = await uploadCollection(api, { datasetId: "ds", plan, options: {}, baseVersion: null });
    expect(up.source.base_version).toBe(0); // every image is in the parts
  });

  it("gives the version create a key that is stable for the same uploads", async () => {
    const source = { manifest_upload_id: "upl_1", part_upload_ids: ["upl_2", "upl_3"] };
    const key = await collectionKey("ds", source);
    expect(key).toMatch(/^collection-[0-9a-f]{48}$/);
    expect(await collectionKey("ds", { ...source })).toBe(key); // "Try again" replays the create
    expect(await collectionKey("ds", { ...source, part_upload_ids: ["upl_2"] })).not.toBe(key);
    expect(await collectionKey("ds2", source)).not.toBe(key);
  });
});

describe("collectionOf", () => {
  it("reads the collection info of a version and ignores other layouts", () => {
    const base = { id: "dv", dataset_id: "ds", version: 2, layout: "dataset" } as never;
    expect(collectionOf(base)).toBeNull();
    expect(isCollectionVersion(base)).toBe(false);
    const v = { ...(base as object), layout: "collection", collection: { n_images: 6, packs: [], classes: [{ label: "a", count: 3 }, { bad: 1 }], size: [16, 16] } } as never;
    expect(isCollectionVersion(v)).toBe(true);
    expect(collectionOf(v)).toMatchObject({ n_images: 6, classes: [{ label: "a", count: 3 }], size: [16, 16], splits: {}, other_classes: 0, base_version: null });
  });
});

describe("validateCollectionDraft", () => {
  it("needs a manifest for column options and both sides of a size", () => {
    expect(validateCollectionDraft(DEFAULT_COLLECTION_DRAFT, false)).toEqual([]);
    const fields = (d: Partial<typeof DEFAULT_COLLECTION_DRAFT>, manifest = false) => validateCollectionDraft({ ...DEFAULT_COLLECTION_DRAFT, ...d }, manifest).map((p) => p.field);
    expect(fields({ labelFrom: "column" })).toEqual(["manifest"]);
    expect(fields({ labelFrom: "column", labelColumn: "" }, true)).toEqual(["labelColumn"]);
    expect(fields({ splitColumn: "fold" })).toEqual(["splitColumn"]);
    expect(fields({ splitColumn: "fold" }, true)).toEqual([]);
    expect(fields({ height: "32" })).toEqual(["width"]);
    expect(fields({ height: "0", width: "5000" })).toEqual(["height", "width"]);
    expect(draftSize({ ...DEFAULT_COLLECTION_DRAFT, height: "32", width: "24" })).toEqual([32, 24]);
    expect(draftSize(DEFAULT_COLLECTION_DRAFT)).toBeNull();
  });
});
