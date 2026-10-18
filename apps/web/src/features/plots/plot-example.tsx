/**
 * What a catalog plot looks like: the recipe rendered on synthetic example data by the
 * describe run (`GET /v1/catalog/plots/{plot_id}/{version}/example`, datasets inline),
 * drawn in the plot sandbox like every other plot.
 */
import type { JsonObject } from "@mapi/plot-sandbox/protocol";
import { useQuery } from "@tanstack/react-query";
import { ImageOff } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { ApiError, api, unwrap } from "@/api/client";
import { Skeleton } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

import { PlotFrame } from "./plot-frame";
import { type ResolvedPlot, resolvePlotDatasets } from "./plot-data";

export const plotExampleKey = (id: string, version: string) => ["catalog", "plots", id, version, "example"] as const;

/** The example of `id@version`, decoded; `null` when the catalog has no example for it. */
export function usePlotExample(id: string, version: string, enabled = true) {
  return useQuery({
    queryKey: plotExampleKey(id, version),
    enabled,
    staleTime: Infinity,
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 2,
    queryFn: async ({ signal }): Promise<ResolvedPlot | null> => {
      let spec;
      try {
        spec = await unwrap(api.GET("/v1/catalog/plots/{plot_id}/{version}/example", { params: { path: { plot_id: id, version } }, signal }));
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return null;
        throw e;
      }
      const datasets = await resolvePlotDatasets(
        spec,
        () => Promise.reject(new Error("a catalog example must embed its datasets")),
        signal,
      );
      return { spec, body: spec.spec as JsonObject, datasets };
    },
  });
}

/** True once the element has come near the viewport (and stays true). */
function useNearViewport<T extends Element>(): [React.RefObject<T | null>, boolean] {
  const ref = useRef<T>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || near) return;
    if (typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) setNear(true);
    }, { rootMargin: "200px" });
    io.observe(el);
    return () => io.disconnect();
  }, [near]);
  return [ref, near];
}

export interface PlotExampleProps {
  id: string;
  version: string;
  title: string;
  height?: number;
  /** A thumbnail: no toolbar, no pointer interaction (the card around it is the button). */
  preview?: boolean;
  className?: string;
}

/** The width a thumbnail is drawn at before it is scaled down to its card. */
const PREVIEW_WIDTH = 760;

/** A plot drawn at {@link PREVIEW_WIDTH} and scaled down so all of it fits `width` x `height`. */
function ScaledPreview({ width, height, children }: { width: number; height: number; children: (onHeight: (h: number) => void) => React.ReactNode }) {
  const [natural, setNatural] = useState(height);
  const scale = Math.min(1, width / PREVIEW_WIDTH, height / natural);
  return (
    <div className="relative h-full w-full overflow-hidden">
      <div
        className="absolute top-0 left-1/2 origin-top"
        style={{ width: PREVIEW_WIDTH, transform: `translateX(-50%) scale(${scale})` }}
      >
        {children(setNatural)}
      </div>
    </div>
  );
}

function useWidth<T extends Element>(ref: React.RefObject<T | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setWidth(el.getBoundingClientRect().width);
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(([e]) => e && setWidth(e.contentRect.width));
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

/** The example render of a catalog plot, loaded when it scrolls into view. */
export function PlotExample({ id, version, title, height = 320, preview = false, className }: PlotExampleProps) {
  const [ref, near] = useNearViewport<HTMLDivElement>();
  const width = useWidth(ref);
  const example = usePlotExample(id, version, near);
  let body: React.ReactNode;
  if (example.data === null || example.isError) {
    body = (
      <div className="flex h-full flex-col items-center justify-center gap-1.5 text-center text-xs text-muted-foreground">
        <ImageOff aria-hidden className="size-5" />
        {example.isError ? "The app cannot load the example." : "This plot has no example."}
      </div>
    );
  } else if (example.data && preview) {
    const data = example.data;
    body = (
      <ScaledPreview width={width || PREVIEW_WIDTH} height={height}>
        {(onHeight) => (
          <PlotFrame kind={data.spec.kind} spec={data.body} datasets={data.datasets} title={`Example: ${title}`} height={height} toolbar={false} onHeightChange={onHeight} />
        )}
      </ScaledPreview>
    );
  } else if (example.data) {
    body = (
      <PlotFrame
        kind={example.data.spec.kind}
        spec={example.data.body}
        datasets={example.data.datasets}
        title={`Example: ${title}`}
        height={height}
        toolbar={!preview}
        exportName={`${id}-example`}
      />
    );
  } else {
    body = <Skeleton className="h-full w-full" />;
  }
  return (
    <div
      ref={ref}
      data-testid="plot-example"
      data-plot={id}
      className={cn(preview && "pointer-events-none overflow-hidden", className)}
      style={preview ? { height } : { minHeight: height }}
      aria-hidden={preview || undefined}
    >
      {body}
    </div>
  );
}
