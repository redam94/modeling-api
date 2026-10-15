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

  useLayoutEffect(() => {
    const frame = frameRef.current;
    if (!frame) return;
    const host = new PlotHost(frame, {
      onRendered: () => {
        setStatus("rendered");
        setError(null);
      },
      onResize: (h) => setFrameHeight(Math.max(MIN_HEIGHT, h)),
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
      <div role="alert" className="flex items-start gap-2 rounded-lg border border-destructive/40 p-4 text-[13px]">
        <ShieldAlert className="size-4 text-destructive" />
        <p>{error}</p>
      </div>
    );
  }

  return (
    <figure className={cn("group relative grid gap-2", className)}>
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
        {status === "loading" ? (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-muted-foreground">
            <Spinner />
          </div>
        ) : null}
      </div>
      {status === "error" && error ? (
        <figcaption role="alert" className="text-xs text-destructive">
          The plot could not be rendered: {error}
        </figcaption>
      ) : null}
    </figure>
  );
}
