/**
 * Builder › Preview: the readout page as a client sees it, on the saved draft and the latest
 * published readout. Inputs show but send nothing (`preview`). A layout 2 draft renders the
 * panels readout (the readout page picks it); its picture panels are previewed below as the
 * live plots of the model version (signed in only; clients get the published picture).
 */
import { lazy, Suspense } from "react";

import { LoadingRows } from "@/components/common";
import { InlineMessage } from "@/components/ods";

import { useMemberClient } from "../api";
import { ReadoutPage } from "../readout/readout-page";
import { isLayout2, RECIPE_INTENTS } from "../types";
import type { RecipePreviewItem } from "./recipe-preview";
import { useSourceGrid } from "./source";
import { useBuilderDashboard } from "./use-builder";

const RecipePreview = lazy(() => import("./recipe-preview"));

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
      <PicturePreviews />
    </div>
  );
}

function PicturePreviews() {
  const d = useBuilderDashboard();
  const spec = d?.draft_spec;
  const grid = useSourceGrid(spec?.source ?? { kind: "endpoint", alias: null });
  if (!spec || !isLayout2(spec)) return null;
  const items: RecipePreviewItem[] = spec.panels.flatMap((p) => (p.kind === "recipe" && !(p.recipe in RECIPE_INTENTS) && p.plot_id ? [{ panelId: p.id, title: p.title, plotId: p.plot_id }] : []));
  const v = grid.version;
  if (!items.length || !v) return null;
  return (
    <Suspense fallback={<LoadingRows rows={3} />}>
      <RecipePreview modelId={v.model_id} version={v.version} items={items} />
    </Suspense>
  );
}
