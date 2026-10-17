/**
 * The banner under the header when the selected organisation is not active: suspended
 * (read-only; with a scheduled deletion, until when and how to cancel) or being deleted.
 */
import { Link } from "@tanstack/react-router";
import { AlertTriangle } from "lucide-react";

import { useTenantDeletion } from "@/api/trust";
import { useWorkspace } from "@/features/auth/workspace";
import { absoluteTime } from "@/lib/format";

export function TenantStatusBanner() {
  const ws = useWorkspace();
  const t = ws.current?.tenant;
  const admin = ws.mode === "session" && ws.current?.tenantRole === "admin";
  const status = t?.status ?? "active";
  const deletion = useTenantDeletion(t?.id ?? "", admin && status !== "active");
  if (!t || status === "active") return null;
  const d = deletion.data;
  const scheduled = d?.state === "scheduled";
  return (
    <div role="status" data-tenant-status={status} className="flex items-start gap-2 border-b border-warning/35 bg-warning/10 px-4 py-2 text-[13px] md:px-6">
      <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" />
      <p>
        {status === "deleting" ? (
          <>
            <span className="font-medium">The app is deleting {t.name}.</span> <span className="text-muted-foreground">You cannot change anything. The app removes its data now.</span>{" "}
            {admin ? (
              <Link to="/settings" hash="data" className="font-medium text-primary hover:underline">
                See the progress
              </Link>
            ) : null}
          </>
        ) : scheduled ? (
          <>
            <span className="font-medium">{t.name} is scheduled for deletion on {absoluteTime(d.purge_after)}.</span>{" "}
            <span className="text-muted-foreground">Until then, it is read-only. You can export its data or cancel the deletion.</span>{" "}
            <Link to="/settings" hash="data" className="font-medium text-primary hover:underline">
              Cancel or export
            </Link>
          </>
        ) : (
          <>
            <span className="font-medium">{t.name} is suspended.</span>{" "}
            <span className="text-muted-foreground">
              All items are read-only. You cannot change or run anything.
              {t.status_reason ? ` Reason: ${t.status_reason}` : ""}
            </span>
          </>
        )}
      </p>
    </div>
  );
}
