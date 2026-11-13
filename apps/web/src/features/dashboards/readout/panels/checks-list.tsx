/** The live checks of an input form, in plain words, each passed or to fix (said in words too). */
import { Check, X } from "lucide-react";

import type { Check as CheckLine } from "../readout-model";

export function ChecksList({ checks }: { checks: readonly CheckLine[] }) {
  return (
    <ul aria-label="Checks" className="grid gap-2 text-sm">
      {checks.map((c) => (
        <li key={c.text} className="flex items-start gap-2" data-ok={c.ok}>
          {c.ok ? <Check aria-hidden className="mt-0.5 size-4 flex-none text-success" /> : <X aria-hidden className="mt-0.5 size-4 flex-none text-destructive" />}
          <span>
            <span className="sr-only">{c.ok ? "Passed: " : "Fix: "}</span>
            {c.text}
          </span>
        </li>
      ))}
    </ul>
  );
}
