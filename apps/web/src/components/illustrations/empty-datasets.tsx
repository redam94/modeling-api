/** Datasets empty state: a table card whose rows are bars, the last one still to come, and an upload sun. */
import { Art, CardFrame, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptyDatasetsArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <CardFrame />
      <rect className={I.indigo} x="64" y="124" width="200" height="16" rx="6" />
      <rect className={I.lav} x="64" y="150" width="160" height="16" rx="6" />
      <rect className={I.lav} x="64" y="176" width="232" height="16" rx="6" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="64" y="204" width="188" height="16" rx="6" />
      <circle className={I.lime} cx="350" cy="78" r="44" />
      <path className={I.onSun} strokeWidth={6} strokeLinecap="round" strokeLinejoin="round" d="M350 98 V 60 M334 74 L350 58 L366 74" />
      <SpendWedge />
      <circle className={I.coral} cx="30" cy="278" r="14" />
    </Art>
  );
}
