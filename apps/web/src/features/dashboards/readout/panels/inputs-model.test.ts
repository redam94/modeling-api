import { describe, expect, it } from "vitest";

import type { BatchTemplate, ScenarioForm, ScenarioInputDef } from "../../types";
import { batchCell, batchChecks, batchRows, batchToXr, checkRows, checkValue, defaultRow, fieldValue, g, HORIZON_KEY, issuesByPointer, normaliseLabel, rowActualsChecks, rowActualsColumns, rowActualsRows, rowActualsToXr } from "./inputs-model";

const price: ScenarioInputDef = { name: "price", label: "Price", type: "number", min: 1, max: 10, step: 0.5, default: 4, bind: { var: "price" } };
const visits: ScenarioInputDef = { name: "visits", label: "Visits", type: "integer", min: 0, max: 50, default: 3, bind: { var: "visits" } };
const region: ScenarioInputDef = { name: "region", label: "Region", type: "choice", choices: ["North", "South", "1"], default: "North", bind: { var: "region" } };
const start: ScenarioInputDef = { name: "start", label: "Start", type: "date", date_min: "2026-01-01", date_max: "2026-12-31", default: "2026-03-01", bind: { var: "start" } };
const promo: ScenarioInputDef = { name: "promo", label: "Promotion", type: "toggle", default: false, bind: { var: "promo" } };

const form: ScenarioForm = { max_rows: 3, inputs: [price, visits, region, start, promo], outputs: ["y"], horizon: null };

describe("checkValue mirrors dashboard_inputs.check_value", () => {
  it("numbers: type, range, step grid and whole numbers, with python's {:g} numbers", () => {
    expect(checkValue(4.5, price)).toBeNull();
    expect(checkValue("4", price)).toBe("Price must be a number");
    expect(checkValue(Number.NaN, price)).toBe("Price must be a finite number");
    expect(checkValue(11, price)).toBe("Price must be between 1 and 10");
    expect(checkValue(4.2, price)).toBe("Price must be 1 plus a multiple of 0.5");
    expect(checkValue(4.5 + 1e-12, price)).toBeNull(); // within 1e-9 of the range
    expect(checkValue(2.5, visits)).toBe("Visits must be a whole number");
    expect(checkValue(7, visits)).toBeNull();
  });

  it("choices match as normalise_label does; dates, toggles and plain text", () => {
    expect(checkValue(" south ", region)).toBeNull();
    expect(checkValue("1.0", region)).toBeNull();
    expect(checkValue(1, region)).toBeNull();
    expect(checkValue("East", region)).toBe("Region must be one of the choices");
    expect(checkValue(true, region)).toBe("Region must be one of the choices");
    expect(checkValue("<b>North</b>", region)).toBe("Region must be plain text");
    expect(checkValue("x".repeat(129), region)).toBe("Region holds more than 128 characters");
    expect(checkValue("2026-05-01", start)).toBeNull();
    expect(checkValue("2027-01-01", start)).toBe("Start must be between 2026-01-01 and 2026-12-31");
    expect(checkValue("01/05/2026", start)).toBe("Start must be a date as YYYY-MM-DD");
    expect(checkValue(false, promo)).toBeNull();
    expect(checkValue("yes", promo)).toBe("Promotion must be true or false");
  });

  it("g formats like python", () => {
    expect([g(10), g(0.5), g(1e-5), g(1234567), g(2.5e6), g(0)]).toEqual(["10", "0.5", "1e-05", "1.23457e+06", "2.5e+06", "0"]);
    expect(normaliseLabel(" Yes ")).toBe("yes");
    expect(normaliseLabel("2.00")).toBe("2");
  });
});

describe("checkRows", () => {
  it("gives the server's pointers; null takes the default; unknown keys and too many rows are refused", () => {
    const issues = checkRows([{ price: 11, visits: null }, { region: "East", other: 1 }], form);
    expect([...issues.keys()]).toEqual(["/rows/0/price", "/rows/1/region", "/rows/1/other"]);
    expect(issues.get("/rows/1/other")).toBe("'other' is not an input of this dashboard");
    expect(checkRows([defaultRow(form)], form).size).toBe(0);
    expect(checkRows([{}, {}, {}, {}], form).get("/rows")).toBe("send at most 3 rows (got 4)");
    expect(checkRows([], form).get("/rows")).toBe("send at least one row");
  });

  it("checks the periods ahead of a forecast form", () => {
    const f: ScenarioForm = { max_rows: 1, inputs: [], horizon: { dim: "date", min: 1, max: 12, default: 6 } };
    expect(defaultRow(f)).toEqual({ [HORIZON_KEY]: 6 });
    expect(checkRows([{ horizon: 13 }], f).get("/rows/0/horizon")).toBe("the number of periods must be 1 to 12");
    expect(checkRows([{ horizon: 2.5 }], f).get("/rows/0/horizon")).toBe("the number of periods must be a whole number");
  });

  it("field text becomes the value it sends; server issues map by pointer", () => {
    expect(fieldValue("4.5", price)).toBe(4.5);
    expect(fieldValue("", price)).toBeNull();
    expect(fieldValue("abc", price)).toBe("abc");
    expect(issuesByPointer([{ pointer: "/rows/0/price", message: "x" }, { message: "y" }]).get("/rows/0/price")).toBe("x");
  });
});

