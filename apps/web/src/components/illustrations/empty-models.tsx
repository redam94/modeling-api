/** Models empty state: a saturation curve over its spend bars, with the operating point in lime. */
import { Art, CardFrame, EMPTY_VIEWBOX, I, SpendWedge, type ArtProps } from "./art";

export function EmptyModelsArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <CardFrame />
      <path className="fill-none stroke-border-strong" strokeWidth={2} strokeLinecap="round" d="M64 238 H 316" />
      <rect className={I.lav} x="80" y="208" width="26" height="30" rx="5" />
      <rect className={I.indigo} x="116" y="190" width="26" height="48" rx="5" />
      <rect className={I.lav} x="152" y="178" width="26" height="60" rx="5" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="188" y="172" width="26" height="66" rx="5" />
      <path className={I.curve} strokeWidth={8} strokeLinecap="round" d="M70 230 C 130 226, 160 140, 232 128 S 300 118, 312 117" />
      <circle className="fill-illus-3 stroke-brand-800" strokeWidth={3} cx="232" cy="128" r="14" />
      <SpendWedge />
    </Art>
  );
}
