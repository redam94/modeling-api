/**
 * The move to the new layout (v0.17, plan §3.7 and §10.1).
 *
 * - {@link UpgradeBanner}: on a layout 1 draft, "Use the new layout" converts the saved draft
 *   to a layout 2 draft (`POST …:upgrade`, `If-Match` the draft etag). When the automatic
 *   upgrade stopped (`problem` `upgrade_needs_review`) the banner lists what stopped it, in
 *   plain words.
 * - {@link UpgradeResult}: after the move, what changed (`notes`) and what to fix before
 *   publishing (`issues`). Clients keep the old layout until the analyst publishes.
 *
 * The result outlives the banner (the Setup tab switches to the layout 2 form the moment the
 * new draft arrives), so it is kept here per dashboard, in memory only.
 */
import { ArrowUpRight, X } from "lucide-react";
import { useSyncExternalStore } from "react";

import { ApiError } from "@/api/client";
import type { Issue } from "@/api/types";
import { ErrorNotice, Spinner } from "@/components/common";
import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";
import { toast } from "@/stores/toast";

import { useUpgradeDashboard } from "../api";
import { isLayout2, type AnyDashboardSpec } from "../types";
import { sameSpec } from "./spec-form";
import { placeOfIssue } from "./spec-form-v2";
import { useBuilderDashboard } from "./use-builder";

// ------------------------------------------------------------------------------ the result store

interface UpgradeOutcome {
  issues: Issue[];
  notes: string[];
}

const outcomes = new Map<string, UpgradeOutcome>();
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());
const subscribe = (l: () => void) => {
  listeners.add(l);
  return () => void listeners.delete(l);
};

function setOutcome(id: string, o: UpgradeOutcome | null): void {
  if (o) outcomes.set(id, o);
  else outcomes.delete(id);
  emit();
}

function useOutcome(id: string | undefined): UpgradeOutcome | null {
  return useSyncExternalStore(
    subscribe,
    () => (id ? (outcomes.get(id) ?? null) : null),
    () => null,
  );
}

/** Forget every kept result (tests). */
export function resetUpgradeOutcomes(): void {
  outcomes.clear();
  emit();
}

// ------------------------------------------------------------------------------ issues in plain words

/** Issues as a plain list: "Channels: The response grid has no channel named tv." */
export interface PlainIssue {
  pointer?: string | null | undefined;
  message: string;
  suggested_action?: string | null | undefined;
}

export function PlainIssues({ issues, label }: { issues: readonly PlainIssue[]; label: string }) {
  if (!issues.length) return null;
  return (
    <ul aria-label={label} className="mt-2 grid list-disc gap-1 pl-5 text-sm">
      {issues.map((i, n) => {
        const place = placeOfIssue(i.pointer);
        return (
          <li key={`${i.pointer ?? ""}-${n}`} className="[overflow-wrap:anywhere]">
            {place ? <span className="font-medium">{place}: </span> : null}
            {i.message}
            {i.suggested_action ? <span className="block text-xs text-muted-foreground">{i.suggested_action}</span> : null}
          </li>
        );
      })}
    </ul>
  );
}

// ------------------------------------------------------------------------------ the banner

function staleError(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 412 || e.code === "DASHBOARD_SPEC_STALE");
}

/** "Use the new layout" on a layout 1 draft (nothing on a layout 2 one). */
export function UpgradeBanner({ spec, readOnly = false }: { spec: AnyDashboardSpec; readOnly?: boolean }) {
  const d = useBuilderDashboard();
  const upgrade = useUpgradeDashboard(d?.id ?? "");
  if (!d || isLayout2(spec) || isLayout2(d.draft_spec)) return null;
  const review = d.problem === "upgrade_needs_review";
  const dirty = !sameSpec(spec, d.draft_spec);
  const blocked = readOnly ? "Only editors and admins change dashboards." : dirty ? "Save or discard your changes first." : null;
  const issues = d.upgrade_issues ?? [];
  const run = () =>
    upgrade.mutate(d.draft_etag, {
      onSuccess: (out) => {
        setOutcome(d.id, { issues: out.issues ?? [], notes: out.notes ?? [] });
        toast.success("The draft uses the new layout", "Check the panels, then publish. Clients see the old layout until you publish.");
      },
    });
  return (
    <div data-testid="upgrade-banner" className="grid gap-2">
      <InlineMessage
        tone={review ? "warning" : "info"}
        role="none"
        action={
          <Button size="sm" disabled={Boolean(blocked) || upgrade.isPending} title={blocked ?? undefined} onClick={run}>
            {upgrade.isPending ? <Spinner /> : <ArrowUpRight />} Use the new layout
          </Button>
        }
      >
        {review ? (
          <>
            <span className="font-medium">This dashboard could not move to the new layout by itself.</span> Clients still see the old layout. Fix these points, or use the new layout and fix them
            there:
            {issues.length ? <PlainIssues issues={issues} label="What stopped the move to the new layout" /> : <span className="block">The app did not say why. Use the new layout to see the points to fix.</span>}
          </>
        ) : (
          <>
            <span className="font-medium">This dashboard uses the old layout.</span> The new layout works for any model type: you choose the panels, and clients can try cases and score files when the
            model allows it. The numbers come from the same model. Clients see the change only after you publish.
          </>
        )}
        {blocked && !readOnly ? <span className="mt-1 block text-xs text-muted-foreground">{blocked}</span> : null}
      </InlineMessage>
      {upgrade.isError ? <ErrorNotice error={upgrade.error} title={staleError(upgrade.error) ? "Another person changed the draft. Load their version, then try again." : "The draft did not move to the new layout"} /> : null}
    </div>
  );
}

/** What the move changed and what to fix (shown on the layout 2 setup after "Use the new layout"). */
export function UpgradeResult() {
  const d = useBuilderDashboard();
  const outcome = useOutcome(d?.id);
  if (!d || !outcome) return null;
  const errors = outcome.issues.filter((i) => i.severity !== "warning" && i.severity !== "info");
  const warnings = outcome.issues.filter((i) => i.severity === "warning" || i.severity === "info");
  return (
    <div data-testid="upgrade-result">
      <InlineMessage
        tone={errors.length ? "warning" : "success"}
        role="status"
        action={
          <Button size="icon-sm" variant="ghost" aria-label="Close the note about the new layout" onClick={() => setOutcome(d.id, null)}>
            <X />
          </Button>
        }
      >
        <span className="font-medium">The draft uses the new layout now.</span> Check the panels, then publish. Until you publish, clients see the old layout.
        {outcome.notes.length ? (
          <ul aria-label="What changed" className="mt-2 grid list-disc gap-1 pl-5 text-sm">
            {outcome.notes.map((n, i) => (
              <li key={i}>{n}</li>
            ))}
          </ul>
        ) : null}
        {errors.length ? (
          <>
            <span className="mt-2 block font-medium">Fix these before you publish:</span>
            <PlainIssues issues={errors} label="What to fix before you publish" />
          </>
        ) : null}
        {warnings.length ? <PlainIssues issues={warnings} label="Points to check" /> : null}
      </InlineMessage>
    </div>
  );
}
