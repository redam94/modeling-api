/**
 * Guided "what-if → optimise my budget": after trying budgets by hand, let the model find
 * the best split (M3.5). Opens the optimisation page with a way back to the analysis.
 */
import { Link } from "@tanstack/react-router";
import { ArrowRight, Wallet } from "lucide-react";

import type { ModelVersion } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";

export function OptimiseBudgetCard({ mv, specId }: { mv: ModelVersion; specId: string }) {
  if (!mv.capabilities?.decision) return null;
  return (
    <Card data-testid="optimise-entry">
      <CardHeader>
        <div className="grid gap-1">
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Wallet aria-hidden className="size-4 text-primary" /> Optimise my budget
            </span>
          </CardTitle>
          <CardDescription>Let the model find the split that gives the best result. You do not move the sliders. The model uses your limits for each channel and shows the range of likely results.</CardDescription>
        </div>
        <Button asChild>
          <Link to="/models/$modelId/versions/$version/optimize" params={{ modelId: mv.model_id, version: String(mv.version) }} search={{ back: specId }}>
            Find the best split <ArrowRight />
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-muted-foreground">The plan keeps today&apos;s total, unless you select a different total. Nothing changes until you use the plan.</p>
      </CardContent>
    </Card>
  );
}
