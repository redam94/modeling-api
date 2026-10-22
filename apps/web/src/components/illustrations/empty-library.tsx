/** "Make your own" empty state: a small stack of cards, the top one a chart in the making, and a plus on the sun. */
import { Art, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptyLibraryArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <rect className={I.card} strokeWidth={2} x="88" y="24" width="252" height="196" rx="20" />
      <rect className={I.card} strokeWidth={2} x="64" y="50" width="256" height="200" rx="20" />
      <rect className={I.card} strokeWidth={2} x="40" y="76" width="260" height="204" rx="22" />
      <rect className={I.pale} x="64" y="100" width="110" height="16" rx="8" />
      <rect className={I.muted} x="64" y="126" width="70" height="10" rx="5" />
      <rect className={I.lav} x="64" y="214" width="30" height="42" rx="5" />
      <rect className={I.indigo} x="104" y="184" width="30" height="72" rx="5" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="144" y="200" width="30" height="56" rx="5" />
      <path className={I.curve} strokeWidth={6} strokeLinecap="round" d="M192 248 C 216 246, 230 182, 278 172" />
      <circle className={I.lime} cx="350" cy="70" r="44" />
      <path className={I.onSun} strokeWidth={6} strokeLinecap="round" d="M330 70 h 40 M350 50 v 40" />
      <SpendWedge />
    </Art>
  );
}
