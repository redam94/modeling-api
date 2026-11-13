/**
 * The development banner (D7): this deployment runs uploaded code without a sandbox
 * (`MAPI_DEV_ALLOW_LOCAL_UNTRUSTED=1`, `GET /v1/me` `dev_untrusted_code`). Shown on the
 * upload, plugin and version pages and on Catalog › Yours. Coral: it is a warning, not a CTA.
 */
import { ShieldAlert } from "lucide-react";

import { useWorkspace } from "@/features/auth/workspace";
import { cn } from "@/lib/utils";

import { pluginFlags } from "./api";

export function DevBanner({ className }: { className?: string }) {
  const ws = useWorkspace();
  if (!pluginFlags(ws.me).devUntrusted) return null;
  return (
    <div
      role="status"
      data-testid="dev-untrusted-banner"
      className={cn("flex items-start gap-3 rounded-2xl border border-attention/30 bg-attention-soft px-4 py-3 text-sm text-foreground", className)}
    >
      <ShieldAlert aria-hidden className="mt-0.5 size-4 flex-none text-attention" />
      <p className="min-w-0">
        <span className="font-semibold text-attention">Development only: uploaded code runs without a sandbox here.</span>{" "}
        This server runs uploads as ordinary local processes. Do not upload code you do not trust, and do not use this setup for real work.
      </p>
    </div>
  );
}
