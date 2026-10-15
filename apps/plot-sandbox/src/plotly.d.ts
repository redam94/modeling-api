// Minimal typings for the subset of the Plotly strict bundle the sandbox uses.
declare module "plotly.js-strict-dist-min" {
  export interface PlotlyPoint {
    x?: unknown;
    y?: unknown;
    z?: unknown;
    curveNumber?: number;
    pointIndex?: number;
    pointNumber?: number;
    data?: { name?: unknown };
  }
  export interface PlotlyEvent {
    points?: PlotlyPoint[];
  }
  export interface PlotlyHTMLElement extends HTMLElement {
    on(event: "plotly_click" | "plotly_selected", handler: (ev: PlotlyEvent | undefined) => void): void;
    removeAllListeners?(event: string): void;
  }
  export function newPlot(
    root: HTMLElement,
    data: unknown[],
    layout?: Record<string, unknown>,
    config?: Record<string, unknown>,
  ): Promise<PlotlyHTMLElement>;
  export function addFrames(root: HTMLElement, frames: unknown[]): Promise<void>;
  export function purge(root: HTMLElement): void;
  export function toImage(
    root: HTMLElement,
    opts: { format: "png" | "svg"; scale?: number; width?: number; height?: number },
  ): Promise<string>;
  const Plotly: {
    newPlot: typeof newPlot;
    addFrames: typeof addFrames;
    purge: typeof purge;
    toImage: typeof toImage;
  };
  export default Plotly;
}
