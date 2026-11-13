import { describe, expect, it } from "vitest";

import { fuzzyScore, matchText, rankRows } from "./rank";

const score = (q: string, title: string, id?: string) => matchText(q, title, id)?.score ?? 0;

describe("matchText", () => {
  it("orders exact id, title prefix, word start, substring and subsequence", () => {
    const id = score("ds_01abcdef", "Some other name", "ds_01abcdef");
    const prefix = score("ad", "Adstock geo");
    const word = score("ad", "Geo adstock");
    const substring = score("ad", "Broadcast");
    const subsequence = score("ad", "Abide");
    expect(id).toBeGreaterThan(prefix);
    expect(prefix).toBeGreaterThan(word);
    expect(word).toBeGreaterThan(substring);
    expect(substring).toBeGreaterThan(subsequence);
    expect(subsequence).toBeGreaterThan(0);
  });

  it("puts a match of all words before a match of some words", () => {
    const all = score("geo study", "Study of geo");
    const partial = score("geo study", "Geo adstock");
    expect(all).toBeGreaterThan(partial);
    expect(partial).toBeGreaterThan(0);
  });

  it("answers null when nothing matches or the query is empty", () => {
    expect(matchText("xyz", "Runs")).toBeNull();
    expect(matchText("   ", "Runs")).toBeNull();
    // letters that are far apart are not a match
    expect(matchText("rs", "Rhat diagnostics")).toBeNull();
  });

  it("gives the ranges of the matched text, merged and in order", () => {
    expect(matchText("media", "eu_media_daily")?.ranges).toEqual([[3, 8]]);
    expect(matchText("daily eu", "eu_media_daily")?.ranges).toEqual([
      [0, 2],
      [9, 14],
    ]);
    expect(matchText("dts", "Datasets")?.ranges).toEqual([
      [0, 1],
      [2, 3],
      [4, 5],
    ]);
  });

  it("ignores case and extra spaces", () => {
    expect(matchText("  GEO   Adstock ", "Geo adstock study")?.ranges).toEqual([[0, 11]]);
  });
});

describe("rankRows", () => {
  it("sorts by score, then by the newest, then by the alphabet", () => {
    const rows = [
      { title: "Fit b", at: "2026-01-01T00:00:00Z" },
      { title: "Fit a", at: "2026-01-01T00:00:00Z" },
      { title: "Fit c", at: "2026-02-01T00:00:00Z" },
      { title: "Outfit", at: "2026-03-01T00:00:00Z" },
      { title: "No date fit" },
      { title: "Other" },
    ];
    expect(rankRows("fit", rows).map((r) => r.row.title)).toEqual(["Fit c", "Fit a", "Fit b", "No date fit", "Outfit"]);
  });

  it("keeps at most `limit` rows and tells how many matched", () => {
    const rows = Array.from({ length: 9 }, (_, i) => ({ title: `cap ${i}` }));
    const ranked = rankRows("cap", rows, { limit: 5 });
    expect(ranked).toHaveLength(5);
    expect(ranked[0]?.ranges).toEqual([[0, 3]]);
  });

  it("can refuse loose matches", () => {
    expect(rankRows("dts", [{ title: "Datasets" }])).toHaveLength(1);
    expect(rankRows("dts", [{ title: "Datasets" }], { loose: false })).toHaveLength(0);
  });

  it("finds a row by its other text, after the rows found by title", () => {
    const rows = [{ title: "Seasonal ARIMA", alt: "ts.sarimax" }, { title: "Sarimax notes" }, { title: "Other", alt: "core.resample" }];
    const ranked = rankRows("sarimax", rows);
    expect(ranked.map((r) => r.row.title)).toEqual(["Sarimax notes", "Seasonal ARIMA"]);
    expect(ranked[1]?.ranges).toEqual([]);
  });

  it("puts the row with the exact id first", () => {
    const rows = [{ title: "run_01abcdef copy" }, { title: "Nightly", id: "run_01abcdef" }];
    expect(rankRows("run_01abcdef", rows)[0]?.row.title).toBe("Nightly");
  });
});

describe("fuzzyScore", () => {
  it("keeps the v0.20 behaviour", () => {
    expect(fuzzyScore("Datasets", "")).toBe(1);
    expect(fuzzyScore("Datasets", "data")).toBe(100);
    expect(fuzzyScore("Datasets", "sets")).toBe(96);
    expect(fuzzyScore("Datasets", "dts")).toBe(10);
    expect(fuzzyScore("Runs", "xyz")).toBe(0);
  });
});
