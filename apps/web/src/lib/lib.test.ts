import { tableFromArrays, tableToIPC } from "apache-arrow";
import { describe, expect, it, vi } from "vitest";

import { stripAnsi } from "./ansi";
import { ArtifactError, fetchArtifactBytes, fetchArtifactJson } from "./artifacts";
import { crc32c, crc32cBase64 } from "./crc32c";
import { detectDelimiter, guessColumn, parseCsv } from "./csv";
import { formatBytes, formatDuration, formatTimestamp, shortId } from "./format";
import { formatPointer, parsePointer, pointerWithin } from "./pointer";
import { isCancellable, isTerminal, nextResourceClass } from "./run-state";
import { distinctOn, pivotStats } from "./summary";
import { parsePreprocess, parsePreview, parseProfile, parseTreeIndex, ViewFormatError } from "./views";

describe("stripAnsi", () => {
  it("removes CSI/OSC sequences, control and bidi characters, keeps text and tabs", () => {
    const raw = "\u001b[31mred\u001b[0m \u001b]8;;https://evil.test\u0007link\u001b]8;;\u0007\tok\u0007‮evil\r\nnext";
    expect(stripAnsi(raw)).toBe("red link\tokevil\nnext");
  });

  it("leaves markup untouched (it is rendered as text, never parsed)", () => {
    expect(stripAnsi("<img src=x onerror=alert(1)>")).toBe("<img src=x onerror=alert(1)>");
  });
});

describe("crc32c", () => {
  it("matches the Castagnoli check value and the GCS base64 form", () => {
    const bytes = new TextEncoder().encode("123456789");
    expect(crc32c(bytes)).toBe(0xe3069283);
    expect(crc32cBase64(bytes)).toBe("4waSgw==");
    expect(crc32cBase64(new Uint8Array())).toBe("AAAAAA==");
  });
});

describe("fetchArtifact*", () => {
  const body = new TextEncoder().encode('{"a":1}');
  const link = { name: "x.json", url: "http://127.0.0.1:4443/x", size: body.length, crc32c: crc32cBase64(body) };
  const ok = vi.fn(async () => new Response(body));

  it("verifies size and checksum", async () => {
    await expect(fetchArtifactJson(link, { fetchImpl: ok as unknown as typeof fetch })).resolves.toEqual({ a: 1 });
    await expect(fetchArtifactBytes({ ...link, crc32c: "AAAAAA==" }, { fetchImpl: ok as unknown as typeof fetch })).rejects.toThrow(
      /checksum mismatch/,
    );
    await expect(fetchArtifactBytes({ ...link, size: 3 }, { fetchImpl: ok as unknown as typeof fetch })).rejects.toThrow(ArtifactError);
  });

  it("refuses oversized artifacts before downloading", async () => {
    const f = vi.fn();
    await expect(fetchArtifactBytes({ ...link, size: 10 }, { maxBytes: 5, fetchImpl: f as unknown as typeof fetch })).rejects.toThrow(/too large/);
    expect(f).not.toHaveBeenCalled();
  });

  it("surfaces HTTP errors", async () => {
    const f = vi.fn(async () => new Response("no", { status: 403 }));
    await expect(fetchArtifactBytes(link, { fetchImpl: f as unknown as typeof fetch })).rejects.toThrow(/HTTP 403/);
  });

  it("downloads binary Arrow payloads intact", async () => {
    const bytes = tableToIPC(tableFromArrays({ v: Float64Array.from([1, 2]) }), "stream");
    const f = vi.fn(async () => new Response(bytes as unknown as BodyInit));
    const got = await fetchArtifactBytes({ name: "a.arrow", url: "u", size: bytes.length, crc32c: crc32cBase64(bytes) }, { fetchImpl: f as unknown as typeof fetch });
    expect(got).toEqual(bytes);
  });
});

describe("csv", () => {
  it("parses quoted fields, escaped quotes, CRLF and a BOM", () => {
    const p = parseCsv('﻿date,market,"note, text"\r\n2026-01-01,de,"say ""hi"""\r\n2026-01-02,fr,\r\n');
    expect(p.header).toEqual(["date", "market", "note, text"]);
    expect(p.rows).toEqual([
      ["2026-01-01", "de", 'say "hi"'],
      ["2026-01-02", "fr", ""],
    ]);
    expect(p.truncated).toBe(false);
  });

  it("detects delimiters and truncation", () => {
    expect(detectDelimiter("a;b;c")).toBe(";");
    expect(detectDelimiter("a\tb")).toBe("\t");
    const p = parseCsv("a,b\n1,2\n3,4\n5,6\n", 2);
    expect(p.rows).toHaveLength(2);
    expect(p.truncated).toBe(true);
  });

  it("guesses dims and vars from names and samples", () => {
    expect(guessColumn("date", ["2026-01-01"])).toEqual({ as: "dim", kind: "time" });
    expect(guessColumn("market", ["de", "fr"])).toEqual({ as: "dim", kind: "categorical" });
    expect(guessColumn("spend", ["1.5", "2e3"])).toEqual({ as: "var" });
  });
});

