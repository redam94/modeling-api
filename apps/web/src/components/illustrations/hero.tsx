/**
 * The Home hero art, drawn for the ink block: four bars (raised, lavender, indigo, white), a
 * coral response curve with its operating point, a lime sun and a pale spend wedge. It
 * "assembles" once when it mounts (bars grow in a stagger, the curve draws, the sun rises; the
 * keyframes live in app.css and end at their final frame under reduced motion). The one
 * orchestrated entrance of a page: pass `animate={false}` anywhere else.
 */
import { cn } from "@/lib/utils";

import { Art, I, type ArtProps } from "./art";

export function HeroArt({ label, className, animate = true }: ArtProps & { animate?: boolean }) {
  const bar = animate ? "assemble-bar" : undefined;
  return (
    <Art viewBox="0 0 380 300" label={label} className={cn("max-w-[380px] max-md:max-w-[260px]", className)}>
      <circle className={cn(I.lime, animate && "assemble-sun")} cx="292" cy="78" r="54" />
      {/* The bars must stay the first four rects: the stagger counts them with nth-of-type. */}
      <rect className={cn(I.raised, bar)} x="26" y="196" width="52" height="84" rx="6" />
      <rect className={cn(I.lav, bar)} x="92" y="150" width="52" height="130" rx="6" />
      <rect className={cn(I.indigo, bar)} x="158" y="112" width="52" height="168" rx="6" />
      <rect className={cn(I.white, bar)} x="224" y="160" width="52" height="120" rx="6" />
      <path
        className={cn(I.curve, animate && "assemble-draw")}
        pathLength={520}
        strokeWidth={10}
        strokeLinecap="round"
        d="M20 270 C 110 260, 150 120, 240 96 S 350 78, 360 76"
      />
      <circle className={I.coral} cx="240" cy="96" r="12" />
      <path className={I.pale} opacity={0.9} d="M290 280 a 70 70 0 0 1 70 -70 v 70 z" />
    </Art>
  );
}
