/**
 * The test report (`PluginKitReport`, D8): the contract kits that ran on the upload in the
 * sandbox, grouped Failed, Passed, Skipped (with the reason). Messages come from the run that
 * imported the upload: untrusted text, shown as text only. Passing tests are a quality
 * signal, not a security check (T19.25), and the card says so.
 */
import { CheckCircle2, CircleSlash, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";

import { displayText, groupKit, KIT_STATUS_LABEL, kitCounts } from "./states";
import type { KitStatus, PluginKitReport } from "./types";

const ICON: Record<KitStatus, typeof CheckCircle2> = { pass: CheckCircle2, fail: XCircle, skipped: CircleSlash };
const INK: Record<KitStatus, string> = { pass: "text-success", fail: "text-destructive", skipped: "text-muted-foreground" };

const KIND_WORD: Record<string, string> = {
  model: "Model",
  step: "Prep step",
  plot: "Chart",
  node: "Workflow step",
  template: "Workflow template",
  package: "Package",
};

export function KitReport({ report }: { report: PluginKitReport | null | undefined }) {
  if (!report) return null;
  const groups = groupKit(report);
  const counts = kitCounts(report);
  return (
    <section data-testid="plugin-kit-report" aria-labelledby="plugin-kit-title" className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-4 rounded-2xl border bg-card p-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="grid gap-1">
          <h3 id="plugin-kit-title" className="font-display text-lg leading-tight font-semibold">
            Test report
          </h3>
          <p className="max-w-[60ch] text-sm text-muted-foreground">
            The same checks <code className="font-mono text-xs">mapi plugin test</code> runs, here in the sandbox on toy data. Passing tests show the code works as described. They don't prove it is safe, so a reviewer still looks.
          </p>
        </div>
        <p className="flex flex-wrap gap-1.5 text-xs font-semibold" aria-label="Test totals">
          {(["fail", "pass", "skipped"] as const).map((s) => (
            <span key={s} className={cn("rounded-full bg-muted px-2.5 py-0.5", counts[s] && INK[s])}>
              {counts[s]} {KIT_STATUS_LABEL[s].toLowerCase()}
            </span>
          ))}
        </p>
      </div>
      {groups.length === 0 ? <p className="text-sm text-muted-foreground">No tests ran.</p> : null}
      {groups.map((g) => {
        const Icon = ICON[g.status];
        return (
          <div key={g.status} className="grid gap-1.5" data-kit-group={g.status}>
            <h4 className={cn("text-xs font-semibold", INK[g.status])}>
              {KIT_STATUS_LABEL[g.status]} ({g.checks.length})
            </h4>
            <ul className="grid gap-1" aria-label={`${KIT_STATUS_LABEL[g.status]} tests`}>
              {g.checks.map((c, i) => (
                <li key={`${c.entry_id ?? ""}:${c.check}:${i}`} className="grid grid-cols-[1rem_minmax(0,1fr)] gap-x-2.5 rounded-lg px-1 py-1">
                  <Icon aria-hidden className={cn("mt-0.5 size-4", INK[g.status])} />
                  <div className="grid min-w-0 gap-0.5">
                    <p className="text-sm">
                      <span className="font-semibold">{displayText(c.check)}</span>
                      <span className="text-muted-foreground"> · {KIND_WORD[c.kind] ?? c.kind}</span>
                      {c.entry_id ? <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]"> {displayText(c.entry_id)}</span> : null}
                    </p>
                    {c.message ? <p className="text-sm text-muted-foreground [overflow-wrap:anywhere] whitespace-pre-line">{displayText(c.message)}</p> : null}
                  </div>
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}
