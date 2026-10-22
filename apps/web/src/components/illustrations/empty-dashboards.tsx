/** Dashboards empty state: a dashboard card being laid out (headline, bars, a what-if panel). */
import { Art, CardFrame, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptyDashboardsArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <CardFrame />
      <rect className={I.lav} x="64" y="200" width="30" height="40" rx="5" />
      <rect className={I.indigo} x="104" y="170" width="30" height="70" rx="5" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="144" y="186" width="30" height="54" rx="5" />
      <rect className={I.muted} x="200" y="120" width="116" height="120" rx="14" />
      <path className={I.curve} strokeWidth={6} strokeLinecap="round" d="M212 222 C 240 220, 252 150, 302 140" />
      <circle className={I.lime} cx="350" cy="78" r="44" />
      <path className={I.onSun} strokeWidth={6} strokeLinecap="round" d="M330 78 h 40 M350 58 v 40" />
      <SpendWedge />
      <circle className={I.coral} cx="30" cy="278" r="14" />
    </Art>
  );
}
