/** Workflows empty state: two inputs feeding a chart step that feeds one lime result. */
import { Art, EMPTY_VIEWBOX, I, type ArtProps } from "./art";

const LINK = "fill-none stroke-border-strong";

export function EmptyWorkflowsArt({ label, className }: ArtProps) {
  return (
    <Art viewBox={EMPTY_VIEWBOX} label={label} className={className}>
      <path className={LINK} strokeWidth={4} strokeLinecap="round" d="M110 92 C 160 92, 160 160, 200 160 M110 228 C 160 228, 160 160, 200 160 M320 160 H 350" />
      <circle className={I.indigo} cx="84" cy="92" r="26" />
      <circle className={I.lav} cx="84" cy="228" r="26" />
      <rect className={I.card} strokeWidth={2} x="200" y="104" width="120" height="112" rx="18" />
      <rect className={I.lav} x="222" y="168" width="18" height="28" rx="5" />
      <rect className={I.indigo} x="246" y="146" width="18" height="50" rx="5" />
      <rect className={I.dashed} strokeWidth={2} strokeDasharray="6 5" x="270" y="156" width="18" height="40" rx="5" />
      <rect className={I.pale} x="222" y="124" width="64" height="10" rx="5" />
      <circle className={I.lime} cx="378" cy="160" r="28" />
      <circle className={I.coral} cx="34" cy="292" r="14" />
    </Art>
  );
}