describe("pointer", () => {
  it("round-trips RFC 6901 escapes", () => {
    expect(parsePointer("/a~1b/c~0d/0")).toEqual(["a/b", "c~d", "0"]);
    expect(formatPointer(["a/b", "c~d", 0])).toBe("/a~1b/c~0d/0");
    expect(parsePointer("")).toEqual([]);
    expect(() => parsePointer("a")).toThrow();
    expect(pointerWithin("/pipeline/2/with", "/pipeline/2")).toBe(true);
    expect(pointerWithin("/pipeline/20", "/pipeline/2")).toBe(false);
  });
});

describe("run state", () => {
  it("matches the contract's terminal set and class ladder", () => {
    expect(["rejected", "succeeded", "partial", "failed", "cancelled"].every((s) => isTerminal(s as never))).toBe(true);
    expect(isTerminal("stalled")).toBe(false);
    expect(isCancellable("cancelling")).toBe(false);
    expect(isCancellable("running")).toBe(true);
    expect(isCancellable("finalizing")).toBe(false);
    expect(nextResourceClass("S")).toBe("M");
    expect(nextResourceClass("M")).toBe("L");
    expect(nextResourceClass("L")).toBeNull();
  });
});

describe("format", () => {
  it("formats bytes, durations and ids", () => {
    expect(formatBytes(1536)).toBe("1.5 KiB");
    expect(formatDuration(3725)).toBe("1h 2m");
    expect(shortId("run_01jaf3q2x8m9v7k5h4g3f2e1d0")).toBe("run_…f2e1d0");
    expect(shortId(`sha256:${"ab".repeat(32)}`)).toBe("sha256:abababababab");
  });
});

describe("view parsers", () => {
  it("parses a preview and rejects ragged rows or unknown formats", () => {
    const ok = parsePreview({ format: "mapi.preview/v1", columns: [{ name: "a", dtype: "f8", role: "var" }], rows: [[1], [null]] });
    expect(ok.rows).toEqual([[1], [null]]);
    expect(() => parsePreview({ format: "mapi.preview/v1", columns: [{ name: "a" }], rows: [[1, 2]] })).toThrow(ViewFormatError);
    expect(() => parsePreview({ format: "other/v9" })).toThrow(/unsupported view format/);
  });

  it("parses a profile and drops an inconsistent missingness grid", () => {
    const p = parseProfile({
      format: "mapi.profile/v1",
      vars: { y: { dtype: "float64", count: 10, missing: 2, mean: 1.5 } },
      missingness: { along: "week", labels: ["y"], coords: ["w1", "w2"], fraction: [[0, 2]] },
    });
    expect(p.vars.y?.missing).toBe(2);
    expect(p.missingness?.fraction).toEqual([[0, 1]]);
    const bad = parseProfile({ format: "mapi.profile/v1", vars: {}, missingness: { along: "w", labels: ["y"], coords: ["a"], fraction: [[0, 1]] } });
    expect(bad.missingness).toBeNull();
  });

  it("parses a tree index into sorted groups", () => {
    const t = parseTreeIndex({ format: "mapi.tree-index/v1", groups: { posterior: { dims: { chain: 4 }, vars: { b: { dims: ["chain"], shape: [4], dtype: "f8" } } }, "/a": {} } });
    expect(t.groups.map((g) => g.path)).toEqual(["/a", "/posterior"]);
    expect(t.groups[1]?.vars.b?.shape).toEqual([4]);
  });

  it("parses preprocessing state arrays (1-D and 2-D)", () => {
    const p = parsePreprocess({
      format: "mapi.preprocess/v1",
      steps: [{ id: "s", arrays: [{ name: "a", dims: ["x"], values: [1, "bad", 3] }, { name: "b", dims: ["r", "c"], values: [[1, 2]] }] }],
    });
    expect(p.steps[0]?.arrays[0]?.values).toEqual([[1, null, 3]]);
    expect(p.steps[0]?.arrays[1]?.values).toEqual([[1, 2]]);
    expect(p.steps[0]?.omitted).toEqual([]);
  });

  it("keeps the runner's omitted arrays, truncation flag and units", () => {
    const p = parsePreprocess({
      format: "mapi.preprocess/v1",
      steps: [
        {
          id: "lags",
          use: "core.lag_tensor@1.0.0",
          kind: "feature",
          stateful: false,
          learned: { l_max: 4 },
          learned_truncated: true,
          arrays: [{ name: "level", dims: [], coords: {}, values: [2.5], units: "EUR" }],
          omitted: ["window", 7],
        },
      ],
    });
    const s = p.steps[0];
    expect(s?.omitted).toEqual(["window"]);
    expect(s?.learnedTruncated).toBe(true);
    expect(s?.arrays[0]).toMatchObject({ name: "level", dims: [], values: [[2.5]], units: "EUR" });
  });
});

