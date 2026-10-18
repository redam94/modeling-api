import { tableFromArrays, tableToIPC } from "apache-arrow";
import { describe, expect, it } from "vitest";

import { inlinePlot, isPlotPayload } from "./plot-payload";

function b64(bytes: Uint8Array): string {
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

const spec = { mark: "line", data: { name: "fan" } };

describe("plot outputs of workflow nodes", () => {
  it("decodes the inline Arrow datasets of a PlotSpec", () => {
    const ipc = tableToIPC(tableFromArrays({ x: Float64Array.from([1, 2]), y: Float64Array.from([3, 4]) }), "stream");
    const value = {
      id: "core.ppc_fan",
      kind: "vega-lite",
      spec,
      datasets: { fan: { kind: "inline_arrow", data: b64(ipc), rows: 2 } },
      caption_plain: "The band is the 90% range.",
    };
    expect(isPlotPayload(value)).toBe(true);
    const plot = inlinePlot(value);
    expect(plot?.kind).toBe("vega-lite");
    expect(plot?.datasets.fan).toEqual([
      { x: 1, y: 3 },
      { x: 2, y: 4 },
    ]);
    expect(plot?.caption).toBe("The band is the 90% range.");
  });

  it("keeps row datasets and refuses file datasets", () => {
    expect(inlinePlot({ kind: "plotly", spec, datasets: { fan: [{ x: 1 }] } })?.datasets.fan).toEqual([{ x: 1 }]);
    expect(inlinePlot({ kind: "vega-lite", spec, datasets: { fan: { kind: "data_ref", path: "ui/a.arrow" } } })).toBeNull();
    expect(isPlotPayload({ kind: "table", spec })).toBe(false);
    expect(inlinePlot({ format: "mapi.table/v1" })).toBeNull();
  });

  it("throws on an embedded dataset that is not Arrow", () => {
    expect(() => inlinePlot({ kind: "vega-lite", spec, datasets: { fan: { kind: "inline_arrow", data: "bm90IGFycm93IGF0IGFsbCwgbm90IGF0IGFsbA==" } } })).toThrow();
  });
});
