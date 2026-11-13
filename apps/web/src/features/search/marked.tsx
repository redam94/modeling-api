import type { ReactNode } from "react";

import type { Range } from "./rank";

/**
 * `text` with the matched parts in `<mark>`: bold ink with a 2px orange underline, never a
 * fill. React nodes only (Trusted Types forbid HTML strings).
 */
export function Marked({ text, ranges }: { text: string; ranges: readonly Range[] }) {
  if (!ranges.length) return <>{text}</>;
  const out: ReactNode[] = [];
  let pos = 0;
  for (const [start, end] of ranges) {
    if (start > pos) out.push(text.slice(pos, start));
    out.push(
      <mark key={start} className="bg-transparent font-semibold text-inherit underline decoration-chart-now decoration-2 underline-offset-[3px]">
        {text.slice(start, end)}
      </mark>,
    );
    pos = end;
  }
  if (pos < text.length) out.push(text.slice(pos));
  return <>{out}</>;
}
