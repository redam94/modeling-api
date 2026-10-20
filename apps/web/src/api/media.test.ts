import { createHash } from "node:crypto";
import { inflateSync } from "node:zlib";

import { describe, expect, it, vi } from "vitest";

import { groupMediaKeys, labelSetText, seriesItems, sliderIndex } from "@/features/runs/media-model";
import { crc32cBase64 } from "@/lib/crc32c";
import { thinMedia } from "@/mocks/media";
import { encodeRgbPng, sha256Hex } from "@/mocks/png";

import { cellText, fetchMediaBlob, linkExpired, MediaError, mediaLabel, parseMediaTable, sniffImageType } from "./media";
import type { MediaKeySummary, RunMedia } from "./types";

const enc = new TextEncoder();
const png = encodeRgbPng(2, 2, new Uint8Array([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9]));

function item(bytes: Uint8Array, patch: Partial<RunMedia> = {}): RunMedia {
  return {
    id: "rm_1",
    run_id: "run_1",
    attempt: 1,
    key: "samples",
    labels: {},
    step: 3,
    kind: "image",
    content_type: "image/png",
    size: bytes.length,
    sha256: `sha256:${createHash("sha256").update(bytes).digest("hex")}`,
    crc32c: crc32cBase64(bytes),
    width: 2,
    height: 2,
    caption: null,
    source: "runner",
    created_at: "2026-10-15T09:30:00Z",
    url: "https://storage.example/obj?sig=1",
    expires_at: "2099-01-01T00:00:00Z",
    ...patch,
  };
}

const fetchOf = (bytes: Uint8Array, status = 200) => vi.fn(async () => new Response(bytes as Uint8Array<ArrayBuffer>, { status }));

describe("mock PNG and SHA-256 helpers", () => {
  it("writes a valid PNG and the same SHA-256 as node", () => {
    expect(sniffImageType(png)).toBe("image/png");
    // IDAT holds a zlib stream of filter byte + RGB rows
    const idat = png.subarray(8 + 25 + 8, png.length - 12 - 4);
    expect([...inflateSync(idat)]).toEqual([0, 255, 0, 0, 0, 255, 0, 0, 0, 0, 255, 9, 9, 9]);
    for (const data of [new Uint8Array(), enc.encode("abc"), png, new Uint8Array(200).fill(7)]) {
      expect(sha256Hex(data)).toBe(createHash("sha256").update(data).digest("hex"));
    }
  });
});

describe("media bytes", () => {
  it("sniffs PNG and JPEG signatures only", () => {
    expect(sniffImageType(new Uint8Array([0xff, 0xd8, 0xff, 0xe0]))).toBe("image/jpeg");
    expect(sniffImageType(enc.encode("<svg onload=alert(1)>"))).toBeNull();
    expect(sniffImageType(enc.encode("GIF89a"))).toBeNull();
  });

  it("downloads a checked image as a Blob of its type", async () => {
    const fetchImpl = fetchOf(png);
    const blob = await fetchMediaBlob(item(png), undefined, fetchImpl);
    expect(blob.type).toBe("image/png");
    expect(blob.size).toBe(png.length);
    expect(fetchImpl).toHaveBeenCalledWith("https://storage.example/obj?sig=1", expect.objectContaining({ credentials: "omit", referrerPolicy: "no-referrer" }));
  });

  it("refuses bytes that do not agree with the item", async () => {
    // a different SHA-256 with the same size and CRC32C is a corrupted or substituted object
    await expect(fetchMediaBlob(item(png, { sha256: `sha256:${"0".repeat(64)}` }), undefined, fetchOf(png))).rejects.toThrow(/SHA-256/);
    await expect(fetchMediaBlob(item(png, { size: png.length + 1 }), undefined, fetchOf(png))).rejects.toBeInstanceOf(MediaError);
    // an SVG labelled as a PNG never becomes an image Blob
    const svg = enc.encode("<svg xmlns='http://www.w3.org/2000/svg'/>");
    await expect(fetchMediaBlob(item(svg), undefined, fetchOf(svg))).rejects.toThrow(/is not a PNG image/);
    await expect(fetchMediaBlob(item(png), undefined, fetchOf(png, 403))).rejects.toThrow(/HTTP 403/);
    await expect(fetchMediaBlob(item(png, { url: undefined }), undefined, fetchOf(png))).rejects.toThrow(/no download link/);
  });

  it("downloads a table as JSON", async () => {
    const bytes = enc.encode(JSON.stringify({ columns: ["a"], rows: [[1]] }));
    const blob = await fetchMediaBlob(item(bytes, { kind: "table", content_type: "application/json", width: null, height: null }), undefined, fetchOf(bytes));
    expect(blob.type).toBe("application/json");
  });

  it("knows when a signed link expires", () => {
    const now = Date.parse("2026-10-15T10:00:00Z");
    expect(linkExpired({ url: "u", expires_at: "2026-10-15T10:00:20Z" }, now)).toBe(true);
    expect(linkExpired({ url: "u", expires_at: "2026-10-15T10:05:00Z" }, now)).toBe(false);
    expect(linkExpired({ url: undefined, expires_at: undefined }, now)).toBe(true);
    expect(mediaLabel({ key: "samples", step: 3 })).toBe("samples step 3");
    expect(mediaLabel({ key: "coefficients", step: null })).toBe("coefficients");
  });
});

