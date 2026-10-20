import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";
import type { RunViewQuery } from "@/api/types";

import { metricColumn, canonicalColumn } from "./column-picker";
import { newFilterError } from "./filter-bar";
import { compactSearch, filtersFromSearch, hasRunFilters, sameViewQuery, searchFromViewQuery, validateRunsSearch, viewQueryFromSearch, type RunsSearch } from "./run-search";
import { columnText, currentOrder, nextOrder, searchProblem, toggleSelection, trackedColumns } from "./runs-table";

describe("runs search in the URL", () => {
  it("keeps valid values and drops the others", () => {
    const s = validateRunsSearch({
      state: "succeeded",
      kind: "nope",
      experiment: "exp_1",
      name: "  ",
      f: ["metric.rmse < 3", "metric.rmse < 3", 7, ""],
      c: "metric.rmse",
      o: "-metric.rmse",
      view: "rv 1",
      t: ["team:growth"],
    });
    expect(s).toMatchObject({ state: "succeeded", kind: undefined, experiment: "exp_1", name: undefined, f: ["metric.rmse < 3"], c: ["metric.rmse"], o: "-metric.rmse", view: undefined, t: ["team:growth"] });
    expect(validateRunsSearch({ f: Array.from({ length: 20 }, (_, i) => `metric.a < ${i}`) }).f).toHaveLength(8);
    expect(validateRunsSearch({ c: Array.from({ length: 20 }, (_, i) => `param.p${i}`) }).c).toHaveLength(16);
  });

  it("leaves empty keys out, so /runs stays /runs", () => {
    expect(compactSearch({ state: undefined, f: [], name: "", o: "name" })).toEqual({ o: "name" });
  });

  it("maps the search to the GET /v1/runs filters (the experiment page fixes the experiment)", () => {
    const s: RunsSearch = { state: "failed", experiment: "exp_a", name: " grid ", f: ["tag.team = growth"], c: ["metric.rmse"], o: "-created_at", t: ["team"] };
    expect(filtersFromSearch(s)).toEqual({ state: "failed", experiment_id: "exp_a", name: "grid", tag: ["team"], filter: ["tag.team = growth"], order_by: "-created_at", column: ["metric.rmse"] });
    expect(filtersFromSearch({ experiment: "exp_a" }, "exp_b")).toEqual({ experiment_id: "exp_b" });
  });

  it("round-trips a saved view's query through the URL", () => {
    const q: RunViewQuery = {
      kind: "fit",
      state: "succeeded",
      name: "adstock",
      experiment_id: "exp_geo",
      tags: ["team:growth"],
      filters: ["metric.rmse < 1000"],
      order_by: "metric.loss{split=val}.min",
      columns: ["metric.loss{split=val}.min", "param.settings.adstock.l_max"],
      spec_id: "spc_1",
      parent_run_id: null,
    };
    const search = searchFromViewQuery(q, "rv_1");
    expect(search).toEqual({
      state: "succeeded",
      kind: "fit",
      experiment: "exp_geo",
      name: "adstock",
      f: ["metric.rmse < 1000"],
      c: ["metric.loss{split=val}.min", "param.settings.adstock.l_max"],
      o: "metric.loss{split=val}.min",
      view: "rv_1",
      t: ["team:growth"],
      spec: "spc_1",
    });
    expect(sameViewQuery(viewQueryFromSearch(search), q)).toBe(true);
    // a change of the table makes the view "changed"
    expect(sameViewQuery(viewQueryFromSearch({ ...search, o: "-metric.rmse" }), q)).toBe(false);
  });

  it("keeps the experiment out of the URL on the experiment page, but in the saved view", () => {
    const q: RunViewQuery = { experiment_id: "exp_geo", columns: ["metric.mape"], filters: [], tags: [] };
    const search = searchFromViewQuery(q, "rv_2", "exp_geo");
    expect(search).toEqual({ c: ["metric.mape"], view: "rv_2" });
    expect(viewQueryFromSearch(search, "exp_geo")).toMatchObject({ experiment_id: "exp_geo", columns: ["metric.mape"] });
    expect(sameViewQuery(viewQueryFromSearch(search, "exp_geo"), q)).toBe(true);
  });

  it("knows when the search narrows the runs", () => {
    expect(hasRunFilters({ o: "name", c: ["metric.rmse"] })).toBe(false);
    expect(hasRunFilters({ f: ["name ~ a"] })).toBe(true);
    expect(hasRunFilters({ experiment: "exp_a" })).toBe(true);
    expect(hasRunFilters({ experiment: "exp_a" }, "exp_a")).toBe(false);
  });
});

