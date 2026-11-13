/**
 * Catalog › Yours › Problems (v0.18 D12): what went wrong while the current catalog was built
 * (a plugin that did not load, a reserved namespace, an unsafe settings schema, …), grouped by
 * the distribution that caused it. Tenant admins and operators only: the caller decides, and
 * the server refuses everyone else (403).
 *
 * `message` is platform text; `untrusted_text` comes from the plugin (an exception, a schema
 * fragment) and is shown only as text in a clipped monospace block, never as markup.
 * Reference text: STE.
 */
import { RefreshCw } from "lucide-react";

import { ErrorNotice, LoadingRows, Spinner } from "@/components/common";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import { DIAGNOSTIC_LABEL, diagnosticsByDistribution, useCatalogDiagnostics, useRequestDescribe, type CatalogDiagnostic, type DiagnosticCode } from "./api";

/** Codes that mean "something is missing from the catalog" (attention) vs. "it loaded, with a change" (warning). */
const MISSING: ReadonlySet<DiagnosticCode> = new Set(["load_error", "namespace_reserved", "id_collision", "node_contract_invalid", "template_invalid"]);

export function diagnosticTone(code: DiagnosticCode): "attention" | "warn" {
  return MISSING.has(code) ? "attention" : "warn";
}

/** Plain summary of a list: "3 problems in 2 plugins". */
export function diagnosticsSummary(items: readonly CatalogDiagnostic[]): string {
  const dists = new Set(items.map((d) => d.distribution ?? ""));
  const n = items.length;
  const p = dists.size;
  return `${n} ${n === 1 ? "problem" : "problems"} in ${p} ${p === 1 ? "source" : "sources"}`;
}

function CodeChip({ code }: { code: DiagnosticCode }) {
  const tone = diagnosticTone(code);
  return (
    <span
      data-code={code}
      className={cn(
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-semibold whitespace-nowrap",
        tone === "attention" ? "bg-attention-soft text-attention" : "bg-[color-mix(in_srgb,var(--warning)_14%,var(--card))] text-warning",
      )}
    >
      {DIAGNOSTIC_LABEL[code] ?? code}
    </span>
  );
}

function Row({ d }: { d: CatalogDiagnostic }) {
  const where = [d.group, d.entry_point].filter(Boolean).join(" · ");
  return (
    <li className="grid gap-2 border-b py-3.5 last:border-b-0" data-testid="diagnostic">
      <div className="flex flex-wrap items-center gap-2">
        <CodeChip code={d.code} />
        {d.id ? <span className="font-mono text-xs font-medium [overflow-wrap:anywhere]">{d.id}</span> : null}
        {where ? <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{where}</span> : null}
      </div>
      <p className="text-sm">{d.message}</p>
      {d.untrusted_text ? (
        <pre aria-label="Text from the plugin" className="max-h-28 overflow-auto rounded-lg bg-muted px-3 py-2 font-mono text-xs leading-5 break-all whitespace-pre-wrap text-muted-foreground">
          {d.untrusted_text}
        </pre>
      ) : null}
    </li>
  );
}

/** The list itself (pure: give it the diagnostics). */
export function DiagnosticsList({ items }: { items: readonly CatalogDiagnostic[] }) {
  const groups = diagnosticsByDistribution(items);
  return (
    <div className="grid gap-4" data-testid="diagnostics-list">
      {[...groups.entries()].map(([dist, list]) => (
        <section key={dist ?? "-"} aria-label={dist ? `Problems from ${dist}` : "Problems not from one plugin"} className="rounded-2xl border bg-card px-5 py-2">
          <h4 className="flex flex-wrap items-baseline justify-between gap-2 pt-3 pb-1 text-base font-semibold">
            <span className="font-mono text-sm [overflow-wrap:anywhere]">{dist ?? "Not from one plugin"}</span>
            <span className="text-xs font-medium text-muted-foreground">
              {list.length} {list.length === 1 ? "problem" : "problems"}
            </span>
          </h4>
          <ul>
            {list.map((d, i) => (
              <Row key={`${d.code}-${d.entry_point ?? ""}-${d.id ?? ""}-${i}`} d={d} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/**
 * Problems, loaded (admins). Operators also get "Describe the catalog again" (a no-op when a
 * describe run is already waiting).
 */
export function CatalogProblems({ operator }: { operator: boolean }) {
  const diagnostics = useCatalogDiagnostics(true);
  const describe = useRequestDescribe();
  const items = diagnostics.data ?? [];
  return (
    <section aria-labelledby="catalog-problems" className="grid gap-3">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="grid gap-1">
          <h3 id="catalog-problems" className="text-xl leading-tight font-semibold">
            Problems
          </h3>
          <p className="max-w-[60ch] text-sm text-muted-foreground">
            Only admins see this list. It shows the plugins and schemas that did not load, or that loaded with a change, when the catalog was built.
          </p>
        </div>
        {operator ? (
          <Button size="sm" variant="outline" className="h-auto min-h-9 rounded-full px-4" disabled={describe.isPending} onClick={() => describe.mutate()}>
            {describe.isPending ? <Spinner /> : <RefreshCw />} Describe the catalog again
          </Button>
        ) : null}
      </div>
      {describe.isSuccess ? (
        <p role="status" className="text-sm text-success">
          The describe run is queued. The catalog changes when it completes.
        </p>
      ) : null}
      {describe.isError ? <ErrorNotice error={describe.error} title="The describe run did not start" /> : null}
      {diagnostics.isPending ? (
        <LoadingRows rows={3} />
      ) : diagnostics.isError ? (
        <ErrorNotice error={diagnostics.error} title="The app cannot load the problems" onRetry={() => void diagnostics.refetch()} />
      ) : items.length ? (
        <>
          <p className="text-sm font-medium">{diagnosticsSummary(items)}</p>
          <DiagnosticsList items={items} />
        </>
      ) : (
        <p className="rounded-2xl border border-dashed border-border-strong p-5 text-sm text-muted-foreground">No problems. Every plugin loaded.</p>
      )}
    </section>
  );
}
