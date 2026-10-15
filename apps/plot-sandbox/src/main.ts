import "./style.css";

import { parseOrigins } from "./csp";
import type { PlotKind } from "./protocol";
import type { Renderer } from "./renderers";
import { SandboxController } from "./sandbox";

const DEFAULT_APP_ORIGINS = ["http://127.0.0.1:5173", "http://localhost:5173"];

async function loadRenderer(kind: PlotKind): Promise<Renderer> {
  if (kind === "vega-lite") return (await import("./vega-renderer")).renderVegaLite;
  return (await import("./plotly-renderer")).renderPlotly;
}

function start(): void {
  const container = document.getElementById("plot");
  if (!container) throw new Error("missing #plot container");
  if (window.parent === window) {
    // Opened directly rather than embedded by the app: render nothing.
    document.body.dataset.standalone = "true";
    return;
  }
  const appOrigins = parseOrigins(import.meta.env.VITE_APP_ORIGINS, DEFAULT_APP_ORIGINS);
  const controller = new SandboxController({
    parent: window.parent,
    appOrigins,
    container,
    loadRenderer,
    setTheme: (theme) => {
      document.documentElement.dataset.theme = theme;
    },
  });
  window.addEventListener("message", (event) => {
    void controller.handle(event);
  });
  const observer = new ResizeObserver(() => {
    controller.reportHeight(document.documentElement.scrollHeight);
  });
  observer.observe(document.body);
  controller.announce();
}

start();
