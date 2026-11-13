/**
 * Generated raster art (`src/assets/brand/`, see PROVENANCE.md there). Every file is line-work
 * on pure black, so one file serves both themes: `art-on-black` (app.css) drops the black on a
 * dark surface and inverts the lines on a light one. The art is decoration: it has no text and
 * assistive technology does not see it.
 *
 * Import the file from `src` in the module that shows it (Vite then gives it a hashed name), and
 * pass its pixel size so the page does not move when it loads.
 */
import { cn } from "@/lib/utils";

export interface BrandArtProps {
  /** The imported file. */
  src: string;
  /** The pixel size of the file (the shown size comes from `className`). */
  width: number;
  height: number;
  className?: string;
  /**
   * `true` (the default): the art follows the theme. `"screen"`: the art sits on a block that is
   * dark in both themes, so the black always drops out. `false`: a backdrop, shown as drawn.
   */
  blend?: boolean | "screen";
}

export function BrandArt({ src, width, height, className, blend = true }: BrandArtProps) {
  return (
    <img
      src={src}
      width={width}
      height={height}
      alt=""
      aria-hidden
      data-slot="brand-art"
      loading="lazy"
      decoding="async"
      draggable={false}
      className={cn("pointer-events-none select-none", blend === "screen" ? "mix-blend-screen" : blend && "art-on-black", className)}
    />
  );
}
