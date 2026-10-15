import * as vega from "vega";
import embed from "vega-embed";
import { expressionInterpreter } from "vega-interpreter";

import { prepareVegaLite } from "./prepare";
import { toSelectRow } from "./protocol";
import type { Rendered, Renderer } from "./renderers";
import { utf8ToBase64 } from "./renderers";
import { TextTooltip } from "./tooltip";

const REFUSED = "external resources are disabled in the plot sandbox";

/**
 * A Vega loader that refuses every URL: data, images and anything else a spec could ask
 * Vega to fetch. Named datasets are injected inline, so nothing legitimate needs it.
 */
export function denyAllLoader(): vega.Loader {
  const base = vega.loader();
  const refuse = (): Promise<never> => Promise.reject(new Error(REFUSED));
  return Object.assign(Object.create(Object.getPrototypeOf(base) as object) as vega.Loader, base, {
    load: refuse,
    sanitize: refuse,
    http: refuse,
    file: refuse,
  });
}

let tooltip: TextTooltip | null = null;

export const renderVegaLite: Renderer = async (ctx) => {
  tooltip ??= new TextTooltip();
  const spec = prepareVegaLite(ctx.spec, ctx.datasets, ctx.theme);
  const result = await embed(ctx.container, spec, {
    mode: "vega-lite",
    renderer: "svg",
    actions: false,
    defaultStyle: false,
    ast: true,
    expr: expressionInterpreter,
    loader: denyAllLoader(),
    tooltip: tooltip.handler,
    hover: true,
    logLevel: vega.Warn,
  });
  const view = result.view;
  view.addEventListener("click", (_event, item) => {
    const datum: unknown = (item as { datum?: unknown } | null | undefined)?.datum;
    if (datum && typeof datum === "object") ctx.onSelect([toSelectRow(datum)]);
  });
  const rendered: Rendered = {
    async export(format) {
      if (format === "svg") {
        const svg = await view.toSVG();
        return `data:image/svg+xml;base64,${utf8ToBase64(svg)}`;
      }
      const canvas = await view.toCanvas(2);
      return canvas.toDataURL("image/png");
    },
    destroy() {
      tooltip?.hide();
      result.finalize();
    },
  };
  return rendered;
};