describe("score summaries", () => {
  const table = {
    columns: [
      { name: "stat", type: "Utf8" },
      { name: "market", type: "Utf8" },
      { name: "date", type: "Timestamp<MILLISECOND>" },
      { name: "component", type: "Utf8" },
      { name: "y_hat", type: "Float64" },
    ],
    rows: [
      { stat: "mean", market: "NL", date: "2023-08-06", component: "tv", y_hat: 10 },
      { stat: "mean", market: "NL", date: "2023-08-06", component: "search", y_hat: 10 },
      { stat: "hdi_90_lower", market: "NL", date: "2023-08-06", component: "tv", y_hat: 8 },
      { stat: "hdi_90_lower", market: "NL", date: "2023-08-06", component: "search", y_hat: 8 },
    ],
    totalRows: 4,
    truncated: false,
  };

  it("pivots the stat column into one column per variable and stat", () => {
    const out = pivotStats(table);
    expect(out.columns.map((c) => c.name)).toEqual(["market", "date", "component", "y_hat_mean", "y_hat_hdi_90_lower"]);
    expect(out.rows).toEqual([
      { market: "NL", date: "2023-08-06", component: "tv", y_hat_mean: 10, y_hat_hdi_90_lower: 8 },
      { market: "NL", date: "2023-08-06", component: "search", y_hat_mean: 10, y_hat_hdi_90_lower: 8 },
    ]);
    const plain = { ...table, columns: table.columns.filter((c) => c.name !== "stat") };
    expect(pivotStats(plain)).toBe(plain);
  });

  it("keeps a numeric dim of the view as a key (a grid in cm is not a value)", () => {
    const grid = {
      columns: [
        { name: "stat", type: "Utf8" },
        { name: "run", type: "Utf8" },
        { name: "y_cm", type: "Float64" },
        { name: "x_cm", type: "Float64" },
        { name: "field", type: "Float64" },
      ],
      rows: [
        { stat: "mean", run: "a", y_cm: 1, x_cm: 1, field: 30 },
        { stat: "mean", run: "a", y_cm: 1, x_cm: 2, field: 31 },
        { stat: "sd", run: "a", y_cm: 1, x_cm: 1, field: 0.1 },
        { stat: "sd", run: "a", y_cm: 1, x_cm: 2, field: 0.2 },
      ],
      totalRows: 4,
      truncated: false,
      dims: ["stat", "run", "y_cm", "x_cm"],
    };
    const out = pivotStats(grid);
    expect(out.columns.map((c) => c.name)).toEqual(["run", "y_cm", "x_cm", "field_mean", "field_sd"]);
    expect(out.rows).toEqual([
      { run: "a", y_cm: 1, x_cm: 1, field_mean: 30, field_sd: 0.1 },
      { run: "a", y_cm: 1, x_cm: 2, field_mean: 31, field_sd: 0.2 },
    ]);
    // without the metadata, a numeric column is a value (the old views)
    const { dims: _dims, ...old } = grid;
    expect(pivotStats(old).rows).toHaveLength(1);
  });

  it("drops rows a broadcast variable repeats", () => {
    const rows = pivotStats(table).rows;
    expect(distinctOn(rows, ["date", "market", "y_hat_mean", undefined])).toHaveLength(1);
    expect(distinctOn(rows, ["component"])).toHaveLength(2);
  });
});

describe("formatTimestamp", () => {
  it("drops a midnight time and keeps others", () => {
    expect(formatTimestamp("2023-08-06T00:00:00.000Z")).toBe("2023-08-06");
    expect(formatTimestamp("2023-08-06T13:05:00Z")).toBe("2023-08-06 13:05:00");
    expect(formatTimestamp("not a date")).toBe("not a date");
  });
});
