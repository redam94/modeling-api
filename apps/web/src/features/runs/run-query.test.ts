import { describe, expect, it } from "vitest";

import { grammarError, labelsKey, orderText, parseRunFilter, parseRunOrder, parseRunRef, pyFloat, pyRepr, refText } from "./run-query";

/**
 * `[kind, input, expected]`: expected is the canonical text (orders with their "-"), a filter's
 * `[text, op, value]`, or `{ error }`. Written from the Python parsers of `mapi_spec.tracking`.
 */
type Vector = [string, string, ...unknown[]];
const LOCAL: Vector[] = [
  ["ref", "name", "name"],
  ["ref", "created_at", "created_at"],
  ["ref", " metric.rmse ", "metric.rmse"],
  ["ref", "metric.loss{split=val,fold=2}.min", "metric.loss{fold=2,split=val}.min"],
  ["ref", "metric.loss{}", "metric.loss"],
  ["ref", "metric.Loss", {"error": "run ref 'metric.Loss': metric key 'Loss' must match ^[a-z][a-z0-9_]{0,63}$ (use labels, not '/' or '.', for qualifiers such as the split or the chain)"}],
  ["ref", "metric.a.b.c", {"error": "run ref 'metric.a.b.c': write a metric as 'metric.KEY', 'metric.KEY{label=value}' or 'metric.KEY.AGG'"}],
  ["ref", "metric.loss.median", {"error": "run ref 'metric.loss.median': aggregate 'median' is not one of last, min, max, count"}],
  ["ref", "metric.loss{split}", {"error": "run ref 'metric.loss{split}': labels 'split': 'split' is not 'key=value'"}],
  ["ref", "metric.loss{a=1,a=2}", {"error": "run ref 'metric.loss{a=1,a=2}': labels 'a=1,a=2': label 'a' is repeated"}],
  ["ref", "metric.loss{A=1}", {"error": "run ref 'metric.loss{A=1}': metric label key 'A' must match ^[a-z][a-z0-9_]{0,31}$"}],
  ["ref", "metric.loss{a=b c}", {"error": "run ref 'metric.loss{a=b c}': metric label value 'b c' (label 'a') must match ^[A-Za-z0-9_.\\-]{1,63}$"}],
  ["ref", "param.fit.draws", "param.fit.draws"],
  ["ref", "param.adstock/l_max", "param.adstock/l_max"],
  ["ref", "param.", {"error": "run ref 'param.': param key '' must match ^[A-Za-z0-9_][A-Za-z0-9_.\\-/]{0,249}$"}],
  ["ref", "tag.team", "tag.team"],
  ["ref", "tag.mapi.workflow", {"error": "run ref 'tag.mapi.workflow': tag key 'mapi.workflow' is reserved (prefix 'mapi.')"}],
  ["ref", "tag.a b", {"error": "run ref 'tag.a b': tag key 'a b' must match ^[A-Za-z0-9_][A-Za-z0-9_.\\-/]{0,127}$"}],
  ["ref", "foo", {"error": "run ref 'foo' must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'"}],
  ["ref", "metric.x{a=1}{b=2}", {"error": "run ref 'metric.x{a=1}{b=2}': write a metric as 'metric.KEY', 'metric.KEY{label=value}' or 'metric.KEY.AGG'"}],
  ["ref", "metric.ab\n{x=1}", "metric.ab\n{x=1}"],
  ["filter", "metric.rmse < 0.5", "metric.rmse", "<", 0.5],
  ["filter", "metric.rmse<0.5", "metric.rmse", "<", 0.5],
  ["filter", "metric.rmse = abc", {"error": "filter 'metric.rmse = abc': 'abc' is not a number"}],
  ["filter", "metric.rmse ~ 3", {"error": "filter 'metric.rmse ~ 3': a metric filter takes one of <= >= != < > ="}],
  ["filter", "param.lr = 0.01", "param.lr", "=", "0.01"],
  ["filter", "param.lr >= 1_000", "param.lr", ">=", 1000.0],
  ["filter", "param.lr >= 1e3", "param.lr", ">=", 1000.0],
  ["filter", "param.lr < inf", {"error": "filter 'param.lr < inf': the value must be a finite number"}],
  ["filter", "param.lr > nan", {"error": "filter 'param.lr > nan': the value must be a finite number"}],
  ["filter", "param.kind ~ hill", "param.kind", "~", "hill"],
  ["filter", "tag.team = growth", "tag.team", "=", "growth"],
  ["filter", "tag.team < 3", {"error": "filter 'tag.team < 3': a tag filter takes one of != = ~"}],
  ["filter", "name ~ grid", "name", "~", "grid"],
  ["filter", "name != x", {"error": "filter 'name != x': a name filter takes one of = ~"}],
  ["filter", "created_at > 2026", {"error": "filter 'created_at > 2026': 'created_at' is valid only in order_by and column"}],
  ["filter", "metric.rmse", {"error": "filter 'metric.rmse' has no operator (one of <= >= != < > = ~)"}],
  ["filter", "metric.rmse ! 3", {"error": "filter 'metric.rmse ! 3': the operator is not one of <= >= != < > = ~"}],
  ["filter", "metric.loss{split=val} <= .5", "metric.loss{split=val}", "<=", 0.5],
  ["filter", "metric.rmse < ", {"error": "filter 'metric.rmse < ': '' is not a number"}],
  ["filter", "nope = 3", {"error": "filter 'nope = 3': run ref 'nope ' must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'"}],
  ["filter", "param.x = it's", "param.x", "=", "it's"],
  ["filter", "metric.rmse < 1.", "metric.rmse", "<", 1.0],
  ["filter", "metric.rmse > -.5e-3", "metric.rmse", ">", -0.0005],
  ["filter", "metric.rmse < 1__0", {"error": "filter 'metric.rmse < 1__0': '1__0' is not a number"}],
  ["filter", "param.a.b <= +7", "param.a.b", "<=", 7.0],
  ["filter", "tag.x = \u00a0a\u00a0", "tag.x", "=", "a"],
  ["order", "-metric.rmse", "-metric.rmse"],
  ["order", "metric.rmse.max", "metric.rmse.max"],
  ["order", " -name ", "-name"],
  ["order", "-", {"error": "order_by '-': run ref '' must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'"}],
  ["order", "--name", {"error": "order_by '--name': run ref '-name' must be 'metric.KEY', 'param.KEY', 'tag.KEY', 'name' or 'created_at'"}],
  ["order", "created_at", "created_at"],
  ["order", "-tag.x y", {"error": "order_by '-tag.x y': run ref 'tag.x y': tag key 'x y' must match ^[A-Za-z0-9_][A-Za-z0-9_.\\-/]{0,127}$"}],
];

