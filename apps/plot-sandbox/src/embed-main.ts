import "./style.css";

import { API_URL_PLACEHOLDER, parseApiUrl } from "./csp";
import { EmbedPage, type EmbedElements } from "./embed";
import type { PlotKind } from "./protocol";
import type { Renderer } from "./renderers";

async function loadRenderer(kind: PlotKind): Promise<Renderer> {
  if (kind === "vega-lite") return (await import("./vega-renderer")).renderVegaLite;
  return (await import("./plotly-renderer")).renderPlotly;
}

function element(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing #${id}`);
  return el;
}

function anchor(id: string): HTMLAnchorElement {
  const el = element(id);
  if (!(el instanceof HTMLAnchorElement)) throw new Error(`#${id} is not a link`);
  return el;
}

/** The API origin: a `<meta name="mapi-api-url">` the host fills in (or the build default). */
function apiUrl(): string {
  const meta = document.querySelector<HTMLMetaElement>('meta[name="mapi-api-url"]')?.content;
  const url = parseApiUrl(meta, "http://127.0.0.1:8000");
  if (url === API_URL_PLACEHOLDER) throw new Error("the embed page was served without its API origin");
  return url;
}

function start(): void {
  const elements: EmbedElements = {
    title: element("title"),
    plot: element("plot"),
    caption: element("caption"),
    status: element("status"),
    png: anchor("png"),
    meta: element("meta"),
  };
  const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? false;
  const theme = dark ? "dark" : "light";
  document.documentElement.dataset.theme = theme;
  const ancestors = window.location.ancestorOrigins as DOMStringList | undefined;
  const page = new EmbedPage({
    apiUrl: apiUrl(),
    pathname: window.location.pathname,
    elements,
    theme,
    loadRenderer,
    frame: {
      framed: window.parent !== window,
      ancestorOrigins: ancestors ? Array.from(ancestors) : null,
      referrer: document.referrer,
    },
  });
  void page.start();
}

start();
