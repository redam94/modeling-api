/**
 * What the file check (`plugin_inspect`) refused, in plain words: the check's name (reference
 * text), the platform's message and the file it is about. Paths come from the upload: text only.
 */
import { XCircle } from "lucide-react";

import { INSPECT_CHECK_LABEL, displayText } from "./states";
import type { InspectProblem } from "./types";

export function ProblemsList({ problems, title = "What to fix" }: { problems: readonly InspectProblem[]; title?: string }) {
  if (!problems.length) return null;
  return (
    <section data-testid="plugin-problems" aria-labelledby="plugin-problems-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-3 rounded-2xl border border-destructive/30 bg-card p-5">
      <div className="grid gap-1">
        <h3 id="plugin-problems-title" className="font-display text-lg leading-tight font-bold">
          {title}
        </h3>
        <p className="text-sm text-muted-foreground">
          We stopped before running anything from the file. Fix {problems.length === 1 ? "this problem" : `these ${problems.length} problems`}, then upload a new version.
        </p>
      </div>
      <ol className="grid gap-2">
        {problems.map((p, i) => (
          <li key={`${p.check}:${p.path ?? ""}:${i}`} data-check={p.check} className="grid grid-cols-[1rem_minmax(0,1fr)] gap-x-2.5 border-t pt-2 first:border-t-0 first:pt-0">
            <XCircle aria-hidden className="mt-0.5 size-4 text-destructive" />
            <div className="grid min-w-0 gap-0.5">
              <p className="text-sm font-semibold">{INSPECT_CHECK_LABEL[p.check] ?? p.check}</p>
              <p className="text-sm [overflow-wrap:anywhere]">{displayText(p.message)}</p>
              {p.path ? <p className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{displayText(p.path)}</p> : null}
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}
