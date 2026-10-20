/**
 * The Data Explorer's `gallery` section (v0.14): thumbnails of a stack of images (one variable
 * along an image dim), 48 per page, with a filter for each facet (the class label, the split
 * and other columns along the image dim) and a larger view of one image with its facet values.
 * The runner sends at most 240 thumbnails (an even sample of each label and split when there
 * are more images). Thumbnails are PNG `data:` URLs; labels and facet values are user data:
 * text only.
 */
import { useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { facetLevels, filterItems, galleryNote, GALLERY_PAGE_SIZE, metaText, pageCount, pageItems, PIXELATED_BELOW, shownCounts, thumbSrc, type GalleryFilters } from "./gallery";
import { count, PlainSummary, ViewNotes } from "./ui";
import type { GalleryFacet, GalleryItem, GalleryView } from "./views";

function FacetChips({ facet, value, shown, onChange }: { facet: GalleryFacet; value: string | null; shown: Record<string, number>; onChange: (level: string | null) => void }) {
  const levels = facetLevels(facet);
  return (
    <div className="grid gap-1">
      <span className="text-xs font-medium text-muted-foreground" id={`gallery-facet-${facet.name}`}>
        {facet.name}
      </span>
      <div className="flex flex-wrap gap-1" role="group" aria-labelledby={`gallery-facet-${facet.name}`}>
        <Button size="sm" variant={value === null ? "secondary" : "ghost"} className={cn("h-7 px-2.5", value === null && "ring-1 ring-border")} aria-pressed={value === null} onClick={() => onChange(null)}>
          All
        </Button>
        {levels.map(({ level, count: n }) => (
          <Button
            key={level}
            size="sm"
            variant={value === level ? "secondary" : "ghost"}
            className={cn("h-7 px-2.5", value === level && "ring-1 ring-border")}
            aria-pressed={value === level}
            title={`${count(n)} images, ${count(shown[level] ?? 0)} shown`}
            onClick={() => onChange(value === level ? null : level)}
          >
            {metaText(level)} <span className="tabular text-muted-foreground">({count(n)})</span>
          </Button>
        ))}
      </div>
    </div>
  );
}

function Thumb({ item, view, selected, onSelect, size }: { item: GalleryItem; view: GalleryView; selected: boolean; onSelect: () => void; size: number }) {
  const src = thumbSrc(item);
  const name = metaText(item.label ?? item.index);
  const pixelated = Math.max(view.thumb_height, view.thumb_width) < PIXELATED_BELOW;
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      aria-label={`Image ${name}`}
      className={cn("grid gap-1 rounded-md border p-1 text-left outline-none focus-visible:outline-2 focus-visible:outline-ring", selected && "ring-2 ring-primary")}
      data-testid="gallery-thumb"
    >
      {src ? (
        <img src={src} alt="" width={size} height={Math.round((size * view.thumb_height) / view.thumb_width)} className="w-full bg-muted/40" style={{ imageRendering: pixelated ? "pixelated" : "auto", aspectRatio: `${view.thumb_width} / ${view.thumb_height}` }} />
      ) : (
        <span className="grid aspect-square place-items-center bg-muted/40 text-[11px] text-muted-foreground">no image</span>
      )}
      <span className="truncate text-[11px] text-muted-foreground">{name}</span>
    </button>
  );
}

function Detail({ item, view }: { item: GalleryItem; view: GalleryView }) {
  const src = thumbSrc(item);
  const meta = Object.entries(item.meta ?? {});
  return (
    <figure className="grid gap-3 rounded-lg border p-3 md:grid-cols-[minmax(0,16rem)_1fr]" data-testid="gallery-detail" aria-label={`Image ${metaText(item.label ?? item.index)}`}>
      {src ? <img src={src} alt="" className="w-full rounded-sm border bg-muted/40" style={{ imageRendering: "pixelated", aspectRatio: `${view.thumb_width} / ${view.thumb_height}` }} /> : null}
      <dl className="grid h-fit grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[13px]">
        <dt className="text-muted-foreground">{view.frame_dim}</dt>
        <dd className="break-all">{metaText(item.label)}</dd>
        <dt className="text-muted-foreground">Position</dt>
        <dd className="tabular">{count(item.index)}</dd>
        {meta.map(([k, v]) => (
          <div key={k} className="contents">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="break-all">{metaText(v)}</dd>
          </div>
        ))}
        <dt className="text-muted-foreground">Size</dt>
        <dd className="tabular">
          {view.source_height} x {view.source_width} pixels{view.thumb_height < view.source_height || view.thumb_width < view.source_width ? `, shown as ${view.thumb_height} x ${view.thumb_width}` : ""}
        </dd>
      </dl>
    </figure>
  );
}

