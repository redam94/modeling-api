/**
 * Builder › Preview: the readout page as a client sees it, on the saved draft and the latest
 * published readout. Inputs show but send nothing (`preview`).
 */
import { InlineMessage } from "@/components/ods";

import { useMemberClient } from "../api";
import { ReadoutPage } from "../readout/readout-page";

export function PreviewTab({ dashboardId, dirty }: { dashboardId: string; dirty: boolean }) {
  const client = useMemberClient(dashboardId);
  return (
    <div className="grid min-w-0 gap-4">
      <InlineMessage tone="info" role="none">
        {dirty ? "The preview shows the saved draft. Save your changes to see them here. " : "This is what a client sees. "}
        Inputs show, but they send nothing from the preview.
      </InlineMessage>
      <div className="min-w-0 rounded-2xl border bg-background p-4 sm:p-6" data-testid="dashboard-preview">
        <ReadoutPage client={client} preview />
      </div>
    </div>
  );
}
