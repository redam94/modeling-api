/** What a share link shows when it cannot open a dashboard: ended or removed, incomplete, or a failure. */
import { Link2Off } from "lucide-react";

import { InlineMessage } from "@/components/ods";
import { Button } from "@/components/ui/button";

export const LINK_ENDED_TEXT = "This link has ended or was removed. Ask the sender for a new link.";
export const LINK_INCOMPLETE_TEXT =
  "This page needs the full link. Open the link that you got again. If you reloaded the page, open the link from the message again.";

export function LinkEnded({ reason = "ended" }: { reason?: "ended" | "incomplete" }) {
  return (
    <div className="grid gap-3" data-share="ended">
      <div className="flex items-center gap-2.5">
        <Link2Off className="size-5 text-muted-foreground" aria-hidden />
        <h1 className="text-xl font-semibold tracking-tight">{reason === "ended" ? "The link has ended" : "Open the full link"}</h1>
      </div>
      <p className="text-base text-muted-foreground">{reason === "ended" ? LINK_ENDED_TEXT : LINK_INCOMPLETE_TEXT}</p>
    </div>
  );
}

/** A failure that may pass (the service could not be reached, too many requests). */
export function LinkProblem({ message, onRetry }: { message: string; onRetry?: (() => void) | undefined }) {
  return (
    <div className="grid gap-4" data-share="problem">
      <h1 className="text-xl font-semibold tracking-tight">We could not open the dashboard</h1>
      <InlineMessage tone="error">{message}</InlineMessage>
      {onRetry ? (
        <Button variant="secondary" className="w-fit" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}
