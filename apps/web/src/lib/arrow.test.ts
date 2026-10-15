import {
  DateMillisecond,
  Dictionary,
  Int32,
  Int64,
  Table,
  tableFromArrays,
  tableToIPC,
  TimestampMillisecond,
  Utf8,
  vectorFromArray,
} from "apache-arrow";
import { describe, expect, it } from "vitest";

import { ArrowDecodeError, base64ToBytes, decodeArrow } from "./arrow";

describe("decodeArrow", () => {
  it("decodes floats, strings and NaN to plain rows", () => {
    const bytes = tableToIPC(
      tableFromArrays({ x: Float64Array.from([1, 2, Number.NaN]), name: ["a", "b", "c"] }),
      "stream",
    );
    const out = decodeArrow(bytes);
    expect(out.columns.map((c) => c.name)).toEqual(["x", "name"]);
    expect(out.rows).toEqual([
      { x: 1, name: "a" },
      { x: 2, name: "b" },
      { x: null, name: "c" },
    ]);
    expect(out.truncated).toBe(false);
  });

  it("converts int64 to numbers, or to strings beyond 2^53", () => {
    const vec = vectorFromArray([1n, 2n ** 60n], new Int64());
    const out = decodeArrow(tableToIPC(new Table({ n: vec }), "file"));
    expect(out.rows[0]?.n).toBe(1);
    expect(out.rows[1]?.n).toBe((2n ** 60n).toString());
  });

  it("renders timestamps and dates as ISO strings", () => {
    const t = Date.UTC(2026, 9, 15, 9, 30);
    const table = new Table({
      ts: vectorFromArray([t], new TimestampMillisecond()),
      d: vectorFromArray([new Date(Date.UTC(2026, 0, 2))], new DateMillisecond()),
    });
    const out = decodeArrow(tableToIPC(table, "stream"));
    expect(out.rows[0]?.ts).toBe("2026-10-15T09:30:00.000Z");
    expect(out.rows[0]?.d).toBe("2026-01-02T00:00:00.000Z");
  });

  it("decodes dictionary-encoded strings and nulls", () => {
    const dict = vectorFromArray(["tv", "search", null, "tv"], new Dictionary(new Utf8(), new Int32()));
    const out = decodeArrow(tableToIPC(new Table({ channel: dict }), "stream"));
    expect(out.rows.map((r) => r.channel)).toEqual(["tv", "search", null, "tv"]);
  });

  it("serialises nested values as JSON text", () => {
    const table = new Table({ s: vectorFromArray([{ a: 1, b: "z" }]) });
    const out = decodeArrow(tableToIPC(table, "stream"));
    expect(JSON.parse(String(out.rows[0]?.s))).toEqual({ a: 1, b: "z" });
  });

  it("truncates to maxRows and reports the total", () => {
    const out = decodeArrow(tableToIPC(tableFromArrays({ x: Float64Array.from({ length: 10 }, (_, i) => i) })), 4);
    expect(out.rows).toHaveLength(4);
    expect(out.totalRows).toBe(10);
    expect(out.truncated).toBe(true);
  });

  it("rejects bytes that are not Arrow IPC", () => {
    expect(() => decodeArrow(new TextEncoder().encode("<html>not arrow</html>"))).toThrow(ArrowDecodeError);
  });

  it("round-trips base64 inline payloads", () => {
    const bytes = tableToIPC(tableFromArrays({ v: Float64Array.from([3.5]) }), "stream");
    let bin = "";
    bytes.forEach((b) => (bin += String.fromCharCode(b)));
    expect(decodeArrow(base64ToBytes(btoa(bin))).rows).toEqual([{ v: 3.5 }]);
  });
});