describe("parseMediaTable", () => {
  const table = (v: unknown) => parseMediaTable(enc.encode(JSON.stringify(v)));

  it("reads columns and rows; cells stay text", () => {
    const t = table({ columns: ["channel", "beta", "note"], rows: [["tv", 0.4, "<script>alert(1)</script>"], ["search", null, true]] });
    expect(t.columns).toEqual(["channel", "beta", "note"]);
    expect(t.rows[0]!.map(cellText)).toEqual(["tv", "0.4", "<script>alert(1)</script>"]);
    expect(t.rows[1]!.map(cellText)).toEqual(["search", "", "true"]);
    expect(table({ columns: ["a"] }).rows).toEqual([]);
  });

  it("refuses tables that break the limits", () => {
    expect(() => parseMediaTable(new Uint8Array([0xff, 0xfe]))).toThrow(/UTF-8 JSON/);
    expect(() => table([1, 2])).toThrow(/JSON object/);
    expect(() => table({ columns: [] })).toThrow(/1 to 64 columns/);
    expect(() => table({ columns: [""] })).toThrow(/column name/);
    expect(() => table({ columns: ["a", "b"], rows: [["x"]] })).toThrow(/Row 1 must have 2 cells/);
    expect(() => table({ columns: ["a"], rows: [[{ x: 1 }]] })).toThrow(/Row 1 has a cell/);
    expect(() => table({ columns: ["a"], rows: [["x".repeat(1001)]] })).toThrow(/Row 1 has a cell/);
    expect(() => table({ columns: ["a"], rows: Array.from({ length: 10_001 }, () => [1]) })).toThrow(/10,000 rows/);
  });
});

describe("media tab model", () => {
  const key = (k: string, labels: Record<string, string>, count = 2): MediaKeySummary => ({ key: k, labels, kind: "image", count, first_step: 0, last_step: 4, last_created_at: "2026-10-15T09:30:00Z" });

  it("groups the media series by key", () => {
    const groups = groupMediaKeys([key("samples", { split: "val" }), key("coefficients", {}, 1), key("samples", { split: "train" }, 3)]);
    expect(groups.map((g) => [g.key, g.count, g.series.map((s) => labelSetText(s.labels))])).toEqual([
      ["coefficients", 1, [""]],
      ["samples", 5, ["split=train", "split=val"]],
    ]);
  });

  it("orders the items of one label set by step and follows the newest", () => {
    const m = (step: number | null, labels: Record<string, string> = {}) => item(png, { id: `rm_${step}`, step, labels });
    const items = seriesItems([m(2), m(0), m(1, { split: "val" }), m(null)], "");
    expect(items.map((x) => x.step)).toEqual([null, 0, 2]);
    expect(sliderIndex(null, 3)).toBe(2);
    expect(sliderIndex(7, 3)).toBe(2);
    expect(sliderIndex(1, 3)).toBe(1);
    expect(sliderIndex(null, 0)).toBe(0);
  });

  it("thins compare media evenly and keeps the last item", () => {
    const items = Array.from({ length: 120 }, (_, i) => i);
    const thinned = thinMedia(items, 50);
    expect(thinned).toHaveLength(50);
    expect(thinned[0]).toBe(0);
    expect(thinned.at(-1)).toBe(119);
  });
});
