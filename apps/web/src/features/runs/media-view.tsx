/**
 * One media item of a run (v0.14), shared by the run page's Media tab and the compare page: an
 * image through a checked download and an object URL (revoked when the item changes or the
 * view unmounts), or a table as text cells. Captions and cells are untrusted text.
 */
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { cellText, fetchMediaBlob, fetchRunMediaItem, linkExpired, MediaError, mediaLabel, parseMediaTable, type MediaTable } from "@/api/media";
import { useProjectScope } from "@/api/queries";
import type { RunMedia } from "@/api/types";
import { ErrorNotice, Mono } from "@/components/common";
import { Skeleton } from "@/components/ui/primitives";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Rows of a table the view shows (the rest is counted). */
export const TABLE_PREVIEW_ROWS = 200;

/** Download an item, with a new signed link when its link expired (or the download was refused). */
async function loadBlob(media: RunMedia, headers: Record<string, string>, signal: AbortSignal): Promise<Blob> {
  const fresh = () => fetchRunMediaItem(media.run_id, media.id, { headers, signal });
  if (linkExpired(media)) return fetchMediaBlob(await fresh(), signal);
  try {
    return await fetchMediaBlob(media, signal);
  } catch (err) {
    // a link can expire between the list and the download: try once with a new one
    if (err instanceof MediaError && /HTTP 4\d\d/.test(err.message)) return fetchMediaBlob(await fresh(), signal);
    throw err;
  }
}

/** The checked bytes of an item (immutable: cached by id and SHA-256, dropped soon after use). */
export function useMediaBlob(media: RunMedia | null) {
  const s = useProjectScope();
  return useQuery({
    queryKey: ["p", s.key, "media-blob", media?.id ?? "", media?.sha256 ?? ""] as const,
    queryFn: ({ signal }) => loadBlob(media!, s.headers, signal),
    enabled: media !== null,
    staleTime: Infinity,
    gcTime: 60_000,
    retry: (n, err) => !(err instanceof MediaError) && n < 2,
  });
}

/** An object URL for a Blob, revoked when the Blob changes or the component unmounts. */
export function useObjectUrl(blob: Blob | undefined): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const u = URL.createObjectURL(blob);
    setUrl(u);
    return () => URL.revokeObjectURL(u);
  }, [blob]);
  return url;
}

export function MediaImage({ media, className, pixelated }: { media: RunMedia; className?: string; pixelated?: boolean }) {
  const blob = useMediaBlob(media);
  const url = useObjectUrl(blob.data);
  if (blob.isError) return <ErrorNotice error={blob.error} title="The app could not show the image" />;
  if (!url) return <Skeleton className={cn("aspect-square w-full max-w-64", className)} aria-label="Loading the image" />;
  const small = pixelated ?? ((media.width ?? 64) < 64 || (media.height ?? 64) < 64);
  return (
    <img
      src={url}
      alt={media.caption ? media.caption : `${mediaLabel(media)}`}
      width={media.width ?? undefined}
      height={media.height ?? undefined}
      data-media-id={media.id}
      className={cn("h-auto max-w-full rounded-md border bg-muted/40", small && "min-w-32 [image-rendering:pixelated]", className)}
    />
  );
}

/** Parse the bytes of a table item (cached with its Blob). */
function useMediaTable(media: RunMedia) {
  const s = useProjectScope();
  const blob = useMediaBlob(media);
  return useQuery({
    queryKey: ["p", s.key, "media-table", media.id, media.sha256] as const,
    queryFn: async (): Promise<MediaTable> => parseMediaTable(new Uint8Array(await blob.data!.arrayBuffer())),
    enabled: Boolean(blob.data),
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  });
}

export function MediaTableView({ media }: { media: RunMedia }) {
  const blob = useMediaBlob(media);
  const table = useMediaTable(media);
  if (blob.isError) return <ErrorNotice error={blob.error} title="The app could not load the table" />;
  if (table.isError) return <ErrorNotice error={table.error} title="The app could not show the table" />;
  if (!table.data) return <Skeleton className="h-24 w-full" aria-label="Loading the table" />;
  const { columns, rows } = table.data;
  const shown = rows.slice(0, TABLE_PREVIEW_ROWS);
  return (
    <div className="grid gap-1.5">
      <div className="max-h-96 overflow-auto rounded-md border [contain:inline-size]">
        <table className="w-full text-xs" aria-label={`Table ${mediaLabel(media)}`}>
          <thead className="sticky top-0 bg-card">
            <tr className="border-b">
              {columns.map((c, i) => (
                <th key={i} scope="col" className="px-2 py-1.5 text-left font-medium whitespace-nowrap text-muted-foreground">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((row, r) => (
              <tr key={r} className="border-b last:border-0">
                {row.map((cell, i) => (
                  <td key={i} className={cn("px-2 py-1 align-top [overflow-wrap:anywhere]", typeof cell === "number" && "text-right tabular")}>
                    {cellText(cell)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > shown.length ? (
        <p className="text-xs text-muted-foreground">
          {(rows.length - shown.length).toLocaleString("en")} more rows. The view shows the first {TABLE_PREVIEW_ROWS}.
        </p>
      ) : null}
    </div>
  );
}

/** The item (image or table) with its caption, size and short SHA-256. */
export function MediaItemView({ media, compact }: { media: RunMedia; compact?: boolean }) {
  return (
    <figure className="grid min-w-0 gap-2">
      {media.kind === "image" ? <MediaImage media={media} /> : <MediaTableView media={media} />}
      <figcaption className="grid gap-0.5 text-xs">
        {media.caption ? <span className="whitespace-pre-wrap [overflow-wrap:anywhere]">{media.caption}</span> : null}
        {compact ? null : (
          <span className="flex flex-wrap gap-x-3 text-muted-foreground">
            <span>step {media.step ?? "—"}</span>
            {media.width && media.height ? (
              <span>
                {media.width} × {media.height} px
              </span>
            ) : null}
            <span>{formatBytes(media.size)}</span>
            <Mono title={media.sha256}>{media.sha256.replace(/^sha256:/, "").slice(0, 12)}</Mono>
          </span>
        )}
      </figcaption>
    </figure>
  );
}
