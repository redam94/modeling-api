/**
 * ProgressRing: how far through a short list someone is (the first-run checklist): a ring that
 * fills in the primary ink over a muted track, with "2/5" in the display face in the middle. A
 * progressbar to assistive tech ("2 of 5"); the fill eases over 600 ms (none under reduced motion).
 */
import { cn } from "@/lib/utils";

const R = 27;
const CIRCUMFERENCE = 2 * Math.PI * R;

export function ProgressRing({
  value,
  max,
  label,
  showText = true,
  className,
}: {
  /** Steps done (clamped to 0..max). */
  value: number;
  max: number;
  /** What is progressing ("Getting started"). */
  label: string;
  /** The "2/5" in the middle; leave out on rings smaller than 48 px. */
  showText?: boolean;
  className?: string;
}) {
  const total = Math.max(0, max);
  const done = Math.min(Math.max(0, value), total);
  const share = total > 0 ? done / total : 0;
  return (
    <svg
      viewBox="0 0 64 64"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={total}
      aria-valuenow={done}
      aria-valuetext={`${done} of ${total}`}
      data-slot="progress-ring"
      className={cn("size-16 flex-none", className)}
    >
      <circle className="fill-none stroke-muted" cx="32" cy="32" r={R} strokeWidth={7} />
      {done > 0 ? (
        <circle
          className="fill-none stroke-primary transition-[stroke-dashoffset] duration-600 ease-[cubic-bezier(0.2,0.8,0.2,1)]"
          cx="32"
          cy="32"
          r={R}
          strokeWidth={7}
          strokeLinecap="round"
          transform="rotate(-90 32 32)"
          strokeDasharray={CIRCUMFERENCE}
          strokeDashoffset={CIRCUMFERENCE * (1 - share)}
        />
      ) : null}
      {showText ? (
        <text aria-hidden x="32" y="37" textAnchor="middle" className="fill-foreground font-display text-[15px] font-bold tabular">
          {`${done}/${total}`}
        </text>
      ) : null}
    </svg>
  );
}
