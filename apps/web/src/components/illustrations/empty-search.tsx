/** Nothing matches (a filter or a search with no results): a card of placeholder bars only. */
import { Art, CardFrame, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptySearchArt({ label, className }: ArtProps) {
  const dashed = { className: I.dashed, strokeWidth: 2, strokeDasharray: "6 5", rx: 5, width: 30 } as const;
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <CardFrame />
      <rect {...dashed} x="64" y="190" height="50" />
      <rect {...dashed} x="104" y="160" height="80" />
      <rect {...dashed} x="144" y="200" height="40" />
      <rect {...dashed} x="184" y="176" height="64" />
      <circle className={I.lime} cx="350" cy="78" r="44" />
      <SpendWedge />
      <circle className={I.coral} cx="30" cy="278" r="14" />
    </Art>
  );
}