describe("runs table helpers", () => {
  it("parses tracked columns to their canonical keys", () => {
    const cols = trackedColumns(["metric.loss{split=val,fold=1}.min", "param.lr", "bad ref", "tag.team"]);
    expect(cols.map((c) => [c.key, c.label])).toEqual([
      ["metric.loss{fold=1,split=val}.min", "loss{fold=1,split=val}.min"],
      ["param.lr", "lr"],
      ["tag.team", "team"],
    ]);
  });

  it("cycles the order of a column: ascending, descending, none", () => {
    expect(nextOrder(undefined, "metric.rmse")).toBe("metric.rmse");
    expect(nextOrder("metric.rmse", "metric.rmse")).toBe("-metric.rmse");
    expect(nextOrder("-metric.rmse", "metric.rmse")).toBeUndefined();
    expect(nextOrder("-metric.rmse", "name")).toBe("name");
    expect(currentOrder(" -metric.loss{b=1,a=2} ")).toEqual({ key: "metric.loss{a=2,b=1}", descending: true });
    expect(currentOrder("nope")).toBeNull();
  });

  it("shows column values as text", () => {
    expect(columnText(412.5)).toBe("412.5");
    expect(columnText("<b>x</b>")).toBe("<b>x</b>");
    expect(columnText(null)).toBe("—");
    expect(columnText(undefined)).toBe("—");
  });

  it("keeps the selection in click order", () => {
    expect(toggleSelection(["a"], "b", true)).toEqual(["a", "b"]);
    expect(toggleSelection(["a", "b"], "a", false)).toEqual(["b"]);
    expect(toggleSelection(["a"], "a", true)).toEqual(["a"]);
  });

  it("shows the server's 422 message under the filter bar", () => {
    const err = new ApiError(422, { type: "x", title: "Invalid request", status: 422, code: "SCHEMA_INVALID", detail: "filter 'x' has no operator" });
    expect(searchProblem(err)).toBe("filter 'x' has no operator");
    expect(searchProblem(new ApiError(500, null))).toBeNull();
    expect(searchProblem(new Error("x"))).toBeNull();
  });

  it("checks a new filter before it goes to the URL", () => {
    expect(newFilterError("metric.rmse < 3", [])).toBeNull();
    expect(newFilterError("  ", [])).toBe("Type a filter.");
    expect(newFilterError("metric.rmse ~ 3", [])).toMatch(/^The filter is not correct: filter 'metric.rmse ~ 3': a metric filter takes one of/);
    expect(newFilterError("name ~ a", ["name ~ a"])).toBe("The table already uses this filter.");
    expect(newFilterError("name ~ z", Array.from({ length: 8 }, (_, i) => `name ~ ${i}`))).toMatch(/8 filters maximum/);
  });

  it("builds metric columns from the run keys", () => {
    expect(metricColumn({ key: "loss", labels: { split: "val", fold: "2" } }, "min")).toBe("metric.loss{fold=2,split=val}.min");
    expect(metricColumn({ key: "rmse" }, "last")).toBe("metric.rmse");
    expect(canonicalColumn(" metric.rmse.last ")).toBe("metric.rmse");
    expect(canonicalColumn("tag.mapi.workflow")).toBeNull();
  });
});
