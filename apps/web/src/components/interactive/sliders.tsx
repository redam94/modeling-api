/**
 * Sliders for what-ifs and beliefs (Guided explore and priors, the Home and Catalog playgrounds):
 * a single value and a low-high range, both Radix sliders with a labelled thumb. The look: a
 * pill track in the muted tint, the filled part in a series or primary colour, a card-coloured
 * thumb with a thick coloured ring. Signed-in surfaces only (never in share or client chunks).
 */
import { Slider as SliderPrimitive } from "radix-ui";

import { cn } from "@/lib/utils";

/** The colour of the filled part and the thumb ring: the primary ink or one chart series. */
export type SliderColor = "primary" | "series-1" | "series-2" | "series-3" | "series-4" | "series-5" | "series-6" | "series-7" | "series-8";

// Literal class names so Tailwind finds every one of them in the source.
const RANGE_CLASS: Record<SliderColor, string> = {
  primary: "bg-primary",
  "series-1": "bg-series-1",
  "series-2": "bg-series-2",
  "series-3": "bg-series-3",
  "series-4": "bg-series-4",
  "series-5": "bg-series-5",
  "series-6": "bg-series-6",
  "series-7": "bg-series-7",
  "series-8": "bg-series-8",
};

const THUMB_BORDER_CLASS: Record<SliderColor, string> = {
  primary: "border-primary",
  "series-1": "border-series-1",
  "series-2": "border-series-2",
  "series-3": "border-series-3",
  "series-4": "border-series-4",
  "series-5": "border-series-5",
  "series-6": "border-series-6",
  "series-7": "border-series-7",
  "series-8": "border-series-8",
};

const track = "relative h-2 w-full grow overflow-hidden rounded-full bg-muted";
const thumb =
  "block size-5 rounded-full border-[3px] bg-card shadow-sm transition-[box-shadow] focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-ring/40 disabled:pointer-events-none";

/** One value on a scale (what-if multipliers, horizons). */
export function ValueSlider({
  value,
  onValueChange,
  min,
  max,
  step,
  label,
  valueText,
  color = "primary",
  className,
  disabled,
}: {
  value: number;
  onValueChange: (v: number) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  valueText?: string;
  color?: SliderColor;
  className?: string;
  disabled?: boolean;
}) {
  return (
    <SliderPrimitive.Root
      className={cn("relative flex h-7 w-full touch-none items-center select-none data-[disabled]:opacity-50", className)}
      value={[value]}
      min={min}
      max={max}
      step={step}
      disabled={disabled}
      onValueChange={(v) => onValueChange(v[0] ?? value)}
    >
      <SliderPrimitive.Track className={track}>
        <SliderPrimitive.Range className={cn("absolute h-full", RANGE_CLASS[color])} />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className={cn(thumb, THUMB_BORDER_CLASS[color])} aria-label={label} {...(valueText ? { "aria-valuetext": valueText } : {})} />
    </SliderPrimitive.Root>
  );
}

/** A low-high range on a scale (ROI or share beliefs). */
export function RangeSlider({
  value,
  onValueChange,
  min,
  max,
  step,
  label,
  format,
  color = "primary",
  disabled,
}: {
  value: [number, number];
  onValueChange: (v: [number, number]) => void;
  min: number;
  max: number;
  step: number;
  label: string;
  format: (v: number) => string;
  color?: SliderColor;
  disabled?: boolean;
}) {
  return (
    <SliderPrimitive.Root
      className="relative flex h-7 w-full touch-none items-center select-none data-[disabled]:opacity-50"
      value={value}
      min={min}
      max={max}
      step={step}
      minStepsBetweenThumbs={1}
      disabled={disabled}
      onValueChange={(v) => onValueChange([v[0] ?? value[0], v[1] ?? value[1]])}
    >
      <SliderPrimitive.Track className={track}>
        <SliderPrimitive.Range className={cn("absolute h-full", RANGE_CLASS[color])} />
      </SliderPrimitive.Track>
      <SliderPrimitive.Thumb className={cn(thumb, THUMB_BORDER_CLASS[color])} aria-label={`${label}: low end`} aria-valuetext={format(value[0])} />
      <SliderPrimitive.Thumb className={cn(thumb, THUMB_BORDER_CLASS[color])} aria-label={`${label}: high end`} aria-valuetext={format(value[1])} />
    </SliderPrimitive.Root>
  );
}
