/** Runs empty state: bars of a fit filling in under its response curve, with a done tick on the sun. */
import { Art, CardFrame, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptyRunsArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <CardFrame />
      <rect className={I.muted} x="64" y="116" width="252" height="124" rx="14" />
      <rect className={I.lav} x="84" y="190" width="30" height="34" rx="5" />
      <rect className={I.lav} x="124" y="172" width="30" height="52" rx="5" />
      <rect className={I.indigo} x="164" y="150" width="30" height="74" rx="5" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="204" y="136" width="30" height="88" rx="5" />
      <path className={I.curve} strokeWidth={6} strokeLinecap="round" d="M80 214 C 130 210, 170 150, 296 138" />
      <circle className={I.lime} cx="350" cy="78" r="44" />
      <path className={I.onSun} strokeWidth={6} strokeLinecap="round" strokeLinejoin="round" d="M331 80 L345 94 L369 66" />
      <SpendWedge />
    </Art>
  );
}
