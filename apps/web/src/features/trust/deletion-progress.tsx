/** A tenant deletion in words: its state, the grace period and the purge's progress per store. */
import type { DeletionState, StoreProgress, TenantDeletion } from "@/api/types";
import { Badge, Progress } from "@/components/ui/primitives";
import { absoluteTime, relativeTime } from "@/lib/format";

export const DELETION_META: Record<DeletionState, { label: string; variant: "warning" | "outline" | "danger" | "info" | "success" }> = {
  scheduled: { label: "Scheduled", variant: "warning" },
  cancelled: { label: "Cancelled", variant: "outline" },
  deleting: { label: "Deleting", variant: "danger" },
  purged: { label: "Purged", variant: "info" },
  failed: { label: "Failed", variant: "danger" },
};

export const STORE_LABEL: Record<StoreProgress["store"], string> = {
  database: "Database records",
  object_store: "Files (datasets, bundles, renders)",
  queue: "Queued work",
  logs: "Run logs",
  webhooks: "Webhooks and deliveries",
};

const STORE_STATE: Record<StoreProgress["state"], "outline" | "info" | "success" | "danger"> = { pending: "outline", running: "info", done: "success", failed: "danger" };

export function storeFraction(s: StoreProgress): number | null {
  if (s.state === "done") return 1;
  if (s.state === "pending") return 0;
  if (s.items_total == null || s.items_total <= 0) return null;
  return Math.min(1, (s.items_done ?? 0) / s.items_total);
}

export function DeletionProgress({ deletion }: { deletion: TenantDeletion }) {
  const meta = DELETION_META[deletion.state];
  return (
    <div className="grid gap-3">
      <p className="flex flex-wrap items-center gap-2 text-[13px]">
        <Badge variant={meta.variant}>{meta.label}</Badge>
        <span className="text-muted-foreground">
          Requested {relativeTime(deletion.requested_at)}
          {deletion.state === "scheduled" ? ` · the purge starts ${relativeTime(deletion.purge_after)} (${absoluteTime(deletion.purge_after)})` : ""}
          {deletion.state === "cancelled" && deletion.cancelled_at ? ` · cancelled ${relativeTime(deletion.cancelled_at)}` : ""}
          {deletion.state === "purged" && deletion.completed_at ? ` · finished ${relativeTime(deletion.completed_at)}` : ""}
        </span>
      </p>
      {deletion.stores?.length && deletion.state !== "cancelled" ? (
        <ul className="grid gap-2" aria-label="Deletion progress">
          {deletion.stores.map((s) => (
            <li key={s.store} className="grid gap-1">
              <div className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
                <span>{STORE_LABEL[s.store]}</span>
                <Badge variant={STORE_STATE[s.state]}>{s.state}</Badge>
              </div>
              <Progress value={storeFraction(s)} label={`${STORE_LABEL[s.store]}: ${s.state}`} barClassName={s.state === "failed" ? "bg-destructive" : undefined} />
              {s.detail ? <p className="text-xs text-muted-foreground">{s.detail}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {deletion.retained && Object.keys(deletion.retained).length ? (
        <details className="text-[13px]">
          <summary className="cursor-pointer text-muted-foreground">What is kept, and why</summary>
          <ul className="mt-1 list-disc pl-5 text-muted-foreground">
            {Object.entries(deletion.retained).map(([k, v]) => (
              <li key={k}>{v}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}