function run(kind: string, input: string): unknown[] {
  if (kind === "ref") return [refText(parseRunRef(input))];
  if (kind === "order") return [orderText(parseRunOrder(input))];
  const f = parseRunFilter(input);
  return [refText(f.ref), f.op, f.value];
}

function check([kind, input, ...expected]: Vector) {
  const first = expected[0];
  if (first && typeof first === "object" && "error" in first) {
    const parse = kind === "ref" ? parseRunRef : kind === "order" ? parseRunOrder : parseRunFilter;
    expect(grammarError(parse, input), input).toBe((first as { error: string }).error);
  } else {
    expect(run(kind, input), input).toEqual(expected);
  }
}

describe("run-query grammar (vectors written from the Python parsers)", () => {
  it.each(LOCAL.map((v) => [`${v[0]} ${JSON.stringify(v[1])}`, v] as const))("%s", (_name, v) => check(v));
});

/**
 * The shared vectors `run-query.vectors.gen.json` (U1 regenerates them from the Python parsers
 * with `MAPI_UPDATE_SNAPSHOTS=1`). Each entry names its parser (`ref`, `filter`, `order`), the
 * input and either the parsed result or the error message.
 */
const GEN = import.meta.glob<unknown>("./run-query.vectors.gen.json", { eager: true, import: "default" });

interface GenVector {
  kind?: string;
  parser?: string;
  input: string;
  error?: string | null;
  text?: string;
  ref?: string;
  op?: string;
  value?: string | number;
  descending?: boolean;
}

function genVectors(): GenVector[] {
  const data = Object.values(GEN)[0];
  if (!data) return [];
  const list = Array.isArray(data) ? data : ((data as { vectors?: unknown[] }).vectors ?? []);
  return list as GenVector[];
}

describe.skipIf(genVectors().length === 0)("run-query grammar (shared vectors)", () => {
  it("parses every vector like the Python parser", () => {
    for (const v of genVectors()) {
      const kind = v.kind ?? v.parser ?? "";
      const parse = kind === "ref" ? parseRunRef : kind === "order" || kind === "order_by" ? parseRunOrder : parseRunFilter;
      if (v.error) {
        expect(grammarError(parse, v.input), v.input).toBe(v.error);
        continue;
      }
      expect(grammarError(parse, v.input), v.input).toBeNull();
      const text = v.text ?? v.ref;
      if (kind === "ref" && text !== undefined) expect(refText(parseRunRef(v.input)), v.input).toBe(text);
      if ((kind === "order" || kind === "order_by") && text !== undefined) {
        const o = parseRunOrder(v.input);
        expect(refText(o.ref), v.input).toBe(text);
        if (v.descending !== undefined) expect(o.descending, v.input).toBe(v.descending);
      }
      if (kind === "filter") {
        const f = parseRunFilter(v.input);
        if (text !== undefined) expect(refText(f.ref), v.input).toBe(text);
        if (v.op !== undefined) expect(f.op, v.input).toBe(v.op);
        if (v.value !== undefined) expect(f.value, v.input).toBe(v.value);
      }
    }
  });
});

describe("Python helpers", () => {
  it("quotes like repr()", () => {
    expect(pyRepr("abc")).toBe("'abc'");
    expect(pyRepr("it's")).toBe(`"it's"`);
    expect(pyRepr(`a'b"c`)).toBe(`'a\\'b"c'`);
    expect(pyRepr("a\nb\u0001\u00a0")).toBe("'a\\nb\\x01\\xa0'");
  });

  it("reads numbers like float()", () => {
    expect(pyFloat(" 1_000.5 ")).toBe(1000.5);
    expect(pyFloat("1.")).toBe(1);
    expect(pyFloat(".5e1")).toBe(5);
    expect(pyFloat("-Infinity")).toBe(-Infinity);
    expect(pyFloat("0x10")).toBeNull();
    expect(pyFloat("1_")).toBeNull();
    expect(pyFloat("")).toBeNull();
  });

  it("writes the canonical label key", () => {
    expect(labelsKey({ split: "val", fold: "2" })).toBe("fold=2,split=val");
    expect(labelsKey([])).toBe("");
  });
});
