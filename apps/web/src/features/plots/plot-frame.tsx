import type { JsonObject, PlotDatasets, PlotKind, Row } from "@mapi/plot-sandbox/protocol";
import { Download, ShieldAlert } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { env } from "@/env";
import { useResolvedTheme } from "@/hooks/use-theme";
import { saveDataUrl } from "@/lib/download";
import { cn } from "@/lib/utils";

import { MIN_HEIGHT, PlotHost } from "./plot-host";

export interface PlotFrameProps {
  kind: PlotKind;
  spec: JsonObject;
  datasets: PlotDatasets;
  title: string;
  height?: number;
  onSelect?: (items: Row[]) => void;
  exportName?: string;
  className?: string;
  toolbar?: boolean;
  /** The rendered height the sandbox reports (thumbnails scale the frame to fit). */
  onHeightChange?: (height: number) => void;
}

/**
 * Renders a plot inside the isolated plot-sandbox origin. This is the only way the app
 * renders Vega-Lite or Plotly (first-party specs included).
 */
export function PlotFrame({
  kind,
  spec,
  datasets,
  title,
  height = 320,
  onSelect,
  exportName,
  className,
  toolbar = true,
  onHeightChange,
}: PlotFrameProps) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  const hostRef = useRef<PlotHost | null>(null);
  const theme = useResolvedTheme();
  const [frameHeight, setFrameHeight] = useState(height);
  const [status, setStatus] = useState<"loading" | "rendered" | "error" | "compromised">("loading");
  const [error, setError] = useState<string | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const exportNameRef = useRef(exportName);
  exportNameRef.current = exportName;
  const onHeightRef = useRef(onHeightChange);
  onHeightRef.current = onHeightChange;

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const host = new PlotHost(frame, {
      onRendered: () => {
        setStatus("rendered");
        setError(null);
      },
      onResize: (h) => {
        setFrameHeight(Math.max(MIN_HEIGHT, h));
        onHeightRef.current?.(Math.max(MIN_HEIGHT, h));
      },
      onSelect: (items) => onSelectRef.current?.(items),
      onExported: (format, dataUrl) => saveDataUrl(dataUrl, `${exportNameRef.current ?? "plot"}.${format}`),
      onError: (code, message) => {
        setStatus(code === "COMPROMISED" ? "compromised" : "error");
        setError(message);
      },
    });
    host.attach();
    hostRef.current = host;
    return () => {
      host.detach();
      hostRef.current = null;
    };
    // One host per iframe element: the sandbox announces "ready" only once per load.
  }, []);

  useEffect(() => {
    setStatus("loading");
    hostRef.current?.render(kind, spec, datasets, theme);
    // Theme changes re-render inside the sandbox via the theme message below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [kind, spec, datasets]);

  useEffect(() => {
    hostRef.current?.setTheme(theme);
  }, [theme]);

  if (status === "compromised") {
    return (
      <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/40 p-4 text-sm">
        <ShieldAlert className="size-4 text-destructive" />
        <p>{error}</p>
      </div>
    );
  }

  return (
    <figure className={cn("group relative grid gap-2", className)} aria-busy={status === "loading"}>
      {toolbar ? (
        <div className="absolute top-1 right-1 z-[1] flex gap-1 opacity-0 transition-opacity group-focus-within:opacity-100 group-hover:opacity-100">
          <Button variant="outline" size="sm" onClick={() => hostRef.current?.requestExport("svg")} disabled={status !== "rendered"}>
            <Download /> SVG
          </Button>
          <Button variant="outline" size="sm" onClick={() => hostRef.current?.requestExport("png")} disabled={status !== "rendered"}>
            <Download /> PNG
          </Button>
        </div>
      ) : null}
      <div className="relative">
        <iframe
          ref={frameRef}
          title={title}
          src={`${env.plotsOrigin}/`}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          allow=""
          loading="lazy"
          className="block w-full rounded-md border-0 bg-transparent"
          style={{ height: frameHeight }}
          data-testid="plot-frame"
        />
        {status === "loading" ? <PlotSkeleton /> : null}
      </div>
      {status === "error" && error ? (
        <figcaption role="alert" className="text-xs text-destructive">
          The app cannot show the plot: {error}
        </figcaption>
      ) : null}
    </figure>
  );
}

// Bar heights of the skeleton, as a share of the plot area: a calm rise and fall, not data.
const SKELETON_BARS = [0.42, 0.58, 0.5, 0.72, 0.64, 0.86, 0.7, 0.54, 0.62, 0.46];

/**
 * What a plot shows while the sandbox draws it: a chart-shaped placeholder (gridlines, an axis
 * and muted bars) in token colours, so the page keeps its shape. Static: it never pulses; the
 * small spinner in the corner is the only motion, and the reduced-motion override stops it.
 */
function PlotSkeleton() {
  return (
    <div aria-hidden data-slot="plot-skeleton" className="pointer-events-none absolute inset-0 flex flex-col gap-3 rounded-md bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <div className="h-3 w-1/3 max-w-40 rounded-full bg-muted" />
        <Spinner className="text-hint" />
      </div>
      <div className="relative flex min-h-0 flex-1 items-end gap-[3%] border-b-2 border-chart-base px-[2%]">
        <div className="absolute inset-x-0 top-1/4 h-px bg-chart-grid" />
        <div className="absolute inset-x-0 top-1/2 h-px bg-chart-grid" />
        <div className="absolute inset-x-0 top-3/4 h-px bg-chart-grid" />
        {SKELETON_BARS.map((h, i) => (
          <div key={i} className="relative flex-1 rounded-t-md bg-muted" style={{ height: `${h * 100}%` }} />
        ))}
      </div>
    </div>
  );
}