const template: BatchTemplate = {
  id_column: "case_id",
  max_rows: 3,
  example_csv: "case_id,price,region\ncase1,4,North\n",
  columns: [
    { name: "case_id", role: "id", type: "text" },
    { name: "price", role: "driver", type: "number", min: 1, max: 10 },
    { name: "region", role: "driver", type: "choice", choices: ["North", "South"] },
  ],
};

describe("batch files", () => {
  it("types cells by column and reorders them to the template", () => {
    const rows = batchRows(template, ["region", "case_id", "price"], [["North", "a", "4"], ["", "", ""], ["South", "b", "x"]]);
    expect(rows).toEqual([
      ["a", 4, "North"],
      ["b", "x", "South"],
    ]);
    expect(batchCell("true", { type: "toggle", role: "driver" })).toBe(true);
  });

  it("checks columns, the row cap, ids and values", () => {
    const ok = batchChecks(template, ["case_id", "price", "region"], [["a", 4, "North"]], 1);
    expect(ok.every((c) => c.ok)).toBe(true);
    const missing = batchChecks(template, ["case_id", "price"], [], 1);
    expect(missing[0]).toEqual({ ok: false, text: "The file needs the column region." });
    const extra = batchChecks(template, ["case_id", "price", "region", "secret"], [["a", 4, "North", null]], 1);
    expect(extra[0]?.text).toBe("Remove the column secret. The model does not use it.");
    const many = batchChecks(template, ["case_id", "price", "region"], [], 4);
    expect(many[1]).toEqual({ ok: false, text: "Use 3 rows or fewer. The file has 4." });
    const bad = batchChecks(template, ["case_id", "price", "region"], [["a", 40, "North"], ["a", 4, "East"]], 2);
    expect(bad.find((c) => c.text.startsWith("Each case_id"))?.ok).toBe(false);
    expect(bad.at(-1)?.text).toBe("Fix the values. row 1: price must be between 1 and 10; row 2: region must be one of the choices.");
  });

  it("sends a one-dim table along the id column", () => {
    expect(batchToXr(template, [["a", 4, "North"]])).toEqual({
      coords: { case_id: { dims: ["case_id"], data: ["a"] } },
      data_vars: { price: { dims: ["case_id"], data: [4] }, region: { dims: ["case_id"], data: ["North"] } },
    });
  });
});

describe("row-matched actuals", () => {
  it("needs rows, unique ids and an outcome on each row", () => {
    const cols = ["id", "x", "churned"];
    expect(rowActualsChecks("id", "churned", cols, [["a", 1, "yes"]], 10).every((c) => c.ok)).toBe(true);
    const bad = rowActualsChecks("id", "churned", cols, [["a", 1, "yes"], ["a", 2, null]], 10);
    expect(bad.map((c) => c.ok)).toEqual([true, false, false]);
    expect(rowActualsChecks("id", "churned", cols, [], 10)[0]?.ok).toBe(false);
  });
});

describe("row-matched actuals", () => {
  it("uses the template's own columns and sends one row per case along the id", () => {
    const cols = rowActualsColumns({ example_csv: "store,time,price,y\n", input_table: { columns: [] } });
    expect(cols).toEqual(["store", "time", "price", "y"]);
    const rows = rowActualsRows(cols, "store", ["y", "store", "time", "price"], [["1", "007", "2024-10-13", "18"], ["", "", "", ""], ["0", "s1", "2024-10-13", "12.5"]]);
    expect(rows).toEqual([
      ["007", "2024-10-13", 18, 1],
      ["s1", "2024-10-13", 12.5, 0],
    ]);
    expect(rowActualsToXr(cols, "store", rows)).toEqual({
      coords: { store: { dims: ["store"], data: ["007", "s1"] } },
      data_vars: {
        time: { dims: ["store"], data: ["2024-10-13", "2024-10-13"] },
        price: { dims: ["store"], data: [18, 12.5] },
        y: { dims: ["store"], data: [1, 0] },
      },
    });
  });
});
