import Plotly, { type PlotlyEvent } from "plotly.js-strict-dist-min";

import { preparePlotly } from "./prepare";
import { type Row, toSelectRow } from "./protocol";
import type { Rendered, Renderer } from "./renderers";
import { toBase64DataUrl } from "./renderers";

function pointsToRows(ev: PlotlyEvent | undefined): Row[] {
  return (ev?.points ?? []).slice(0, 100).map((p) =>
    toSelectRow({
      x: p.x,
      y: p.y,
      z: p.z,
      trace: p.curveNumber,
      index: p.pointIndex ?? p.pointNumber,
      name: p.data?.name,
    }),
  );
}

export const renderPlotly: Renderer = async (ctx) => {
  const { data, layout, frames } = preparePlotly(ctx.spec, ctx.datasets, ctx.theme);
  const el = await Plotly.newPlot(ctx.container, data, layout, {
    displaylogo: false,
    displayModeBar: false,
    responsive: true,
    showLink: false,
    showSendToCloud: false,
    showEditInChartStudio: false,
    scrollZoom: false,
    staticPlot: false,
  });
  if (frames.length) await Plotly.addFrames(ctx.container, frames);
  el.on("plotly_click", (ev) => ctx.onSelect(pointsToRows(ev)));
  el.on("plotly_selected", (ev) => ctx.onSelect(pointsToRows(ev)));
  const rendered: Rendered = {
    async export(format) {
      const url = await Plotly.toImage(ctx.container, { format, scale: format === "png" ? 2 : 1 });
      return toBase64DataUrl(url, format);
    },
    destroy() {
      Plotly.purge(ctx.container);
    },
  };
  return rendered;
};