export function GallerySection({ view }: { view: GalleryView; expert: boolean }) {
  const [filters, setFilters] = useState<GalleryFilters>({});
  const [page, setPage] = useState(0);
  const [picked, setPicked] = useState<number | null>(null);
  const facets = useMemo(() => view.facets ?? [], [view.facets]);
  const matching = useMemo(() => filterItems({ items: view.items ?? [], facets }, filters), [view.items, facets, filters]);
  const pages = pageCount(matching.length);
  const current = Math.min(page, pages - 1);
  const shown = pageItems(matching, current);
  const selected = matching.find((i) => i.index === picked) ?? null;
  const colour = view.color === "rgb" ? "colour" : "gray";
  const size = Math.max(48, Math.min(96, 2 * Math.max(view.thumb_height, view.thumb_width)));

  const setFacet = (name: string, level: string | null) => {
    setFilters((f) => ({ ...f, [name]: level }));
    setPage(0);
  };

  return (
    <>
      <PlainSummary>
        {`${view.var} holds ${count(view.n_images)} ${colour} images of ${view.source_height} x ${view.source_width} pixels along ${view.frame_dim}.`}
      </PlainSummary>
      {facets.length ? (
        <div className="flex flex-wrap gap-x-6 gap-y-3">
          {facets.map((f) => (
            <FacetChips key={f.name} facet={f} value={filters[f.name] ?? null} shown={shownCounts({ items: view.items ?? [], facets }, filters, f)} onChange={(level) => setFacet(f.name, level)} />
          ))}
        </div>
      ) : null}
      <p className="text-xs text-muted-foreground" data-testid="gallery-note">
        {galleryNote(view)}
      </p>
      <p className="text-[13px]" aria-live="polite" data-testid="gallery-count">
        {matching.length === view.items.length ? `${count(matching.length)} thumbnails.` : `${count(matching.length)} of ${count(view.items.length)} thumbnails match the filters.`}
      </p>
      {shown.length ? (
        <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(auto-fill, minmax(${size}px, 1fr))` }} role="list" aria-label={`Images of ${view.var}`}>
          {shown.map((item) => (
            <div role="listitem" key={item.index}>
              <Thumb item={item} view={view} size={size} selected={item.index === picked} onSelect={() => setPicked(item.index === picked ? null : item.index)} />
            </div>
          ))}
        </div>
      ) : (
        <p className="text-muted-foreground">No image matches the filters.</p>
      )}
      {pages > 1 ? (
        <nav className="flex items-center gap-3" aria-label="Gallery pages">
          <Button size="sm" variant="outline" disabled={current === 0} onClick={() => setPage(current - 1)}>
            Previous
          </Button>
          <span className="tabular text-[13px]">
            Page {current + 1} of {pages}
          </span>
          <Button size="sm" variant="outline" disabled={current >= pages - 1} onClick={() => setPage(current + 1)}>
            Next
          </Button>
          <span className="text-xs text-muted-foreground">{GALLERY_PAGE_SIZE} images on each page.</span>
        </nav>
      ) : null}
      {selected ? <Detail item={selected} view={view} /> : <p className="text-xs text-muted-foreground">Click an image to see it larger, with its values.</p>}
      {view.other_vars?.length ? <p className="text-xs text-muted-foreground">Other image variables (not shown): {view.other_vars.join(", ")}.</p> : null}
      <ViewNotes caption={view.caption_plain} warnings={view.warnings_plain} />
    </>
  );
}
