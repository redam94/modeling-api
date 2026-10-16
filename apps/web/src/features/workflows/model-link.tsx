/**
 * From a workflow to the model it fitted: a `model_version` output (`model://project/name@vN`)
 * resolved against the project's models and linked to the Model Explorer (and the version's
 * validations). Nothing is shown while the model is unknown (another project, or deleted).
 */
import { Link } from "@tanstack/react-router";
import { ShieldCheck, Telescope } from "lucide-react";

import { useModels } from "@/api/queries";
import { Button } from "@/components/ui/button";

import type { NodeOutputRef } from "./types";

/** `model://<project>/<name>@v<N>` → its parts, or `null`. */
export function parseModelVersionRef(ref: string | null | undefined): { project: string; name: string; version: number } | null {
  const m = /^model:\/\/([^/\s]+)\/([^@\s/]+)@v(\d+)$/.exec(ref ?? "");
  if (!m) return null;
  const version = Number(m[3]);
  return version >= 1 ? { project: m[1]!, name: m[2]!, version } : null;
}

/** The model-version refs among a node's or a run's outputs (first one per ref). */
export function modelVersionRefs(outputs: Record<string, NodeOutputRef> | null | undefined): string[] {
  const refs = Object.values(outputs ?? {})
    .filter((o) => o.type === "model_version" && parseModelVersionRef(o.ref))
    .map((o) => o.ref!);
  return [...new Set(refs)];
}

/** The model id and version a ref names in the current project, once the models are loaded. */
export function useModelVersionTarget(ref: string | null | undefined): { modelId: string; version: number; name: string } | null {
  const parsed = parseModelVersionRef(ref);
  const models = useModels();
  if (!parsed) return null;
  const model = models.data?.items.find((m) => m.name === parsed.name);
  return model ? { modelId: model.id, version: parsed.version, name: parsed.name } : null;
}

/** "Open in the Model Explorer" (and, unless `compact`, the version's validations). */
export function ModelExplorerLinks({ modelRef, compact = false, size = "sm" }: { modelRef: string | null | undefined; compact?: boolean; size?: "sm" | "default" }) {
  const target = useModelVersionTarget(modelRef);
  if (!target) return null;
  const params = { modelId: target.modelId, version: String(target.version) };
  return (
    <span className="flex flex-wrap gap-2">
      <Button asChild size={size} variant="outline">
        <Link to="/models/$modelId/versions/$version/explore" params={params} aria-label={`Explore ${target.name} v${target.version} in the Model Explorer`}>
          <Telescope /> Explore {target.name} v{target.version}
        </Link>
      </Button>
      {compact ? null : (
        <Button asChild size={size} variant="ghost">
          <Link to="/models/$modelId/versions/$version/validations" params={params}>
            <ShieldCheck /> Validation
          </Link>
        </Button>
      )}
    </span>
  );
}
